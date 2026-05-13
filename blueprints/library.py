"""
blueprints/library.py — Video Library
======================================
Upload, tag, and manage party video clips.
The AI content generator pulls from this library to build daily posts.
"""

import logging
import os
import subprocess
import tempfile
import uuid

from flask import Blueprint, jsonify, render_template, request
from sqlalchemy import func

from auth import login_required
from extensions import db
from models import LibraryVideo

logger = logging.getLogger(__name__)

library_bp = Blueprint("library", __name__)

TAGS = LibraryVideo.TAGS


def _get_duration(file_path: str) -> float:
    """Return video duration in seconds via ffprobe. Falls back to 0."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             file_path],
            capture_output=True, text=True, timeout=15,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


@library_bp.route("/", methods=["GET"])
@login_required
def index():
    tag_filter = request.args.get("tag", "")
    query = LibraryVideo.query
    if tag_filter and tag_filter in TAGS:
        query = query.filter_by(tag=tag_filter)
    videos = query.order_by(LibraryVideo.created_at.desc()).all()

    tag_counts = dict(
        db.session.query(LibraryVideo.tag, func.count(LibraryVideo.id))
        .group_by(LibraryVideo.tag).all()
    )

    return render_template(
        "admin/library.html",
        videos=videos,
        tags=TAGS,
        tag_filter=tag_filter,
        tag_counts=tag_counts,
        total=LibraryVideo.query.count(),
    )


@library_bp.route("/upload", methods=["POST"])
@login_required
def upload():
    from services.r2_storage import _get_client, _get_public_url, is_configured

    file = request.files.get("video")
    if not file or not file.filename:
        return jsonify({"error": "No video file provided"}), 400

    tag   = request.form.get("tag", "other")
    notes = (request.form.get("notes", "") or "").strip() or None

    ext = os.path.splitext(file.filename)[1].lower() or ".mp4"
    allowed = {".mp4", ".mov", ".m4v", ".avi", ".webm", ".mkv", ".hevc", ".3gp"}
    if ext not in allowed:
        return jsonify({"error": f"File type {ext} not supported. Use MP4 or MOV."}), 400

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name

        duration = _get_duration(tmp_path)

        if not is_configured():
            return jsonify({"error": "R2 storage not configured — add R2 keys in Settings"}), 500

        key    = f"library/{uuid.uuid4().hex}{ext}"
        client = _get_client()
        with open(tmp_path, "rb") as f:
            client.put_object(
                Bucket=os.environ["R2_BUCKET_NAME"],
                Key=key,
                Body=f,
                ContentType=file.content_type or "video/mp4",
            )

        url   = _get_public_url(key)
        video = LibraryVideo(
            filename=file.filename,
            r2_url=url,
            r2_key=key,
            tag=tag,
            duration=duration,
            notes=notes,
        )
        db.session.add(video)
        db.session.commit()

        return jsonify({"ok": True, "id": video.id, "url": url,
                        "duration": video.duration_fmt})

    except Exception as e:
        logger.exception("library upload error")
        db.session.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@library_bp.route("/<int:video_id>/delete", methods=["POST"])
@login_required
def delete_video(video_id):
    video = LibraryVideo.query.get_or_404(video_id)

    if video.r2_key:
        try:
            from services.r2_storage import _get_client
            _get_client().delete_object(
                Bucket=os.environ["R2_BUCKET_NAME"], Key=video.r2_key
            )
        except Exception as e:
            logger.warning("R2 delete failed for %s: %s", video.r2_key, e)

    db.session.delete(video)
    db.session.commit()
    return jsonify({"ok": True})


@library_bp.route("/<int:video_id>/update", methods=["POST"])
@login_required
def update_video(video_id):
    video = LibraryVideo.query.get_or_404(video_id)
    data  = request.get_json(silent=True) or {}

    if "tag" in data and data["tag"] in TAGS:
        video.tag = data["tag"]
    if "notes" in data:
        video.notes = (data["notes"] or "").strip() or None

    db.session.commit()
    return jsonify({"ok": True})
