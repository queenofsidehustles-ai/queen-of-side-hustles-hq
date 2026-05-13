"""
services/clip_generator.py — AI Clip Generator
================================================
Picks a video from the library, trims a highlight, writes a hook script,
adds ElevenLabs voiceover (if configured), uploads to R2, and creates a
ContentItem ready for approval and publishing.

FFmpeg operations used here are intentionally simple:
  1. Trim  — -ss / -t with libx264 re-encode (handles any phone format)
  2. Merge — replace audio track with voiceover using -map

No filter_complex, no overlay compositing — just the reliable basics.
"""

import logging
import os
import random
import subprocess
import tempfile
import uuid

import requests

logger = logging.getLogger(__name__)

CLIP_DURATION = 30      # max seconds per clip (uses full video if shorter)
MIN_VIDEO_SEC = 5       # skip videos shorter than this


# ---------------------------------------------------------------------------
# Script generation
# ---------------------------------------------------------------------------

def _generate_script(tag_label: str, notes: str = "") -> str:
    """Ask Gemini (via OpenRouter) to write a 60-word voiceover hook."""
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        return (
            f"Watch what's possible when you build a kids party business the right way. "
            f"This is real footage from one of my parties — and this could be your life too. "
            f"Follow for daily tips on building your party business from scratch."
        )

    context = f"Video category: {tag_label}."
    if notes:
        context += f" Additional context: {notes}."

    prompt = f"""You are writing a voiceover script for Coach Monica Lewis (@kidspartybizcoach).
Monica helps moms start profitable kids party businesses.

{context}

Write a 55-65 word voiceover script that:
- Opens with a scroll-stopping hook (curiosity, pain point, or surprising result)
- Briefly teases what the viewer is seeing in the video
- Ends with ONE clear call to action (follow, save, DM "PARTY")
- Sounds natural when spoken aloud — no bullet points, no hashtags
- Speaks directly to moms who want to start a party business

Return ONLY the script text, nothing else."""

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "google/gemini-2.5-flash",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150,
                "temperature": 0.8,
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning("Script generation failed: %s — using fallback", e)
        return (
            f"This is what a successful kids party business actually looks like. "
            f"Real parties, real income, real freedom. "
            f"If you've been thinking about starting — this is your sign. "
            f"Follow for daily tips on building your party business."
        )


# ---------------------------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------------------------

def _trim_clip(input_path: str, output_path: str, start: float, duration: float) -> tuple[bool, str]:
    """
    Trim video to [start, start+duration] using stream copy — no re-encode.
    This avoids OOM on large iPhone MOV/HEVC files (500MB+).
    The merge step does the H.264 re-encode on the short 25s clip.
    Returns (success, error_message).
    """
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", input_path,
        "-t", str(duration),
        "-c", "copy",
        "-movflags", "+faststart",
        output_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            last_lines = r.stderr.strip().splitlines()
            msg = " | ".join(last_lines[-3:]) if last_lines else r.stderr[-300:]
            logger.error("FFmpeg trim failed (rc=%d): %s", r.returncode, msg)
            return False, f"FFmpeg rc={r.returncode}: {msg}"
        return True, ""
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg trim timed out")
        return False, "FFmpeg timed out (>120s)"
    except FileNotFoundError:
        logger.error("FFmpeg not found — is it installed?")
        return False, "FFmpeg not installed on server"
    except Exception as e:
        logger.exception("FFmpeg trim error: %s", e)
        return False, str(e)


def _normalize_clip(input_path: str, output_path: str) -> bool:
    """Re-encode a short clip to H.264/AAC for platform compatibility."""
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            logger.warning("FFmpeg normalize failed (rc=%d): %s", r.returncode, r.stderr[-200:])
        return r.returncode == 0
    except Exception as e:
        logger.warning("FFmpeg normalize error: %s", e)
        return False


def _merge_voiceover(video_path: str, audio_path: str, output_path: str) -> bool:
    """Replace video's audio track with voiceover. Stops at whichever is shorter."""
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-shortest",
        "-movflags", "+faststart",
        output_path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            logger.error("FFmpeg merge failed (rc=%d): %s", r.returncode, r.stderr[-400:])
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg merge timed out")
        return False
    except Exception as e:
        logger.exception("FFmpeg merge error: %s", e)
        return False


# ---------------------------------------------------------------------------
# R2 upload
# ---------------------------------------------------------------------------

