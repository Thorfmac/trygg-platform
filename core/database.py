# =============================================================
# platform/database.py
# =============================================================
# Handles all database connections and provides helper functions
# used by every agent across all modules.
#
# We use psycopg2 — the standard PostgreSQL driver for Python.
# A connection pool means we don't open/close a new connection
# on every database call, which would be slow.
# =============================================================

import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.pool
import psycopg2.extras

logger = logging.getLogger(__name__)


# The connection pool — created once when the application starts
# min=1 connection always open, max=5 for burst activity
_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def init_pool(db_url: str) -> None:
    """
    Create the connection pool. Call this once at startup.
    """
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=5,
        dsn=db_url,
        # Return rows as dictionaries (column name → value)
        # rather than plain tuples — much easier to work with
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    logger.info("Database connection pool initialised")


@contextmanager
def get_conn():
    """
    Borrow a connection from the pool, use it, then return it.

    Usage:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT ...")

    The 'with' block automatically commits on success
    and rolls back if anything goes wrong.
    """
    if _pool is None:
        raise RuntimeError("Database pool not initialised — call init_pool() first")

    conn = _pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


# ── Job run helpers ──────────────────────────────────────────
# Every agent creates a job run record at the start and updates
# it at the end. This gives us a full audit trail automatically.

def start_job_run(job_name: str, job_module: str) -> str:
    """
    Record that a scheduled job has started.
    Returns the job run ID (used to close the record later).
    """
    job_id = str(uuid.uuid4())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO platform.job_runs
                    (id, job_name, job_module, status, started_at)
                VALUES (%s, %s, %s, 'running', NOW())
            """, (job_id, job_name, job_module))
    logger.info(f"Job started: {job_name} [{job_id}]")
    return job_id


def complete_job_run(
    job_id: str,
    records_processed: int = 0,
    metadata: dict | None = None,
) -> None:
    """Mark a job run as successfully completed."""
    import json
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE platform.job_runs
                SET status = 'completed',
                    completed_at = NOW(),
                    records_processed = %s,
                    metadata = %s
                WHERE id = %s
            """, (records_processed, json.dumps(metadata or {}), job_id))
    logger.info(f"Job completed: {job_id} ({records_processed} records)")


def fail_job_run(job_id: str, error_message: str) -> None:
    """Mark a job run as failed with the error message."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE platform.job_runs
                SET status = 'failed',
                    completed_at = NOW(),
                    error_message = %s
                WHERE id = %s
            """, (error_message[:2000], job_id))  # truncate very long errors
    logger.error(f"Job failed: {job_id} — {error_message}")


# ── Company helpers ──────────────────────────────────────────

def get_active_companies() -> list[dict]:
    """
    Return all active companies on any active watchlist.
    This is what the feed scanner iterates over.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT c.*
                FROM mimir.companies c
                JOIN mimir.watchlist_companies wc ON wc.company_id = c.id
                JOIN mimir.watchlists w ON w.id = wc.watchlist_id
                WHERE c.is_active = TRUE
                  AND w.is_active = TRUE
                ORDER BY c.name
            """)
            return [dict(row) for row in cur.fetchall()]


def company_exists_by_slug(slug: str) -> bool:
    """Check whether a company slug already exists."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM mimir.companies WHERE slug = %s",
                (slug,)
            )
            return cur.fetchone() is not None


# ── Signal helpers ───────────────────────────────────────────

def signal_exists(content_hash: str) -> bool:
    """
    Check whether we've already stored this signal.
    Used to prevent duplicate articles being processed.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 1 FROM mimir.signals
                WHERE content_hash = %s AND is_duplicate = FALSE
            """, (content_hash,))
            return cur.fetchone() is not None


def insert_signal(signal: dict) -> str:
    """
    Store a new signal in the database.
    Returns the signal ID.
    """
    import json
    signal_id = str(uuid.uuid4())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO mimir.signals (
                    id, company_id, signal_type, headline, summary,
                    source_url, source_name, published_at, discovered_at,
                    content_hash, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, NOW(),
                    %s, %s
                )
            """, (
                signal_id,
                signal["company_id"],
                signal["signal_type"],
                signal["headline"],
                signal.get("summary"),
                signal.get("source_url"),
                signal.get("source_name"),
                signal.get("published_at"),
                signal["content_hash"],
                json.dumps(signal.get("metadata", {})),
            ))
    return signal_id


def update_signal_scores(
    signal_id: str,
    relevance_score: int,
    sentiment: str,
    investment_implication: str,
    model_used: str,
) -> None:
    """
    Write the AI scoring results back to a signal record.
    Called by the triage agent after scoring.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE mimir.signals
                SET relevance_score = %s,
                    sentiment = %s,
                    investment_implication = %s,
                    model_used = %s,
                    scored_at = NOW()
                WHERE id = %s
            """, (
                relevance_score,
                sentiment,
                investment_implication,
                model_used,
                signal_id,
            ))
