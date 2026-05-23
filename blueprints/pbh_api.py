"""blueprints/pbh_api.py — Party Biz Hub Content Engine (JSON API)"""

import json
import mimetypes
import os
import queue
import threading
import uuid
from datetime import datetime

from flask import Blueprint, request, jsonify, Response, current_app
from auth import login_required
from models import ContentItem, PBHAsset, PipelineLog
from extensions import db

pbh_api_bp = Blueprint("pbh_api", __name__)

# ── Party Biz Hub feature knowledge base ────────────────────────────────────

PBH_KNOWLEDGE = """
Party Biz Hub is a business management app built specifically for kids party businesses.

FEATURES:
- Quote Builder: Create a professional, itemized quote in under 60 seconds. Add packages, add-ons, travel fees, and deposits. Client sees a polished quote page — not a text message.
- Online Booking Page: A personalized link clients use to book and pay directly. No more back-and-forth texts. Deposit collected automatically at booking.
- Contract Templates: Legal contracts auto-filled with the client's event details. Sent digitally, signed online. No printer needed.
- AI Content Machine: One click generates TikTok, Instagram, Facebook, and YouTube posts about the business. No writing skills needed.
- Client Dashboard: Every booking, payment, contract, and conversation for every client in one place. Never lose track of an event again.
- Invoice & Payment: Send professional invoices and collect payment online. Stripe-powered.
- Custom Branding: Add a logo, choose colors, add a business name. The app looks like YOUR software, not a generic tool.
- Mobile Friendly: Works on phone, tablet, and desktop. Manage the entire business from anywhere.
- Automated Reminders: Auto-sends payment reminders and event confirmations so nothing falls through the cracks.
- Back Office View: See all upcoming events, outstanding invoices, and recent bookings on one dashboard — the command center for the business.

TARGET USER: Someone already running a kids party business (face painter, balloon artist, bounce house rental, princess entertainer, party planner) who is losing time to manual admin work.
"""

# ── Content type prompts ────────────────────────────────────────────────────

PBH_PROMPTS = {
    "app_demo": (
        "Create a {platform} video script showing Party Biz Hub in action. "
        "Use the Party Biz Hub feature details provided above — do NOT invent features. "
        "Hook: start with ONE specific pain point (chasing unpaid invoices, losing bookings, spending hours on admin, looking unprofessional). "
        "Demo moment: describe exactly what the relevant feature does visually — 2-3 concrete sentences. "
        "CTA: 'Link in bio — start free at partybizhub.com'. "
        "Keep it under 45 seconds when read aloud. Faceless video — no 'I' or personal pronouns."
    ),
    "party_tip": (
        "Create a {platform} post with a party business tip that naturally leads to Party Biz Hub as the solution. "
        "Target audience: someone already running a kids party business who wants better systems and more bookings. "
        "Format: Hook (pain/curiosity) → 2-3 actionable tip lines → soft sell of Party Biz Hub as the tool that handles it. "
        "CTA: 'Get Party Biz Hub free — partybizhub.com'. "
        "Voice: direct, smart, no fluff. Like advice from a successful party biz owner."
    ),
    "hook_post": (
        "Create a bold text-only hook post for {platform} about one of these topics (pick the strongest): "
        "pricing your party services correctly, getting more bookings, looking professional vs amateur, "
        "contracts protecting you, or why party businesses fail in year one. "
        "Format: 1 devastating hook line → 3-5 punchy lines that twist the knife → 1 line CTA mentioning Party Biz Hub. "
        "No emojis. No hashtags in body. Ultra punchy. Under 150 words total."
    ),
    "testimonial": (
        "Write a {platform} post in the voice of a satisfied Party Biz Hub user (fictional but realistic). "
        "They run a kids party business (balloon artist, face painter, bounce house rental, or princess entertainer). "
        "Story arc: before Party Biz Hub (chaos, missed bookings, manual invoices) → after (professional, automated, earning more). "
        "Include a specific dollar amount or time saved. End with social proof CTA: 'See why 100+ party pros use Party Biz Hub — partybizhub.com'. "
        "Warm, real-talk voice. First person."
    ),
    "comparison": (
        "Create a {platform} post comparing running a party business WITHOUT Party Biz Hub vs WITH it. "
        "Use a simple before/after or side-by-side format. "
        "Focus on 3 specific pain points: booking chaos, unprofessional quotes, no contract protection. "
        "End with: 'Party Biz Hub fixes all three — starting at $27/month'. "
        "CTA: 'Try it free — partybizhub.com'. Tone: confident, matter-of-fact, no hype."
    ),
}


