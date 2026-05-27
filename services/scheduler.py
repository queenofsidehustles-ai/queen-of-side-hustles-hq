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

    def publish_job():
        with app.app_context():
            _run_auto_publish()

    _scheduler.add_job(
        daily_job,
        CronTrigger(hour=hour, minute=minute),
        id="daily_generate",
    )

    from apscheduler.triggers.interval import IntervalTrigger
    _scheduler.add_job(
        publish_job,
        IntervalTrigger(minutes=1),
        id="auto_publish",
    )

    _scheduler.start()
    logger.info("Daily auto-generate scheduled at %02d:%02d UTC (enabled=%s)", hour, minute, enabled)
    logger.info("Auto-publish scheduler running every minute")


def _run_auto_publish():
    """Check for scheduled posts that are due and publish them via GetLate."""
    import json
    from datetime import datetime
    from extensions import db
    from models import ContentItem
    from services.getlate import publish_to_all_platforms

    now = datetime.utcnow()
    due_items = ContentItem.query.filter(
        ContentItem.status == "scheduled",
        ContentItem.scheduled_at <= now,
        ContentItem.brand.in_(["pbh", "pbh_user"]),
    ).all()

    if not due_items:
        return

    logger.info("Auto-publish: %d scheduled post(s) are due", len(due_items))

    for item in due_items:
        try:
            content_dict = {
                "script":       item.script or "",
                "platform":     item.platform or "tiktok",
                "image_url":    item.image_url or "",
                "r2_image_url": item.r2_image_url or "",
                "video_url":    item.video_url or "",
                "r2_video_url": item.r2_video_url or "",
            }
            captions = {}
            if item.captions:
                try:
                    captions = json.loads(item.captions)
                except Exception:
                    pass

            result = publish_to_all_platforms(content_item=content_dict, captions_dict=captions)

            if result.get("status") != "error":
                item.status = "published"
                item.published_at = datetime.utcnow()
                db.session.commit()
                logger.info("Auto-published item %d to: %s", item.id,
                            ", ".join(result.get("platforms_published", [])))
            else:
                logger.warning("Auto-publish failed for item %d: %s",
                               item.id, result.get("error", "unknown"))
        except Exception:
            logger.exception("Error auto-publishing item %d", item.id)


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
