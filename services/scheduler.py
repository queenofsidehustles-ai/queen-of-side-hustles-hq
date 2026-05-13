"""
services/scheduler.py — Daily auto-generate scheduler
=======================================================
Runs generate_clips() at a configured time each day.

With gunicorn --preload --workers 2:
  - App loads in master process, then workers are forked.
  - Threads don't survive fork, so scheduler only runs in master. ✅

With werkzeug dev server:
  - WERKZEUG_RUN_MAIN check prevents double-start from reloader. ✅
"""

import logging
import os

logger = logging.getLogger(__name__)

_scheduler = None


def init_scheduler(app):
    global _scheduler

    # Dev mode: werkzeug reloader spawns two processes — only run in the child
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("APScheduler not installed — daily auto-generate disabled")
        return

    with app.app_context():
        try:
            from models import Setting
            enabled = Setting.get("AUTO_GENERATE_ENABLED", "false") == "true"
            time_str = Setting.get("AUTO_GENERATE_TIME", "07:00")
        except Exception:
            enabled = False
            time_str = "07:00"

    try:
        hour, minute = map(int, time_str.split(":"))
    except Exception:
        hour, minute = 7, 0

    _scheduler = BackgroundScheduler(daemon=True)

    def daily_job():
        with app.app_context():
            _run_daily_generate(app)

    _scheduler.add_job(
        daily_job,
        CronTrigger(hour=hour, minute=minute),
        id="daily_generate",
    )
    _scheduler.start()
    logger.info("Daily auto-generate scheduled at %02d:%02d UTC (enabled=%s)", hour, minute, enabled)


def _run_daily_generate(app):
    """Generate clips — skips if already ran today."""
    logger = logging.getLogger(__name__)
    try:
        from datetime import date
        from extensions import db
        from models import ContentItem, Setting

        if Setting.get("AUTO_GENERATE_ENABLED", "false") != "true":
            logger.info("Daily auto-generate: disabled in settings, skipping")
            return

        # Deduplicate: skip if library clips were already created today
        today = date.today()
        already = ContentItem.query.filter(
            ContentItem.input_type == "library",
            db.func.date(ContentItem.created_at) == today,
        ).count()
        if already > 0:
            logger.info("Daily auto-generate: %d clips already created today, skipping", already)
            return

        count = int(Setting.get("AUTO_GENERATE_COUNT", "3"))
        logger.info("Daily auto-generate: generating %d clips", count)

        from services.clip_generator import generate_clips
        results = generate_clips(count=count)
        succeeded = sum(1 for r in results if r.get("ok"))
        logger.info("Daily auto-generate complete: %d/%d clips created", succeeded, len(results))

    except Exception:
        logger.exception("Daily auto-generate failed")