def _upload_to_r2(file_path: str) -> str | None:
    """Upload a processed clip to R2. Returns public URL or None."""
    required = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"]
    if not all(os.getenv(v) for v in required):
        logger.warning("R2 not configured — cannot upload clip")
        return None

    try:
        import boto3
        client = boto3.client(
            "s3",
            endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
        key = f"clips/{uuid.uuid4().hex}.mp4"
        with open(file_path, "rb") as f:
            client.put_object(
                Bucket=os.environ["R2_BUCKET_NAME"],
                Key=key,
                Body=f,
                ContentType="video/mp4",
            )
        public_url = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")
        return f"{public_url}/{key}"
    except Exception as e:
        logger.exception("R2 clip upload failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_clip(video) -> dict:
    """
    Generate one AI clip from a LibraryVideo.

    Returns:
        {"ok": True, "content_item_id": int, "script": str}   on success
        {"ok": False, "error": str}                            on failure
    """
    from extensions import db
    from models import ContentItem

    duration = video.duration or 0.0
    if duration < MIN_VIDEO_SEC:
        return {"ok": False, "error": f"Video too short ({duration:.0f}s) — need at least {MIN_VIDEO_SEC}s"}

    clip_dur  = min(CLIP_DURATION, duration)
    max_start = max(0.0, duration - clip_dur)
    start     = round(random.uniform(0, max_start), 1)

    tmp_files = []

    def _tmp(suffix):
        f = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        f.close()
        tmp_files.append(f.name)
        return f.name

    try:
        # 1. Download source video from R2
        logger.info("Downloading library video: %s", video.r2_url)
        raw_path = _tmp(".mp4")
        r = requests.get(video.r2_url, stream=True, timeout=120)
        r.raise_for_status()
        with open(raw_path, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
        file_size = os.path.getsize(raw_path)
        logger.info("Downloaded %d bytes to %s", file_size, raw_path)
        if file_size < 1024:
            return {"ok": False, "error": f"Download too small ({file_size} bytes) — R2 URL may be invalid or private"}

        # 2. Trim clip
        trimmed_path = _tmp("_trim.mp4")
        logger.info("Trimming clip: start=%.1fs duration=%.1fs", start, clip_dur)
        ok, trim_err = _trim_clip(raw_path, trimmed_path, start, clip_dur)
        if not ok:
            return {"ok": False, "error": trim_err}

        # 3. Generate voiceover script
        script = _generate_script(video.tag_label, notes=video.notes or "")
        logger.info("Script (%d chars): %s…", len(script), script[:60])

        # 4. ElevenLabs TTS
        from services.elevenlabs import synthesize
        audio_bytes = synthesize(script)

        final_path = _tmp("_final.mp4")
        if audio_bytes:
            audio_path = _tmp(".mp3")
            with open(audio_path, "wb") as f:
                f.write(audio_bytes)
            logger.info("Merging voiceover (%d bytes)", len(audio_bytes))
            if not _merge_voiceover(trimmed_path, audio_path, final_path):
                logger.warning("Voiceover merge failed — normalizing without voiceover")
                if not _normalize_clip(trimmed_path, final_path):
                    import shutil
                    shutil.copy2(trimmed_path, final_path)
        else:
            # No voiceover — normalize to H.264 so clip is platform-compatible
            logger.info("No voiceover — normalizing clip to H.264")
            if not _normalize_clip(trimmed_path, final_path):
                import shutil
                shutil.copy2(trimmed_path, final_path)

        # 5. Upload to R2
        logger.info("Uploading clip to R2")
        clip_url = _upload_to_r2(final_path)
        if not clip_url:
            return {"ok": False, "error": "R2 upload failed — check R2 settings"}

        # 6. Create ContentItem in the approval queue
        item = ContentItem(
            input_text=f"Library clip — {video.tag_label}: {video.filename}",
            input_type="library",
            platform="tiktok",
            script=script,
            video_url=clip_url,
            r2_video_url=clip_url,
            include_video=True,
            status="ReadyToPost",
        )
        db.session.add(item)

        # 7. Mark video as used
        video.used_count = (video.used_count or 0) + 1

        db.session.commit()
        logger.info("Clip created: ContentItem #%d", item.id)
        return {"ok": True, "content_item_id": item.id, "script": script}

    except Exception as e:
        logger.exception("generate_clip failed")
        db.session.rollback()
        return {"ok": False, "error": str(e)}

    finally:
        for path in tmp_files:
            try:
                os.unlink(path)
            except OSError:
                pass


def generate_clips(count: int = 3) -> list[dict]:
    """
    Generate `count` AI clips from the library, picking varied tags.
    Returns list of result dicts from generate_clip().
    """
    from models import LibraryVideo

    all_videos = LibraryVideo.query.filter(
        LibraryVideo.duration >= MIN_VIDEO_SEC
    ).all()

    if not all_videos:
        return [{"ok": False, "error": "No videos in library yet — upload some first"}]

    # Prefer variety: sort by used_count ascending so least-used videos go first
    all_videos.sort(key=lambda v: (v.used_count or 0))

    # Pick up to `count` distinct videos (different tags where possible)
    chosen = []
    seen_tags = set()
    for v in all_videos:
        if len(chosen) >= count:
            break
        if v.tag not in seen_tags:
            chosen.append(v)
            seen_tags.add(v.tag)

    # Top up with any remaining if we couldn't get enough variety
    for v in all_videos:
        if len(chosen) >= count:
            break
        if v not in chosen:
            chosen.append(v)

    results = []
    for video in chosen:
        logger.info("Generating clip from: %s (%s)", video.filename, video.tag_label)
        results.append(generate_clip(video))

    return results