def _build_input_text(content_type, platform, custom_angle=""):
    template = PBH_PROMPTS.get(content_type, PBH_PROMPTS["party_tip"])
    base = template.format(platform=platform.capitalize())

    # App demo always gets the knowledge base so the AI has real feature details
    knowledge_block = f"\n\nAPP KNOWLEDGE BASE (use this as your source of truth):\n{PBH_KNOWLEDGE}" if content_type == "app_demo" else ""

    if custom_angle:
        return (
            f"CONTENT SUBJECT — what this post must be ABOUT (do not deviate from this):\n"
            f"{custom_angle}\n\n"
            f"IMPORTANT RULES:\n"
            f"1. Every sentence must be directly about the SUBJECT above.\n"
            f"2. If the subject mentions a writing STYLE or TECHNIQUE (e.g. 'use buyer psychology', "
            f"'use storytelling') — apply that technique to write ABOUT the subject. Do NOT write about the technique itself.\n"
            f"3. Do not switch to a different topic (e.g. pricing, undercharging, general tips) "
            f"unless the subject explicitly states that topic.\n"
            f"4. Follow the format structure below.\n\n"
            f"FORMAT GUIDE ({platform.capitalize()} / {content_type.replace('_', ' ')}):\n{base}"
            f"{knowledge_block}"
        )

    return base + knowledge_block


# ── Generate (SSE stream) ────────────────────────────────────────────────────

@pbh_api_bp.route("/generate", methods=["POST"])
@login_required
def generate():
    data = request.get_json() or {}

    content_type = data.get("content_type", "party_tip")
    platform     = data.get("platform", "tiktok")
    custom_angle = data.get("custom_angle", "")
    asset_id     = data.get("asset_id")

    input_text = _build_input_text(content_type, platform, custom_angle)

    item = ContentItem(
        brand=       "pbh",
        input_text=  input_text,
        input_type=  "idea",
        platform=    platform,
        include_video= False,
        status=      "draft",
    )
    db.session.add(item)
    db.session.commit()

    # Attach asset URL as reference image if one was chosen
    if asset_id:
        asset = PBHAsset.query.get(asset_id)
        if asset:
            item.image_url    = asset.r2_url
            item.r2_image_url = asset.r2_url
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


# ── Batch generate ───────────────────────────────────────────────────────────

@pbh_api_bp.route("/batch-generate", methods=["POST"])
@login_required
def batch_generate():
    """Generate multiple PBH content items in the background (no SSE)."""
    data  = request.get_json() or {}
    count = min(int(data.get("count", 3)), 7)
    platforms = data.get("platforms", ["tiktok", "instagram", "facebook", "youtube"])

    # Rotate through content types for variety
    types_rotation = ["party_tip", "hook_post", "app_demo", "testimonial", "comparison",
                      "party_tip", "hook_post"]
    created_ids = []
    app = current_app._get_current_object()

    for i in range(count):
        ctype    = types_rotation[i % len(types_rotation)]
        platform = platforms[i % len(platforms)]
        input_text = _build_input_text(ctype, platform)

        item = ContentItem(
            brand=      "pbh",
            input_text= input_text,
            input_type= "idea",
            platform=   platform,
            include_video= False,
            status=     "draft",
        )
        db.session.add(item)
        db.session.commit()
        created_ids.append(item.id)

        def run(cid=item.id):
            def noop(*a, **kw): pass
            with app.app_context():
                from pipeline import run_pipeline
                run_pipeline(cid, noop)

        threading.Thread(target=run, daemon=True).start()

    return jsonify({"ok": True, "created": created_ids, "count": len(created_ids)})


# ── Items list ───────────────────────────────────────────────────────────────

@pbh_api_bp.route("/items", methods=["GET"])
@login_required
def list_items():
    status_filter = request.args.get("status")
    q = ContentItem.query.filter_by(brand="pbh").order_by(ContentItem.created_at.desc())
    if status_filter:
        q = q.filter_by(status=status_filter)
    return jsonify([i.to_dict() for i in q.all()])


@pbh_api_bp.route("/items/<int:item_id>", methods=["DELETE"])
@login_required
def delete_item(item_id):
    item = ContentItem.query.filter_by(id=item_id, brand="pbh").first_or_404()
    db.session.delete(item)
    db.session.commit()
    return jsonify({"ok": True})


@pbh_api_bp.route("/items/clear", methods=["POST"])
@login_required
def clear_items():
    deleted = ContentItem.query.filter_by(brand="pbh").delete()
    db.session.commit()
    return jsonify({"ok": True, "deleted": deleted})


# ── Publish (reuse existing logic via content_api) ───────────────────────────

