"""
blueprints/content_api.py — Content JSON API Blueprint
=======================================================
JSON API routes for content items and the SSE pipeline stream.
Registered at url_prefix="/content/api".

Teaching notes:
- SSE (Server-Sent Events): real-time pipeline updates without WebSockets
- Threading: pipeline runs in background thread, results queued to stream
- publish_post() in services/getlate.py takes a content_item dict — not
  individual caption/platform/media args. We build that dict here.
"""

import json
import mimetypes
import os
import queue
import threading
import uuid
from datetime import datetime
from flask import Blueprint, request, jsonify, Response, current_app
from auth import login_required
from models import ContentItem, PipelineLog
from extensions import db

content_api_bp = Blueprint("content_api", __name__)


@content_api_bp.route("/create", methods=["POST"])
@login_required
def create():
    """Create content item and run pipeline via SSE stream."""
    data = request.get_json() or {}

    item = ContentItem(
        input_text=data.get("input_text", ""),
        input_type=data.get("input_type", "idea"),
        platform=data.get("platform", "tiktok"),
        include_video=data.get("include_video", False),
        status="draft",
    )
    db.session.add(item)
    db.session.commit()

    content_id = item.id
    q = queue.Queue()
    app = current_app._get_current_object()

    def emit(stage, status, message, detail=""):
        q.put(json.dumps({
            "content_id": content_id,
            "stage": stage, "status": status,
            "message": message, "detail": detail,
        }))
        try:
            log_entry = PipelineLog(
                content_id=content_id, stage=stage, status=status,
                message=str(message)[:500], detail=str(detail)[:500],
            )
            db.session.add(log_entry)
            db.session.commit()
        except Exception:
            pass

    def run():
        with app.app_context():
            PipelineLog.query.filter_by(content_id=content_id).delete()
            db.session.commit()
            from pipeline import run_pipeline
            run_pipeline(content_id, emit)
        q.put("DONE")

    threading.Thread(target=run, daemon=True).start()

    def stream():
        while True:
            try:
                msg = q.get(timeout=960)  # 16 min max (longer than video timeout)
                if msg == "DONE":
                    yield f"data: {json.dumps({'stage': 'done', 'status': 'complete', 'content_id': content_id})}\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'stage': 'done', 'status': 'timeout'})}\n\n"
                break

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@content_api_bp.route("/create-draft", methods=["POST"])
@login_required
def create_draft():
    """Create a content item without running the pipeline. Returns JSON with item id."""
    data = request.get_json() or {}
    # as_ready=True used by "Post As-Is" flow — skip pipeline, mark ready immediately
    status = "ready" if data.get("as_ready") else "draft"
    # brand: 'kpps' (default) or 'pbh_user' (Party Biz Hub flow)
    brand = data.get("brand", "kpps")
    if brand not in ("kpps", "pbh_user", "bear_hug"):
        brand = "kpps"
    item = ContentItem(
        input_text=data.get("input_text", ""),
        input_type=data.get("input_type", "idea"),
        platform=data.get("platform", "tiktok"),
        include_video=data.get("include_video", False),
        skip_voiceover=bool(data.get("skip_voiceover", False)),
        brand=brand,
        status=status,
    )
    db.session.add(item)
    db.session.commit()

    # If a library clip was selected, attach it as the video source
    clip_id = data.get("clip_id")
    if clip_id:
        try:
            from models import LibraryVideo
            clip = LibraryVideo.query.get(int(clip_id))
            if clip:
                item.r2_video_url = clip.r2_url
                item.video_url    = clip.r2_url
                item.input_type   = "video"  # "library" is reserved for batch-generated clips
                clip.used_count   = (clip.used_count or 0) + 1
                db.session.commit()
        except Exception:
            pass

    return jsonify({"id": item.id})


@content_api_bp.route("/<int:item_id>/run-pipeline", methods=["POST"])
@login_required
def run_pipeline_route(item_id):
    """Run the pipeline on an existing draft content item. Returns SSE stream."""
    item = ContentItem.query.get_or_404(item_id)
    content_id = item.id
    q = queue.Queue()
    app = current_app._get_current_object()

    def emit(stage, status, message, detail=""):
        q.put(json.dumps({
            "content_id": content_id,
            "stage": stage, "status": status,
            "message": message, "detail": detail,
        }))
        try:
            log_entry = PipelineLog(
                content_id=content_id, stage=stage, status=status,
                message=str(message)[:500], detail=str(detail)[:500],
            )
            db.session.add(log_entry)
            db.session.commit()
        except Exception:
            pass

    def run():
        with app.app_context():
            PipelineLog.query.filter_by(content_id=content_id).delete()
            db.session.commit()
            from pipeline import run_pipeline
            run_pipeline(content_id, emit)
        q.put("DONE")

    threading.Thread(target=run, daemon=True).start()

    def stream():
        while True:
            try:
                msg = q.get(timeout=960)
                if msg == "DONE":
                    yield f"data: {json.dumps({'stage': 'done', 'status': 'complete', 'content_id': content_id})}\n\n"
                    break
                yield f"data: {msg}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'stage': 'done', 'status': 'timeout'})}\n\n"
                break

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@content_api_bp.route("/items", methods=["GET"])
@login_required
def list_items():
    """List all content items as JSON."""
    status_filter = request.args.get("status")
    query = ContentItem.query.order_by(ContentItem.created_at.desc())
    if status_filter:
        query = query.filter_by(status=status_filter)
    items = query.all()
    return jsonify([item.to_dict() for item in items])


