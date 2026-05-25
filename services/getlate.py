"""
services/getlate.py — Publishing via GetLate.dev
==================================================
Multi-platform social media publishing in one API call.
Students learn: this is the "output" stage — where content goes live.
"""

import os
import requests
from datetime import datetime, timezone

_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".avi", ".m4v", ".mkv"}

def _media_type(url: str) -> str:
    ext = os.path.splitext((url or "").split("?")[0].lower())[1]
    return "video" if ext in _VIDEO_EXTS else "image"

# ---------------------------------------------------------------------------
# API endpoint
# ---------------------------------------------------------------------------
GETLATE_BASE_URL = "https://getlate.dev/api/v1"


def _parse_scheduled_time(scheduled_at_str):
    """Parse a scheduled_at string into Zernio's expected format: YYYY-MM-DDTHH:MM:SS (no Z)."""
    formats = [
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(scheduled_at_str, fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            continue
    # Already in right format or can't parse — strip Z if present
    if isinstance(scheduled_at_str, str) and scheduled_at_str.endswith("Z"):
        return scheduled_at_str[:-1]
    return scheduled_at_str


def _get_headers():
    """Build auth headers for GetLate/Zernio API."""
    api_key = os.getenv("ZERNIO_API_KEY") or os.getenv("GETLATE_API_KEY")
    if not api_key:
        return None
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }


# ---------------------------------------------------------------------------
# publish_post() — Send content to connected social accounts
# ---------------------------------------------------------------------------
def publish_post(content_item, platforms=None, emit_event=None):
    """
    Publish a content item to social media via GetLate.dev.

    Args:
        content_item: dict from the database (must have script, image_url, etc.)
        platforms: list of platform names to publish to (defaults to item's platform)
        emit_event: Callback for SSE logging

    Returns:
        dict with: post_id, platforms_published, status
    """
    emit = emit_event or (lambda *a, **kw: None)
    headers = _get_headers()

    if not platforms:
        platforms = [content_item.get("platform", "instagram")]

    if not headers:
        emit("publish", "progress", "No GetLate API key set yet — simulating publish. To publish for real, get your key from https://getlate.dev and paste it in Settings > GetLate > API Key.")
        return {
            "post_id": "demo_post_id",
            "platforms_published": platforms,
            "status": "demo",
            "demo": True,
            "message": "Get your key from https://getlate.dev and add it in Settings."
        }

    emit("publish", "progress", f"Publishing to {', '.join(platforms)} via GetLate.dev...")

    try:
        # Fetch connected account IDs (Zernio requires accountId, not just platform name)
        accounts_resp = requests.get(f"{GETLATE_BASE_URL}/accounts", headers=headers, timeout=10)
        accounts_resp.raise_for_status()
        accounts_data = accounts_resp.json()
        all_accounts = accounts_data if isinstance(accounts_data, list) else accounts_data.get("accounts", [])
        account_map = {a["platform"]: a["_id"] for a in all_accounts if a.get("platform") and a.get("_id")}

        platform_entries = []
        for p in platforms:
            if p in account_map:
                platform_entries.append({"platform": p, "accountId": account_map[p]})
        if not platform_entries:
            return {"status": "error", "error": f"No connected accounts found for platforms: {platforms}. Connect them at zernio.com"}

        # Build the post payload
        payload = {
            "content": content_item.get("script", ""),
            "platforms": platform_entries,
        }

        # Zernio uses 'mediaItems' (not 'media') for attaching files
        # If voiced video exists, use ONLY that — never send two videos
        voiced = content_item.get("r2_video_url") or content_item.get("video_url")
        raw    = content_item.get("r2_image_url") or content_item.get("image_url")
        if voiced and "placehold" in voiced: voiced = None
        if raw    and "placehold" in raw:    raw    = None
        best = voiced or raw
        if best:
            payload["mediaItems"] = [{"url": best, "type": _media_type(best)}]

        # If there's a scheduled time, use scheduledFor (camelCase — required by Zernio)
        if content_item.get("scheduled_at"):
            parsed_time = _parse_scheduled_time(content_item["scheduled_at"])
            if parsed_time:
                payload["scheduledFor"] = parsed_time
                payload["timezone"] = "America/New_York"
        else:
            # Publish immediately
            payload["publishNow"] = True

        response = requests.post(
            f"{GETLATE_BASE_URL}/posts",
            headers=headers,
            json=payload,
            timeout=90  # Video processing can take 60-90s
        )
        if not response.ok:
            body = response.text[:400]
            err = f"GetLate {response.status_code}: {body}"
            emit("publish", "error", err)
            raise requests.exceptions.HTTPError(err, response=response)
        data = response.json()

        post_id = data.get("post", {}).get("_id") or data.get("id", data.get("post_id", "unknown"))

        emit("publish", "progress",
             f"Published! Post ID: {post_id}")

        return {
            "post_id": post_id,
            "platforms_published": platforms,
            "status": "published",
            "demo": False,
            "response": data
        }

    except requests.exceptions.RequestException as e:
        error_msg = str(e)
        emit("publish", "error", f"GetLate error: {error_msg}")
        raise


# ---------------------------------------------------------------------------
# publish_to_all_platforms() — One click → every connected platform
# ---------------------------------------------------------------------------
def publish_to_all_platforms(content_item, captions_dict=None, scheduled_at=None, emit_event=None, allowed_account_ids=None):
    """
    Publish to ALL connected social accounts in one shot.
    Makes a separate Zernio call per platform so each gets its own caption.

    Args:
        content_item:  dict with script, image_url, r2_image_url, video_url, r2_video_url
        captions_dict: dict of {platform: caption_text} — uses platform-specific copy
        scheduled_at:  ISO datetime string or None for immediate publish
        emit_event:    SSE callback

    Returns:
        dict with: results (list), platforms_published, platforms_failed, status
    """
    emit = emit_event or (lambda *a, **kw: None)
    headers = _get_headers()
    captions_dict = captions_dict or {}

    if not headers:
        emit("publish", "progress", "Demo mode — add your Zernio API key in Settings to publish for real.")
        return {
            "status": "demo",
            "platforms_published": ["tiktok", "instagram", "youtube", "facebook"],
            "platforms_failed": [],
            "demo": True,
        }

    # Fetch all connected accounts once
    try:
        accounts_resp = requests.get(f"{GETLATE_BASE_URL}/accounts", headers=headers, timeout=10)
        accounts_resp.raise_for_status()
        accounts_data = accounts_resp.json()
        all_accounts = accounts_data if isinstance(accounts_data, list) else accounts_data.get("accounts", [])
        account_map = {a["platform"]: a["_id"] for a in all_accounts if a.get("platform") and a.get("_id")}
    except requests.exceptions.RequestException as e:
        return {"status": "error", "error": f"Could not fetch connected accounts: {str(e)}"}

    if not account_map:
        return {"status": "error", "error": "No connected social accounts found. Connect them at zernio.com"}

    emit("publish", "progress", f"Found {len(account_map)} connected accounts: {', '.join(account_map.keys())}")

    # Build shared mediaItems payload (Zernio uses 'mediaItems', not 'media')
    # Filter out demo placeholder URLs (placehold.co) — never publish those
    voiced_url = content_item.get("r2_video_url") or content_item.get("video_url")
    raw_url    = content_item.get("r2_image_url") or content_item.get("image_url")
    if voiced_url and "placehold" in voiced_url: voiced_url = None
    if raw_url    and "placehold" in raw_url:    raw_url    = None

    # If a voiced/processed video exists, use ONLY that — never send two videos in one post
    # If no voiced video, fall back to the original raw media (image or video)
    best_url = voiced_url or raw_url
    media = [{"url": best_url, "type": _media_type(best_url)}] if best_url else []

    # Parse scheduled time once
    scheduled_for = None
    if scheduled_at:
        scheduled_for = _parse_scheduled_time(scheduled_at)

    results = []
    platforms_published = []
    platforms_failed = []

    for platform, account_id in account_map.items():
        # Skip if caller passed a specific allow-list of account IDs
        if allowed_account_ids and account_id not in allowed_account_ids:
            continue

        # Use platform-specific caption, fall back to script
        raw_caption = captions_dict.get(platform)

        # Handle nested structure e.g. {"DREAMER": "...", "OPERATOR": "..."}
        # Pick DREAMER first, then first available, then fall back to script
        if isinstance(raw_caption, dict):
            raw_caption = (raw_caption.get("DREAMER")
                           or raw_caption.get("dreamer")
                           or next(iter(raw_caption.values()), None))

        caption = raw_caption or content_item.get("script", "") or ""
        if not isinstance(caption, str):
            caption = ""
        # Only skip if there's nothing to post at all (no caption AND no media)
        if not caption and not best_url:
            continue

        payload = {
            "content": caption,
            "platforms": [{"platform": platform, "accountId": account_id}],
        }
        if media:
            payload["mediaItems"] = media
        if scheduled_for:
            # scheduledFor = camelCase, required by Zernio API
            payload["scheduledFor"] = scheduled_for
            payload["timezone"] = "America/New_York"
        else:
            # Publish immediately
            payload["publishNow"] = True

        try:
            resp = requests.post(f"{GETLATE_BASE_URL}/posts", headers=headers, json=payload, timeout=90)
            # Log raw response for debugging before raising
            raw_body = resp.text[:300]
            if not resp.ok:
                emit("publish", "progress",
                     f"{platform.capitalize()} — HTTP {resp.status_code}: {raw_body}")
                platforms_failed.append(platform)
                results.append({"platform": platform, "status": "error",
                                 "error": f"HTTP {resp.status_code}: {raw_body}"})
                continue
            try:
                data = resp.json()
            except Exception:
                data = {}
            post_id = (data.get("post", {}).get("_id")
                       or data.get("id") or data.get("post_id", ""))
            post_status = (data.get("post", {}).get("status")
                           or data.get("status", ""))
            emit("publish", "progress", f"{platform.capitalize()} — sent! Post ID: {post_id or '(unknown)'}")
            platforms_published.append(platform)
            results.append({
                "platform": platform,
                "post_id": post_id,
                "zernio_status": post_status,
                "status": "published",
                "raw": raw_body,
            })
        except Exception as e:
            emit("publish", "progress", f"{platform.capitalize()} — error: {str(e)[:80]}")
            platforms_failed.append(platform)
            results.append({"platform": platform, "status": "error", "error": str(e)[:120]})

    overall = "published" if platforms_published else "error"
    emit("publish", "progress",
         f"Done! Published to: {', '.join(platforms_published) or 'none'}."
         + (f" Failed: {', '.join(platforms_failed)}" if platforms_failed else ""))

    return {
        "status": overall,
        "platforms_published": platforms_published,
        "platforms_failed": platforms_failed,
        "results": results,
    }


# ---------------------------------------------------------------------------
# get_connected_accounts() — List connected social accounts
# ---------------------------------------------------------------------------
def get_connected_accounts(emit_event=None):
    """
    Fetch the list of connected social media accounts from GetLate.dev.

    Returns:
        list of account dicts with: id, platform, username, status
    """
    emit = emit_event or (lambda *a, **kw: None)
    headers = _get_headers()

    if not headers:
        # Return demo accounts so the UI has something to show
        return [
            {"id": "demo_1", "platform": "instagram", "username": "@demo_user", "status": "demo"},
            {"id": "demo_2", "platform": "tiktok", "username": "@demo_user", "status": "demo"},
            {"id": "demo_3", "platform": "linkedin", "username": "Demo User", "status": "demo"},
        ]

    try:
        response = requests.get(
            f"{GETLATE_BASE_URL}/accounts",
            headers=headers,
            timeout=15
        )
        response.raise_for_status()
        return response.json().get("accounts", [])

    except requests.exceptions.RequestException as e:
        emit("publish", "error", f"Failed to fetch connected accounts: {str(e)}")
        return []
