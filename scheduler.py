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
# ARCHITECTURE NOTE (revised 31 May 2026):
#   Ares agent imports are LAZY (inside the wrapper functions) so that
#   one broken or missing Ares module cannot take down the entire
#   scheduler. Previous version imported all agents at module level,
#   which crashed the scheduler with ModuleNotFoundError when aiohttp
#   wasn't installed. Don't go back to top-level Ares imports.
#
# CURRENTLY WIRED AGENTS (revised 5 June 2026):
#   Mímir: feed_ingestion, triage, daily_digest      (sync)
#   Lex:   ingestion, digest                          (sync)
#   Ares:  feed_ingestion, triage, verifier,          (async)
#          digest_morning, digest_midday, digest_postclose
#
# COMMENTED OUT (not yet built):
#   Ares: financial_snapshot, deep_analysis,
#         trade_proposal, execution
#
# Job schedule:
#   ── UTC anchored (UK-relevant) ──────────────────────────────
#   Every 6 hours        — Mímir feed ingestion (00, 06, 12, 18 UTC)
#   Every 6 hours :30    — Mímir triage
#   3x daily             — Ares feed ingestion (05, 12, 19 UTC)
#   3x daily             — Ares triage         (05:30, 12:15, 19:15 UTC)
#   Every day 07:00 UTC  — Mímir daily digest
#   Every day 07:15 UTC  — Ares morning digest
#   Every day 07:30 UTC  — Ares verifier
#   Every day 08:00 UTC  — Lex regulatory ingestion
#   Every day 08:30 UTC  — Lex digest
#
#   ── America/New_York anchored (DST-aware) ──────────────────
#   Every day 09:00 NY   — Ares midday digest    (pre-NYSE-open heads-up)
#   Every day 16:30 NY   — Ares postclose digest (after NYSE close)
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
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from core.config import load_config
from core.database import init_pool

# Top-level imports ONLY for modules known to work synchronously.
# Ares agents are imported LAZILY inside their wrapper functions —
# do NOT add Ares imports here. See architecture note above.
from mimir.agents import feed_ingestion, triage, digest
from lex.agents import ingestion as lex_ingestion, digest as lex_digest

# ── Logging setup ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("trygg.scheduler")


# =============================================================
# Mímir wrappers (synchronous — proven stable)
# =============================================================

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


# =============================================================
# Lex wrappers (synchronous)
# =============================================================

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


# =============================================================
# Ares wrappers — LAZY IMPORTS (do not move to module level)
# =============================================================
# Each wrapper imports its agent inside the try block. If the agent
# module doesn't exist or has a syntax/dependency error, the wrapper
# logs SKIPPED and the scheduler keeps running.
#
# This is the architectural fix for the 31 May restart-loop bug.
# =============================================================

def run_ares_feed_ingestion():
    """Fetch GNews articles for Ares entities and store as raw signals."""
    logger.info("── Starting: ares.feed_ingestion ──")
    try:
        from ares.agents import feed_ingestion as ares_feed_ingestion
        asyncio.run(ares_feed_ingestion.run_feed_ingestion())
        logger.info("── Complete: ares.feed_ingestion ──")
    except ImportError as e:
        logger.error(f"── SKIPPED: ares.feed_ingestion not available — {e} ──")
    except Exception as e:
        logger.error(f"── FAILED: ares.feed_ingestion — {e} ──")


def run_ares_triage():
    """Score untriaged signals in ares.signals using Claude Haiku 4.5."""
    logger.info("── Starting: ares.triage ──")
    try:
        from ares.agents import triage as ares_triage
        asyncio.run(ares_triage.run_triage())
        logger.info("── Complete: ares.triage ──")
    except ImportError as e:
        logger.error(f"── SKIPPED: ares.triage not available — {e} ──")
    except Exception as e:
        logger.error(f"── FAILED: ares.triage — {e} ──")


def run_ares_verifier():
    """Process PENDING VERIFICATION items via EDGAR + Firecrawl + Sonnet."""
    logger.info("── Starting: ares.verifier ──")
    try:
        from ares.agents import verifier as ares_verifier
        asyncio.run(ares_verifier.run_verifier())
        logger.info("── Complete: ares.verifier ──")
    except ImportError as e:
        logger.error(f"── SKIPPED: ares.verifier not available — {e} ──")
    except Exception as e:
        logger.error(f"── FAILED: ares.verifier — {e} ──")


def run_ares_digest_morning():
    """Comprehensive 24h-lookback briefing delivered at 07:15 UTC."""
    logger.info("── Starting: ares.digest [morning] ──")
    try:
        from ares.agents import digest as ares_digest
        asyncio.run(ares_digest.run_digest(briefing_type="morning"))
        logger.info("── Complete: ares.digest [morning] ──")
    except ImportError as e:
        logger.error(f"── SKIPPED: ares.digest [morning] not available — {e} ──")
    except Exception as e:
        logger.error(f"── FAILED: ares.digest [morning] — {e} ──")


def run_ares_digest_midday():
    """Pre-NYSE-open heads-up at 09:00 America/New_York (6h lookback)."""
    logger.info("── Starting: ares.digest [midday] ──")
    try:
        from ares.agents import digest as ares_digest
        asyncio.run(ares_digest.run_digest(briefing_type="midday"))
        logger.info("── Complete: ares.digest [midday] ──")
    except ImportError as e:
        logger.error(f"── SKIPPED: ares.digest [midday] not available — {e} ──")
    except Exception as e:
        logger.error(f"── FAILED: ares.digest [midday] — {e} ──")