@content_api_bp.route("/<int:item_id>", methods=["GET"])
@login_required
def get_item(item_id):
    """Get a single content item."""
    item = ContentItem.query.get_or_404(item_id)
    data = item.to_dict()
    data["logs"] = [log.to_dict() for log in item.pipeline_logs]
    return jsonify(data)


@content_api_bp.route("/<int:item_id>", methods=["DELETE"])
@login_required
def delete_item(item_id):
    """Delete a content item."""
    item = ContentItem.query.get_or_404(item_id)
    db.session.delete(item)
    db.session.commit()
    return jsonify({"success": True})


@content_api_bp.route("/clear-ai-clips", methods=["POST"])
@login_required
def clear_ai_clips():
    """Delete all AI-generated library clips from the queue."""
    deleted = ContentItem.query.filter_by(input_type="library").delete()
    db.session.commit()
    return jsonify({"ok": True, "deleted": deleted})


@content_api_bp.route("/<int:item_id>/set-video-url", methods=["POST"])
@login_required
def set_video_url(item_id):
    """Update video URL for a content item (used to fix MOV → MP4 migrations)."""
    item = ContentItem.query.get_or_404(item_id)
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    item.r2_video_url = url
    item.video_url = url
    db.session.commit()
    return jsonify({"success": True, "video_url": url})


@content_api_bp.route("/<int:item_id>/publish", methods=["POST"])
@login_required
def publish(item_id):
    """
    Publish content item via GetLate.dev.

    publish_post() in services/getlate.py expects:
        content_item: dict (script, image_url, r2_image_url, video_url,
                            r2_video_url, platform, scheduled_at)
        platforms:    list of platform name strings (optional)
        emit_event:   SSE callback (optional)

    We build a content_item dict from the ORM object and pass it through.
    """
    item = ContentItem.query.get_or_404(item_id)
    from services.getlate import publish_post

    # Build the content_item dict that publish_post() expects
    content_item = {
        "script": item.script or "",
        "platform": item.platform or "tiktok",
        "image_url": item.image_url or "",
        "r2_image_url": item.r2_image_url or "",
        "video_url": item.video_url or "",
        "r2_video_url": item.r2_video_url or "",
        "scheduled_at": getattr(item, "scheduled_at", None),
    }

    # Resolve caption for the item's platform
    if item.captions:
        try:
            captions_dict = json.loads(item.captions)
            content_item["script"] = captions_dict.get(
                item.platform,
                captions_dict.get("default", item.script or "")
            )
        except (json.JSONDecodeError, AttributeError):
            pass  # fall back to item.script set above

    # Accept scheduled_at from request body
    body = request.get_json(silent=True) or {}
    scheduled_at_str = body.get("scheduled_at")
    if scheduled_at_str:
        try:
            item.scheduled_at = datetime.fromisoformat(scheduled_at_str)
            item.status = "scheduled"
            db.session.commit()
            content_item["scheduled_at"] = item.scheduled_at
        except (ValueError, TypeError):
            pass

    result = publish_post(
        content_item=content_item,
        platforms=[item.platform] if item.platform else None,
    )

    if not scheduled_at_str:
        item.status = "published"
        item.published_at = datetime.utcnow()
    db.session.commit()

    return jsonify(result)


