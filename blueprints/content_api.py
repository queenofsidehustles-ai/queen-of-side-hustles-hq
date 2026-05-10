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
            "stage": stage,
            "status": status,
            "message": message,
            "detail": detail,
        }))

    def run():
        with app.app_context():
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
    item = ContentItem(
        input_text=data.get("input_text", ""),
        input_type=data.get("input_type", "idea"),
        platform=data.get("platform", "tiktok"),
        include_video=data.get("include_video", False),
        status="draft",
    )
    db.session.add(item)
    db.session.commit()
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
            "stage": stage,
            "status": status,
            "message": message,
            "detail": detail,
        }))

    def run():
        with app.app_context():
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