def run_ares_digest_postclose():
    """Post-NYSE-close brief at 16:30 America/New_York (11h lookback)."""
    logger.info("── Starting: ares.digest [postclose] ──")
    try:
        from ares.agents import digest as ares_digest
        asyncio.run(ares_digest.run_digest(briefing_type="postclose"))
        logger.info("── Complete: ares.digest [postclose] ──")
    except ImportError as e:
        logger.error(f"── SKIPPED: ares.digest [postclose] not available — {e} ──")
    except Exception as e:
        logger.error(f"── FAILED: ares.digest [postclose] — {e} ──")


# ─── TODO: Ares agents not yet built ──────────────────────────
# Uncomment and wire up each as the corresponding agent is built.
# Each follows the lazy-import pattern above.
#
# def run_ares_financial_snapshot(): ...
# def run_ares_deep_analysis(): ...
# def run_ares_trade_proposal(): ...
# def run_ares_execution(): ...


# =============================================================
# Startup check
# =============================================================

def run_startup_check():
    """Run Mímir ingestion and triage immediately on startup."""
    logger.info("Trygg Platform starting up...")
    logger.info(f"Environment: {config.environment}")
    logger.info(f"Triage model: {config.model_triage}")
    logger.info(f"Synthesis model: {config.model_synthesis}")
    logger.info("Running initial Mímir feed scan on startup...")
    run_feed_ingestion()
    run_triage()
    logger.info("Startup scan complete. Scheduler is now running.")


# =============================================================
# Main
# =============================================================

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

    # ── Lex ───────────────────────────────────────────────────

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

    # ── Ares: Feed Ingestion ─────────────────────────────────
    # 3x daily, UTC-anchored, spaced to feed each briefing window
    # while staying inside GNews 100/day rate limit.

    scheduler.add_job(
        run_ares_feed_ingestion,
        trigger=CronTrigger(hour="5,12,19", minute=0),
        id="ares_feed_ingestion",
        name="Ares Feed Ingestion",
        misfire_grace_time=300,
    )

    # ── Ares: Triage ─────────────────────────────────────────
    # Runs 15-30 min after each ingestion to score fresh signals.
    # NB: 12:15 sits cleanly between Mímir 12:00 ingestion and 12:30 triage.

    scheduler.add_job(
        run_ares_triage,
        trigger=CronTrigger(hour=5, minute=30),
        id="ares_triage_morning",
        name="Ares Triage (morning)",
        misfire_grace_time=300,
    )

    scheduler.add_job(
        run_ares_triage,
        trigger=CronTrigger(hour=12, minute=15),
        id="ares_triage_midday",
        name="Ares Triage (midday)",
        misfire_grace_time=300,
    )

    scheduler.add_job(
        run_ares_triage,
        trigger=CronTrigger(hour=19, minute=15),
        id="ares_triage_postclose",
        name="Ares Triage (postclose)",
        misfire_grace_time=300,
    )

    # ── Ares: Briefings ───────────────────────────────────────
    # Morning at UTC (UK-anchored). Midday and postclose at NY local
    # (DST-aware via timezone parameter on CronTrigger).

    scheduler.add_job(
        run_ares_digest_morning,
        trigger=CronTrigger(hour=7, minute=15),
        id="ares_digest_morning",
        name="Ares Morning Digest",
        misfire_grace_time=600,
    )

    scheduler.add_job(
        run_ares_digest_midday,
        trigger=CronTrigger(hour=9, minute=0, timezone="America/New_York"),
        id="ares_digest_midday",
        name="Ares Midday Brief",
        misfire_grace_time=600,
    )

    scheduler.add_job(
        run_ares_digest_postclose,
        trigger=CronTrigger(hour=16, minute=30, timezone="America/New_York"),
        id="ares_digest_postclose",
        name="Ares Post-Close Brief",
        misfire_grace_time=600,
    )

    # ── Ares: Verifier ────────────────────────────────────────
    # Daily after morning brief — processes PENDING VERIFICATION items
    # surfaced by overnight signal flow.

    scheduler.add_job(
        run_ares_verifier,
        trigger=CronTrigger(hour=7, minute=30),
        id="ares_verifier",
        name="Ares Verifier",
        misfire_grace_time=600,
    )

    # ─── TODO: Ares execution jobs (uncomment as agents land) ──
    # scheduler.add_job(run_ares_financial_snapshot,
    #     trigger=CronTrigger(hour=6, minute=15),
    #     id="ares_financial_snapshot", name="Ares Financial Snapshot",
    #     misfire_grace_time=300)
    #
    # scheduler.add_job(run_ares_deep_analysis,
    #     trigger=CronTrigger(day_of_week="sun", hour=6, minute=0),
    #     id="ares_deep_analysis", name="Ares Deep Analysis",
    #     misfire_grace_time=600)
    #
    # scheduler.add_job(run_ares_trade_proposal,
    #     trigger=CronTrigger(hour=13, minute=45),
    #     id="ares_trade_proposal", name="Ares Trade Proposal",
    #     misfire_grace_time=300)
    #
    # scheduler.add_job(run_ares_execution,
    #     trigger=CronTrigger(hour=14, minute=30),
    #     id="ares_execution", name="Ares Execution",
    #     misfire_grace_time=120)

    run_startup_check()
    logger.info("Scheduler running. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Scheduler stopped.")
