# =============================================================
# scheduler.py
# =============================================================
# The main scheduler — the heartbeat of the entire Trygg platform.
#
# Job schedule:
#   00:00, 06:00, 12:00, 18:00 UTC  — Mímir feed ingestion
#   00:30, 06:30, 12:30, 18:30 UTC  — Mímir triage
#   07:00 UTC daily                  — Mímir digest
#   08:00 UTC daily                  — Lex ingestion
#   08:30 UTC daily                  — Lex digest
#
#   06:00 UTC daily                  — Ares feed ingestion
#   06:15 UTC daily                  — Ares financial snapshot
#   06:30 UTC daily                  — Ares triage (after Mímir triage)
#   07:15 UTC daily                  — Ares digest
#   Sunday 06:00 UTC                 — Ares deep analysis (Opus 4.7)
# =============================================================
import asyncio
import logging
import sys
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from core.config import load_config
from core.database import init_pool

# Mímir agents
from mimir.agents import feed_ingestion, triage, digest
from lex.agents import ingestion as lex_ingestion, digest as lex_digest

# Ares agents
from ares.agents import feed_ingestion as ares_feed_ingestion
from ares.agents import financial_snapshot as ares_financial_snapshot
from ares.agents import triage as ares_triage
from ares.agents import digest as ares_digest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("trygg.scheduler")


# ── Mímir wrappers ────────────────────────────────────────────

def run_feed_ingestion():
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


def run_startup_check():
    logger.info("Trygg Platform starting up...")
    logger.info(f"Environment: {config.environment}")
    logger.info(f"Triage model: {config.model_triage}")
    logger.info(f"Synthesis model: {config.model_synthesis}")
    logger.info("Running initial Mímir feed scan on startup...")
    run_feed_ingestion()
    run_triage()
    logger.info("Startup scan complete. Scheduler is now running.")


if __name__ == "__main__":
    try:
        config = load_config()
        init_pool(config.db_url)
    except Exception as e:
        logger.critical(f"Startup failed: {e}")
        sys.exit(1)

    scheduler = BlockingScheduler(timezone="UTC")

    # ── Mímir ────────────────────────────────────────────────
    scheduler.add_job(
        run_feed_ingestion,
        trigger=CronTrigger(hour="0,6,12,18", minute=0),
        id="mimir_feed_ingestion",
        name="Mímir Feed Ingestion",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        run_triage,
        trigger=CronTrigger(hour="0,6,12,18", minute=30),
        id="mimir_triage",
        name="Mímir Triage",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        run_daily_digest,
        trigger=CronTrigger(hour=7, minute=0),
        id="mimir_daily_digest",
        name="Mímir Daily Digest",
        misfire_grace_time=600,
    )

    # ── Lex ──────────────────────────────────────────────────
    scheduler.add_job(
        run_lex_ingestion,
        trigger=CronTrigger(hour=8, minute=0),
        id="lex_ingestion",
        name="Lex Ingestion",
        misfire_grace_time=600,
    )
    scheduler.add_job(
        run_lex_digest,
        trigger=CronTrigger(hour=8, minute=30),
        id="lex_digest",
        name="Lex Digest",
        misfire_grace_time=600,
    )

    # ── Ares ─────────────────────────────────────────────────
    scheduler.add_job(
        run_ares_feed_ingestion,
        trigger=CronTrigger(hour=6, minute=0),
        id="ares_feed_ingestion",
        name="Ares Feed Ingestion",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        run_ares_financial_snapshot,
        trigger=CronTrigger(hour=6, minute=15),
        id="ares_financial_snapshot",
        name="Ares Financial Snapshot",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        run_ares_triage,
        trigger=CronTrigger(hour=6, minute=45),
        id="ares_triage",
        name="Ares Triage",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        run_ares_digest,
        trigger=CronTrigger(hour=7, minute=15),
        id="ares_digest",
        name="Ares Digest",
        misfire_grace_time=600,
    )

    run_startup_check()
    logger.info("Scheduler running. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Scheduler stopped.")
