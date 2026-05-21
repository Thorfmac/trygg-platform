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
# How to run:
#   python scheduler.py
#
# In production this runs inside a Docker container managed
# by docker-compose, so it restarts automatically if it crashes.
# =============================================================
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

    # Feed ingestion — every 6 hours
    # Runs at: 00:00, 06:00, 12:00, 18:00 UTC
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

    # ── Run startup check immediately, then hand off ──────────
    run_startup_check()
    logger.info("Scheduler running. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Scheduler stopped.")