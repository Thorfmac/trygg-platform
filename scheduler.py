# =============================================================
# scheduler.py
# =============================================================
# The main scheduler — the heartbeat of the entire Trygg platform.
#
# What it does:
#   - Runs continuously in the background
#   - Fires agent jobs at defined intervals
#   - Handles errors gracefully (one failed job doesn't stop others)
#   - Logs everything to stdout (captured by Docker / systemd)
#
# Job schedule at launch:
#   Every 6 hours  — Mímir feed ingestion (scan news for all companies)
#   Every 6 hours  — Mímir triage (score any unscored signals)
#   Every day 7am  — Mímir daily digest (assemble and send reports)
#   Every day 8am  — Lex regulatory ingestion (check FCA/PRA/ESMA/ICO/EBA feeds)
#   Every day 8:30 — Lex digest (send regulatory briefing)
#
#   Every day 6:00 — Ares feed ingestion
#   Every day 6:15 — Ares financial snapshot
#   Every day 6:45 — Ares triage
#   Every day 7:15 — Ares digest
#   Every Sunday 6:00 — Ares deep analysis (Opus 4.7)
#
# How to run:
#   python scheduler.py
#
# In production this runs inside a Docker container managed
# by docker-compose, so it restarts automatically if it crashes.
# =============================================================
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from core.config import load_config
from core.database import init_pool
# Import agents — add new ones here as modules are built
from mimir.agents import feed_ingestion, triage, digest
from lex.agents import ingestion as lex_ingestion, digest as lex_digest
# Ares agents
from ares.agents import feed_ingestion as ares_feed_ingestion
from ares.agents import financial_snapshot as ares_financial_snapshot
from ares.agents import triage as ares_triage
from ares.agents import digest as ares_digest
from ares.agents import deep_analysis as ares_deep_analysis
from ares.execution import trade_proposal as ares_trade_proposal

# ── Logging setup ─────────────────────────────────────────────
# Logs go to stdout — Docker captures them automatically
# Format: timestamp | level | module | message
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("trygg.scheduler")


# ── Mímir wrappers ────────────────────────────────────────────

def run_feed_ingestion():
    """Wrapper so APScheduler can call the agent cleanly."""
    logger.info("── Starting: mimir.feed_ingestion ──")
    try:
        result = feed_ingestion.run(config)
        logger.info(f"── Complete: {result['signals_new']} new signals ──")
    except Exception as e:
        logger.error(f"── FAILED: mimir.feed_ingestion — {e} ──")


def run_triage():
    logger.info("── Starting: mimir.triage ──")
    try:
        result = triage.run(config)
        logger.info(
            f"── Complete: {result.get('scored', 0)} scored, "
            f"{result.get('high_value_count', 0)} high-value ──"
        )
        for sig in result.get("high_value_signals", []):
            if sig["score"] >= 9:
                logger.info(f"  Firing alert for {sig['company_name']} (score {sig['score']})")
                digest.send_alert(sig["signal_id"], config)
    except Exception as e:
        logger.error(f"── FAILED: mimir.triage — {e} ──")


def run_daily_digest():
    logger.info("── Starting: mimir.digest ──")
    try:
        result = digest.run(config)
        logger.info(
            f"── Complete: {result.get('digests_sent', 0)} sent, "
            f"{result.get('digests_empty', 0)} empty ──"
        )
    except Exception as e:
        logger.error(f"── FAILED: mimir.digest — {e} ──")


def run_lex_ingestion():
    logger.info("── Starting: lex.ingestion ──")
    try:
        result = lex_ingestion.run(config)
        logger.info(f"── Complete: {result['items_new']} new items ──")
    except Exception as e:
        logger.error(f"── FAILED: lex.ingestion — {e} ──")


def run_lex_digest():
    logger.info("── Starting: lex.digest ──")
    try:
        result = lex_digest.run(config)
        logger.info(f"── Complete: {result['digests_sent']} sent ──")
    except Exception as e:
        logger.error(f"── FAILED: lex.digest — {e} ──")


# ── Ares wrappers ─────────────────────────────────────────────

def run_ares_feed_ingestion():
    logger.info("── Starting: ares.feed_ingestion ──")
    try:
        asyncio.run(ares_feed_ingestion.run_feed_ingestion())
        logger.info("── Complete: ares.feed_ingestion ──")
    except Exception as e:
        logger.error(f"── FAILED: ares.feed_ingestion — {e} ──")


def run_ares_financial_snapshot():
    logger.info("── Starting: ares.financial_snapshot ──")
    try:
        asyncio.run(ares_financial_snapshot.run_financial_snapshot())
        logger.info("── Complete: ares.financial_snapshot ──")
    except Exception as e:
        logger.error(f"── FAILED: ares.financial_snapshot — {e} ──")


def run_ares_triage():
    logger.info("── Starting: ares.triage ──")
    try:
        asyncio.run(ares_triage.run_triage())
        logger.info("── Complete: ares.triage ──")
    except Exception as e:
        logger.error(f"── FAILED: ares.triage — {e} ──")


def run_ares_digest():
    logger.info("── Starting: ares.digest ──")
    try:
        asyncio.run(ares_digest.run_digest())
        logger.info("── Complete: ares.digest ──")
    except Exception as e:
        logger.error(f"── FAILED: ares.digest — {e} ──")