@content_api_bp.route("/accounts", methods=["GET"])
@login_required
def list_accounts():
    """Return connected Zernio social accounts so the UI can show a platform picker."""
    from services.getlate import _get_headers, GETLATE_BASE_URL
    import requests as req_lib
    headers = _get_headers()
    if not headers:
        return jsonify({"accounts": [], "demo": True,
                        "message": "Add your Zernio API key in Settings to see connected accounts."})
    try:
        r = req_lib.get(f"{GETLATE_BASE_URL}/accounts", headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        raw = data if isinstance(data, list) else data.get("accounts", [])
        simplified = []
        for a in raw:
            if not a.get("_id"):
                continue
            # Extract profile name — Zernio may nest it or use a flat field
            profile_raw = a.get("profile") or {}
            if isinstance(profile_raw, dict):
                profile_name = profile_raw.get("name") or "Default"
            else:
                profile_name = str(profile_raw) if profile_raw else "Default"
            simplified.append({
                "id":       a.get("_id", ""),
                "platform": a.get("platform", ""),
                "username": a.get("username") or a.get("name") or "",
                "profile":  profile_name,
            })
        return jsonify({"accounts": simplified, "demo": False})
    except Exception as e:
        return jsonify({"accounts": [], "demo": False, "error": str(e)})


@content_api_bp.route("/calendar", methods=["GET"])
@login_required
def calendar_data():
    """Return scheduled and published items for the calendar view.
    Query params: year (int), month (int, 1-12).
    Returns items grouped by date string YYYY-MM-DD.
    """
    from datetime import date
    year  = int(request.args.get("year",  date.today().year))
    month = int(request.args.get("month", date.today().month))

    # Build date window: full month
    import calendar as cal_mod
    _, days_in_month = cal_mod.monthrange(year, month)
    start_dt = datetime(year, month, 1)
    end_dt   = datetime(year, month, days_in_month, 23, 59, 59)

    items = ContentItem.query.filter(
        ContentItem.status.in_(["scheduled", "published"]),
        db.or_(
            db.and_(ContentItem.scheduled_at >= start_dt, ContentItem.scheduled_at <= end_dt),
            db.and_(ContentItem.published_at >= start_dt, ContentItem.published_at <= end_dt),
        )
    ).order_by(ContentItem.scheduled_at, ContentItem.published_at).all()

    BRAND_COLOR = {"kpps": "#C7A35A", "pbh": "#8b5cf6", "pbh_user": "#8b5cf6", "bear_hug": "#f59e0b"}

    result = []
    for item in items:
        dt = item.scheduled_at or item.published_at
        if not dt:
            continue
        result.append({
            "id":      item.id,
            "date":    dt.strftime("%Y-%m-%d"),
            "time":    dt.strftime("%-I:%M %p"),
            "status":  item.status,
            "brand":   item.brand or "kpps",
            "color":   BRAND_COLOR.get(item.brand or "kpps", "#C7A35A"),
            "platform": item.platform or "",
            "preview": (item.script or item.caption or "")[:60],
        })

    return jsonify({"year": year, "month": month, "items": result})


@content_api_bp.route("/<int:item_id>/publish-all", methods=["POST"])
@login_required
def publish_all(item_id):
    """
    Publish (or schedule) this content item to connected social platforms.
    Accepts optional { "scheduled_at": "...", "account_ids": ["id1","id2"] } in body.
    If account_ids is provided, only those accounts receive the post.
    """
    item = ContentItem.query.get_or_404(item_id)
    from services.getlate import publish_to_all_platforms

    body = request.get_json(silent=True) or {}
    scheduled_at_str = body.get("scheduled_at")
    account_ids = body.get("account_ids") or None  # None = post to all

    # Persist scheduled_at if provided
    if scheduled_at_str:
        try:
            item.scheduled_at = datetime.fromisoformat(scheduled_at_str)
            item.status = "scheduled"
            db.session.commit()
        except (ValueError, TypeError):
            scheduled_at_str = None

    # Parse captions dict for platform-specific copy
    captions_dict = {}
    if item.captions:
        try:
            captions_dict = json.loads(item.captions)
        except (json.JSONDecodeError, AttributeError):
            pass

    content_item = {
        "script": item.script or "",
        "image_url": item.image_url or "",
        "r2_image_url": item.r2_image_url or "",
        "video_url": item.video_url or "",
        "r2_video_url": item.r2_video_url or "",
    }

    try:
        result = publish_to_all_platforms(
            content_item=content_item,
            captions_dict=captions_dict,
            scheduled_at=scheduled_at_str,
            allowed_account_ids=account_ids,
        )
    except Exception as e:
        return jsonify({"status": "error", "error": str(e), "platforms_published": [],
                        "platforms_failed": []}), 500

    if not scheduled_at_str and result.get("platforms_published"):
        item.status = "published"
        item.published_at = datetime.utcnow()
        db.session.commit()

    return jsonify(result)


@content_api_bp.route("/<int:item_id>/diagnose", methods=["GET"])
@login_required
def diagnose_publish(item_id):
    """
    Diagnostic endpoint — shows exactly what would be sent to Zernio
    and what Zernio's connected accounts look like. Does NOT publish.
    """
    item = ContentItem.query.get_or_404(item_id)
    from services.r2_storage import is_configured as r2_ok
    import requests as req_lib
    import os

    # What media URLs the item has
    media_info = {
        "image_url": item.image_url or None,
        "r2_image_url": item.r2_image_url or None,
        "video_url": item.video_url or None,
        "r2_video_url": item.r2_video_url or None,
        "r2_configured": r2_ok(),
    }

    # What captions exist
    captions_info = {}
    if item.captions:
        try:
            captions_info = json.loads(item.captions)
            captions_info = {k: v[:80] + "..." if len(v) > 80 else v for k, v in captions_info.items()}
        except Exception:
            captions_info = {"error": "could not parse captions JSON"}

    # Fetch Zernio connected accounts
    zernio_key = os.getenv("ZERNIO_API_KEY") or os.getenv("GETLATE_API_KEY")
    zernio_info = {}
    if not zernio_key:
        zernio_info = {"error": "No ZERNIO_API_KEY set in Railway variables"}
    else:
        try:
            r = req_lib.get("https://getlate.dev/api/v1/accounts",
                            headers={"Authorization": f"Bearer {zernio_key}", "Content-Type": "application/json"},
                            timeout=10)
            zernio_info = {"status_code": r.status_code, "response": r.json()}
        except Exception as e:
            zernio_info = {"error": str(e)}

    return jsonify({
        "item_id": item_id,
        "platform": item.platform,
        "status": item.status,
        "media": media_info,
        "captions_preview": captions_info,
        "zernio_accounts": zernio_info,
    })


@content_api_bp.route("/upload", methods=["POST"])
@login_required
def upload_media():
    """Upload a photo or video file directly to R2 storage."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    from services.r2_storage import is_configured, _get_client, _get_public_url

    if not is_configured():
        return jsonify({"error": "R2 storage not configured — add R2 keys in Railway variables"}), 500

    file_data = file.read()
    filename = file.filename
    content_type = file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    is_video = content_type.startswith("video/")
    folder = "videos" if is_video else "images"
    ext = os.path.splitext(filename)[1] or (mimetypes.guess_extension(content_type) or (".mp4" if is_video else ".jpg"))

    # MOV files (video/quicktime) are rejected by TikTok/Instagram/YouTube.
    # iPhone MOV uses H.264/AAC — same codec as MP4 — so we store it as .mp4
    # so social media platforms accept it.
    if is_video and (content_type == "video/quicktime" or ext.lower() == ".mov"):
        content_type = "video/mp4"
        ext = ".mp4"

    key = f"{folder}/{uuid.uuid4().hex}{ext}"

    client = _get_client()
    bucket = os.environ["R2_BUCKET_NAME"]
    client.put_object(Bucket=bucket, Key=key, Body=file_data, ContentType=content_type)
    url = _get_public_url(key)

    # Link to content item if item_id provided
    item_id = request.form.get("item_id")
    if item_id:
        item = ContentItem.query.get(int(item_id))
        if item:
            if is_video:
                item.r2_video_url = url
                item.video_url = url
            else:
                item.r2_image_url = url
                item.image_url = url
            db.session.commit()

    return jsonify({"url": url, "key": key, "type": "video" if is_video else "image"})


@content_api_bp.route("/<int:item_id>/add-music", methods=["POST"])
@login_required
def add_music(item_id):
    """Mix a background music track into the item's video at low volume."""
    item = ContentItem.query.get_or_404(item_id)

    if "file" not in request.files:
        return jsonify({"error": "No audio file provided"}), 400

    video_url = item.r2_video_url or item.video_url
    if not video_url:
        return jsonify({"error": "No video attached to this post yet"}), 400

    music_bytes = request.files["file"].read()
    if not music_bytes:
        return jsonify({"error": "Empty audio file"}), 400

    volume = float(request.form.get("volume", "0.25"))
    volume = max(0.05, min(0.6, volume))  # clamp 5–60 %

    from services.video_processor import mix_background_music
    new_url = mix_background_music(video_url, music_bytes, volume=volume)

    if not new_url:
        return jsonify({"error": "Music mixing failed — check that FFmpeg is installed on the server"}), 500

    item.r2_video_url = new_url
    item.video_url    = new_url
    db.session.commit()

    return jsonify({"url": new_url})


@content_api_bp.route("/<int:item_id>/thumbnail", methods=["POST"])
@login_required
def set_thumbnail(item_id):
    """Save a JPEG frame (captured from the review video) as the post thumbnail."""
    item = ContentItem.query.get_or_404(item_id)

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    from services.r2_storage import is_configured, _get_client, _get_public_url

    file = request.files["file"]
    file_data = file.read()

    if not file_data:
        return jsonify({"error": "Empty file"}), 400

    if not is_configured():
        return jsonify({"error": "R2 storage not configured — add R2 keys in Railway variables"}), 500

    key = f"thumbnails/{uuid.uuid4().hex}.jpg"
    client = _get_client()
    bucket = os.environ["R2_BUCKET_NAME"]
    client.put_object(Bucket=bucket, Key=key, Body=file_data, ContentType="image/jpeg")
    url = _get_public_url(key)

    item.image_url    = url
    item.r2_image_url = url
    db.session.commit()

    return jsonify({"url": url})