@pbh_api_bp.route("/items/<int:item_id>/approve", methods=["POST"])
@login_required
def approve(item_id):
    item = ContentItem.query.filter_by(id=item_id, brand="pbh").first_or_404()
    item.status = "approved"
    db.session.commit()
    return jsonify({"ok": True})


@pbh_api_bp.route("/items/<int:item_id>/publish", methods=["POST"])
@login_required
def publish(item_id):
    item = ContentItem.query.filter_by(id=item_id, brand="pbh").first_or_404()
    from services.getlate import publish_post

    content_item = {
        "script":      item.script or "",
        "platform":    item.platform or "tiktok",
        "image_url":   item.image_url or "",
        "r2_image_url": item.r2_image_url or "",
        "video_url":   item.video_url or "",
        "r2_video_url": item.r2_video_url or "",
        "scheduled_at": getattr(item, "scheduled_at", None),
    }

    # Use the platform-specific caption — always resolve to a clean string
    if item.captions:
        try:
            captions_dict = json.loads(item.captions)
            raw = captions_dict.get(item.platform) or captions_dict.get("default") or item.script or ""
            # Handle nested dict (e.g. {"DREAMER": "...", "OPERATOR": "..."})
            if isinstance(raw, dict):
                raw = raw.get("DREAMER") or raw.get("dreamer") or next(iter(raw.values()), "")
            content_item["script"] = str(raw).strip() if raw else (item.script or "")
        except (json.JSONDecodeError, AttributeError):
            pass

    # GetLate rejects empty content — fall back to script if caption resolved to nothing
    if not content_item["script"]:
        content_item["script"] = item.script or ""

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

    try:
        result = publish_post(
            content_item=content_item,
            platforms=[item.platform] if item.platform else None,
        )
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 200

    if not scheduled_at_str and result.get("status") != "error":
        item.status = "published"
        item.published_at = datetime.utcnow()
    db.session.commit()

    return jsonify(result)


# ── Assets ───────────────────────────────────────────────────────────────────

@pbh_api_bp.route("/assets", methods=["GET"])
@login_required
def list_assets():
    return jsonify([a.to_dict() for a in PBHAsset.query.order_by(PBHAsset.created_at.desc()).all()])


@pbh_api_bp.route("/assets/upload", methods=["POST"])
@login_required
def upload_asset():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "No file selected"}), 400

    from services.r2_storage import is_configured, _get_client, _get_public_url

    if not is_configured():
        return jsonify({"error": "R2 storage not configured"}), 500

    file_data    = file.read()
    filename     = file.filename
    content_type = file.content_type or mimetypes.guess_type(filename)[0] or "image/jpeg"
    ext          = os.path.splitext(filename)[1].lower() or ".jpg"
    is_video     = content_type.startswith("video/") or ext in (".mp4", ".mov", ".webm", ".avi")

    # MOV → MP4 for browser compatibility
    if ext == ".mov" or content_type == "video/quicktime":
        content_type = "video/mp4"
        ext = ".mp4"

    key = f"pbh-assets/{uuid.uuid4().hex}{ext}"

    client = _get_client()
    bucket = os.environ["R2_BUCKET_NAME"]
    client.put_object(Bucket=bucket, Key=key, Body=file_data, ContentType=content_type)
    url = _get_public_url(key)

    tag   = request.form.get("tag", "other")
    notes = request.form.get("notes", "")

    asset = PBHAsset(filename=filename, r2_url=url, r2_key=key, tag=tag, notes=notes,
                     file_type="video" if is_video else "image")
    db.session.add(asset)
    db.session.commit()

    return jsonify(asset.to_dict())


@pbh_api_bp.route("/assets/<int:asset_id>/thumb")
@login_required
def asset_thumb(asset_id):
    """Proxy the asset image through Flask so R2 access issues don't break the picker."""
    import requests as req
    from flask import Response as FlaskResponse
    asset = PBHAsset.query.get_or_404(asset_id)
    if not asset.r2_url:
        return "Not found", 404
    try:
        r = req.get(asset.r2_url, timeout=10, stream=True)
        content_type = r.headers.get("Content-Type", "image/jpeg")
        return FlaskResponse(r.iter_content(8192), content_type=content_type,
                             headers={"Cache-Control": "public, max-age=3600"})
    except Exception:
        return "Could not load asset", 502


@pbh_api_bp.route("/assets/<int:asset_id>", methods=["DELETE"])
@login_required
def delete_asset(asset_id):
    asset = PBHAsset.query.get_or_404(asset_id)
    db.session.delete(asset)
    db.session.commit()
    return jsonify({"ok": True})