def run_ares_deep_analysis():
    logger.info("── Starting: ares.deep_analysis ──")
    try:
        asyncio.run(ares_deep_analysis.run_deep_analysis(
            analysis_type="weekly_review",
            trigger_event="Scheduled weekly run",
        ))
        logger.info("── Complete: ares.deep_analysis ──")
    except Exception as e:
        logger.error(f"── FAILED: ares.deep_analysis — {e} ──")


def run_ares_trade_proposal():
    logger.info("── Starting: ares.trade_proposal ──")
    try:
        asyncio.run(ares_trade_proposal.run_trade_proposal())
        logger.info("── Complete: ares.trade_proposal ──")
    except Exception as e:
        logger.error(f"── FAILED: ares.trade_proposal — {e} ──")


def run_ares_execution():
    logger.info("── Starting: ares.execution ──")
    try:
        asyncio.run(ares_trade_proposal.run_execution())
        logger.info("── Complete: ares.execution ──")
    except Exception as e:
        logger.error(f"── FAILED: ares.execution — {e} ──")


def run_startup_check():
    """Run ingestion and triage immediately on startup."""
    logger.info("Trygg Platform starting up...")
    logger.info(f"Environment: {config.environment}")
    logger.info(f"Triage model: {config.model_triage}")
    logger.info(f"Synthesis model: {config.model_synthesis}")
    logger.info("Running initial feed scan on startup...")
    run_feed_ingestion()
    run_triage()
    logger.info("Startup scan complete. Scheduler is now running.")


if __name__ == "__main__":
    # ── Load config and initialise database ──────────────────
    try:
        config = load_config()
        init_pool(config.db_url)
    except Exception as e:
        logger.critical(f"Startup failed: {e}")
        sys.exit(1)

    # ── Create the scheduler ──────────────────────────────────
    scheduler = BlockingScheduler(timezone="UTC")

    # ── Mímir ─────────────────────────────────────────────────

    # Feed ingestion — every 6 hours
    scheduler.add_job(
        run_feed_ingestion,
        trigger=CronTrigger(hour="0,6,12,18", minute=0),
        id="mimir_feed_ingestion",
        name="Mímir Feed Ingestion",
        misfire_grace_time=300,
    )

    # Triage — 30 minutes after each feed ingestion
    scheduler.add_job(
        run_triage,
        trigger=CronTrigger(hour="0,6,12,18", minute=30),
        id="mimir_triage",
        name="Mímir Triage",
        misfire_grace_time=300,
    )

    # Daily digest — 07:00 UTC (08:00 UK summer)
    scheduler.add_job(
        run_daily_digest,
        trigger=CronTrigger(hour=7, minute=0),
        id="mimir_daily_digest",
        name="Mímir Daily Digest",
        misfire_grace_time=600,
    )

    # ── Lex ───────────────────────────────────────────────────

    # Lex ingestion — 08:00 UTC daily
    scheduler.add_job(
        run_lex_ingestion,
        trigger=CronTrigger(hour=8, minute=0),
        id="lex_ingestion",
        name="Lex Ingestion",
        misfire_grace_time=600,
    )

    # Lex digest — 08:30 UTC daily (after ingestion completes)
    scheduler.add_job(
        run_lex_digest,
        trigger=CronTrigger(hour=8, minute=30),
        id="lex_digest",
        name="Lex Digest",
        misfire_grace_time=600,
    )

    # ── Ares ──────────────────────────────────────────────────

    # Feed ingestion — 06:00 UTC daily
    scheduler.add_job(
        run_ares_feed_ingestion,
        trigger=CronTrigger(hour=6, minute=0),
        id="ares_feed_ingestion",
        name="Ares Feed Ingestion",
        misfire_grace_time=300,
    )

    # Financial snapshot — 06:15 UTC daily
    scheduler.add_job(
        run_ares_financial_snapshot,
        trigger=CronTrigger(hour=6, minute=15),
        id="ares_financial_snapshot",
        name="Ares Financial Snapshot",
        misfire_grace_time=300,
    )

    # Triage — 06:45 UTC daily
    scheduler.add_job(
        run_ares_triage,
        trigger=CronTrigger(hour=6, minute=45),
        id="ares_triage",
        name="Ares Triage",
        misfire_grace_time=300,
    )

    # Digest — 07:15 UTC daily
    scheduler.add_job(
        run_ares_digest,
        trigger=CronTrigger(hour=7, minute=15),
        id="ares_digest",
        name="Ares Digest",
        misfire_grace_time=600,
    )

    # Deep analysis — Sundays 06:00 UTC (Opus 4.7)
    scheduler.add_job(
        run_ares_deep_analysis,
        trigger=CronTrigger(day_of_week="sun", hour=6, minute=0),
        id="ares_deep_analysis",
        name="Ares Deep Analysis",
        misfire_grace_time=600,
    )

    # ── Run startup check immediately, then hand off ──────────
    # Trade proposal — 13:45 UTC (45 mins before US open)
    scheduler.add_job(
        run_ares_trade_proposal,
        trigger=CronTrigger(hour=13, minute=45),
        id="ares_trade_proposal",
        name="Ares Trade Proposal",
        misfire_grace_time=300,
    )

    # Execution — 14:30 UTC (US market open)
    scheduler.add_job(
        run_ares_execution,
        trigger=CronTrigger(hour=14, minute=30),
        id="ares_execution",
        name="Ares Execution",
        misfire_grace_time=120,
    )

    run_startup_check()
    logger.info("Scheduler running. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Scheduler stopped.")
