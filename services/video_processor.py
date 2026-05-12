"""
services/video_processor.py — AI text overlay burning via FFmpeg
================================================================
Downloads a raw video from R2, burns on-screen text overlays
(hook + content points + CTA) using FFmpeg, then re-uploads the
processed video to R2.

Brand style: white bold text, hot-pink stroke, centered bottom third.
"""

import json
import logging
import os
import re
import subprocess
import tempfile
import uuid

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Brand styling
# ---------------------------------------------------------------------------
_FONT_COLOR = "white"
_STROKE_COLOR = "0xEC4899"   # Monica's brand pink
_STROKE_WIDTH = 3
_TEXT_Y_TOP = "h*0.08"       # hook position — top of frame (like her TikTok style)
_TEXT_Y_BOTTOM = "h*0.72"    # key points + CTA — bottom third
_MAX_CHARS = 26              # max chars per overlay line (short = readable)

_RESOLVED_FONT = ""          # cached font path, resolved once at first use

# Bundled font committed to the repo — most reliable option
_BUNDLED_FONT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "fonts", "Arial-Bold.ttf"
)


def _resolve_font() -> str:
    """Return an absolute path to a usable TTF font for FFmpeg drawtext."""
    global _RESOLVED_FONT
    if _RESOLVED_FONT:
        return _RESOLVED_FONT

    # 1. Bundled font (most reliable — committed to repo)
    if os.path.isfile(_BUNDLED_FONT):
        _RESOLVED_FONT = _BUNDLED_FONT
        logger.info("FFmpeg font (bundled): %s", _RESOLVED_FONT)
        return _RESOLVED_FONT

    # 2. fc-match fallback (works when fontconfig + liberation_ttf are installed)
    try:
        result = subprocess.run(
            ["fc-match", "--format=%{file}", "sans:bold"],
            capture_output=True, text=True, timeout=5,
        )
        path = result.stdout.strip()
        if path and os.path.isfile(path):
            _RESOLVED_FONT = path
            logger.info("FFmpeg font (fc-match): %s", _RESOLVED_FONT)
            return _RESOLVED_FONT
    except Exception:
        pass

    # 3. Search nix store directly
    try:
        result = subprocess.run(
            ["find", "/nix/store", "-name", "LiberationSans-Bold.ttf", "-type", "f"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if line and os.path.isfile(line):
                _RESOLVED_FONT = line
                logger.info("FFmpeg font (nix store): %s", _RESOLVED_FONT)
                return _RESOLVED_FONT
    except Exception:
        pass

    logger.warning("No font file found — FFmpeg drawtext text may not render")
    return ""


# ---------------------------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------------------------
def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _sanitize(text: str) -> str:
    """Strip characters that break FFmpeg's drawtext filter parser."""
    text = text.replace("\\", " ")
    text = text.replace("'", "")
    text = text.replace(":", "-")
    text = text.replace("[", "(")
    text = text.replace("]", ")")
    text = text.replace(",", " ")
    text = text.replace("=", " ")
    text = text.replace("%", "")
    text = text.replace("{", "(")
    text = text.replace("}", ")")
    text = text.replace("\n", " ")
    text = text.replace("\r", " ")
    return text.strip()[:_MAX_CHARS * 2]   # hard cap


def _build_filter(overlays: list) -> str:
    """
    Build a comma-joined FFmpeg drawtext filtergraph.
    Each overlay dict: {text, start, end, fontsize, position (optional: "top"|"bottom")}
    Hook overlays use "top" position; key points and CTA use "bottom".
    """
    font = _resolve_font()
    fontfile_clause = f"fontfile='{font}':" if font else ""

    parts = []
    for ov in overlays:
        text = _sanitize(ov.get("text", ""))
        if not text:
            continue
        start = float(ov.get("start", 0))
        end = float(ov.get("end", start + 3))
        fontsize = int(ov.get("fontsize", 50))
        y_pos = _TEXT_Y_TOP if ov.get("position") == "top" else _TEXT_Y_BOTTOM

        f = (
            f"drawtext="
            f"{fontfile_clause}"
            f"text='{text}':"
            f"fontsize={fontsize}:"
            f"fontcolor={_FONT_COLOR}:"
            f"borderw={_STROKE_WIDTH}:"
            f"bordercolor={_STROKE_COLOR}:"
            f"x=(w-text_w)/2:"
            f"y={y_pos}:"
            f"enable='between(t,{start},{end})'"
        )
        parts.append(f)

    return ",".join(parts) if parts else "null"


def burn_overlays(input_path: str, output_path: str, overlays: list) -> tuple:
    """
    Burn text overlays onto a video using FFmpeg drawtext.
    Returns (success: bool, error_msg: str).
    """
    if not overlays:
        import shutil
        shutil.copy2(input_path, output_path)
        return True, ""

    vf = _build_filter(overlays)
    logger.info("FFmpeg filter: %s", vf[:400])

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "copy",   # copy audio as-is — avoids encoding failure if no audio track
        output_path,
    ]
    logger.info("FFmpeg cmd: %s", " ".join(cmd))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            # Return the last 800 chars of stderr as the error message
            err = result.stderr[-800:].strip()
            logger.error("FFmpeg overlay error (rc=%d):\n%s", result.returncode, err)
            return False, err
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "FFmpeg timed out after 300s"
    except FileNotFoundError:
        return False, "ffmpeg binary not found"


# ---------------------------------------------------------------------------
# AI overlay plan generation
# ---------------------------------------------------------------------------
def generate_overlay_plan(transcript: str, duration: float,
                          script: str = "", emit_event=None) -> dict:
    """
    Ask Gemini (via OpenRouter) to create a timed overlay plan from the transcript.

    Returns dict: {overlays: [{text, start, end, fontsize}], demo: bool}
    """
    emit = emit_event or (lambda *a, **kw: None)
    emit("overlay", "progress", "AI is reading your transcript and planning on-screen text...")

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        emit("overlay", "warning", "No OpenRouter key — using demo overlays.")
        cta_start = max(0.0, duration - 4.0)
        return {
            "overlays": [
                {"text": "Watch this tip", "start": 0, "end": 3, "fontsize": 56, "position": "top"},
                {"text": "Follow for more party tips", "start": cta_start, "end": duration, "fontsize": 52, "position": "bottom"},
            ],
            "demo": True,
        }

    cta_start = round(max(0.0, duration - 4.5), 1)

    prompt = f"""You are a biopsychology-driven content strategist for Monica Lewis — Kids Party Business Coach (@kidspartybizcoach).

Monica helps moms launch profitable kids party businesses. Two audiences:
• DREAMER: Wants to start from scratch, scared of pricing/clients/credibility
• OPERATOR: Already running events, wants better systems and higher income

VIDEO TRANSCRIPT ({duration:.0f}s):
\"\"\"{transcript[:2000]}\"\"\"

POST CONTEXT:
\"\"\"{script[:400]}\"\"\"

MISSION: Design on-screen text overlays that STOP THE SCROLL and build toward a conversion.

━━ HOOK (position: top, 0–3.5s) ━━
The most critical overlay. Must trigger a biological response in under 1 second.
Use ONE of these proven scroll-stopping formulas:
  • CURIOSITY GAP — withhold the answer: "Why your price is wrong"
  • PAIN-FIRST — name their exact struggle: "Zero bookings this weekend"
  • SPECIFICITY SHOCK — real number, real result: "One party made me $800"
  • IDENTITY CALL-OUT — speak to who they are: "Every party mom needs this"
  • COUNTER-INTUITIVE — flip their assumption: "Stop discounting your parties"
  • BEFORE/AFTER — compressed transformation: "Broke mom to party CEO"

Biopsychology rules for the hook:
- Dopamine anticipation: create a gap — brain MUST close it by watching more
- Amygdala activation: use words tied to fear, desire, money, identity, transformation
- Zeigarnik effect: leave the thought unfinished so they cannot scroll away
- First 3 words carry 80% of the weight — front-load the punch
- NO generic words: "amazing", "great", "tips", "how to" — these kill the scroll-stop

━━ CONTENT OVERLAYS (position: bottom) ━━
2–3 overlays timed to key moments in the transcript.
Each overlay = the single most powerful phrase from that moment.
Think: what would stop someone mid-scroll if they landed at that exact second?

━━ CTA (position: bottom, last 4s) ━━
One action. Warm, community feel. Example: "Follow for party biz tips" or "Save this for later"

RULES FOR ALL OVERLAYS:
- MAX 6 words — every word must earn its place
- NO apostrophes, colons, brackets, or equals signs
- All caps is OK for 2-3 word punchy overlays only

Return ONLY valid JSON (no markdown, no explanation):
{{
  "overlays": [
    {{"text": "scroll-stopping hook here", "start": 0, "end": 3.5, "fontsize": 56, "position": "top"}},
    {{"text": "key moment 1", "start": 8.0, "end": 11.0, "fontsize": 48, "position": "bottom"}},
    {{"text": "key moment 2", "start": 15.0, "end": 18.0, "fontsize": 48, "position": "bottom"}},
    {{"text": "Follow for party biz tips", "start": {cta_start}, "end": {round(duration, 1)}, "fontsize": 52, "position": "bottom"}}
  ]
}}"""

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://command-center.app",
            },
            json={
                "model": "google/gemini-2.5-flash",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 600,
                "temperature": 0.7,
            },
            timeout=30,
        )

        if not response.ok:
            logger.error("OpenRouter overlay plan error: %s", response.text[:200])
            return {"overlays": [], "demo": False, "error": "AI generation failed"}

        raw = response.json()["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```\s*$", "", raw, flags=re.MULTILINE)

        plan = json.loads(raw)
        overlays = plan.get("overlays", [])
        emit("overlay", "progress", f"Got overlay plan — {len(overlays)} text segments.")
        return {"overlays": overlays, "demo": False}

    except json.JSONDecodeError as e:
        logger.error("Overlay plan JSON parse error: %s", e)
        return {"overlays": [], "demo": False, "error": f"JSON parse error: {e}"}
    except Exception as e:
        logger.exception("Overlay plan generation failed")
        return {"overlays": [], "demo": False, "error": str(e)}


# ---------------------------------------------------------------------------
# R2 upload helper (from local file, not URL)
# ---------------------------------------------------------------------------
def upload_processed_video(file_path: str, emit_event=None):
    """
    Upload a locally processed video file to Cloudflare R2.
    Returns the public URL on success, None if R2 not configured.
    """
    emit = emit_event or (lambda *a, **kw: None)

    required = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"]
    if not all(os.getenv(v) for v in required):
        emit("overlay", "warning", "R2 not configured — processed video won't be saved to cloud.")
        return None

    try:
        import boto3

        account_id = os.environ["R2_ACCOUNT_ID"]
        client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
            config=boto3.session.Config(signature_version="s3v4"),
        )
        bucket = os.environ["R2_BUCKET_NAME"]
        key = f"videos/processed_{uuid.uuid4().hex}.mp4"

        emit("overlay", "progress", "Uploading processed video to R2...")
        with open(file_path, "rb") as f:
            client.put_object(Bucket=bucket, Key=key, Body=f, ContentType="video/mp4")

        public_url = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")
        url = f"{public_url}/{key}"
        emit("overlay", "progress", f"Processed video saved: {url}")
        return url

    except Exception as e:
        logger.exception("R2 upload of processed video failed")
        emit("overlay", "warning", f"R2 upload failed: {e}")
        return None


# ---------------------------------------------------------------------------
# process_video() — the main entry point called from pipeline.py
# ---------------------------------------------------------------------------
def process_video(raw_video_url: str, transcript: str, duration: float,
                  script: str = "", emit_event=None) -> dict:
    """
    Full pipeline: download → generate overlay plan → burn overlays → upload.

    Args:
        raw_video_url: R2 public URL of the raw uploaded video
        transcript:    Full transcript text from Groq
        duration:      Video duration in seconds
        script:        AI-generated post script (context for overlay AI)
        emit_event:    SSE callback

    Returns:
        dict: {processed_url, overlays, demo, error (if any)}
    """
    emit = emit_event or (lambda *a, **kw: None)

    if not _ffmpeg_available():
        emit("overlay", "warning", "FFmpeg is not available — skipping video overlay processing.")
        return {"processed_url": None, "overlays": [], "demo": False,
                "error": "ffmpeg not found"}

    # 1. Generate the overlay plan
    plan = generate_overlay_plan(transcript, duration, script=script, emit_event=emit_event)
    overlays = plan.get("overlays", [])

    if not overlays:
        emit("overlay", "warning", "No overlays generated — video will be published as-is.")
        return {"processed_url": raw_video_url, "overlays": [], "demo": False}

    # 2. Download the raw video to a temp file
    emit("overlay", "progress", "Downloading your raw video for processing...")
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_in:
        input_path = tmp_in.name
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_out:
        output_path = tmp_out.name

    try:
        resp = requests.get(raw_video_url, stream=True, timeout=120)
        resp.raise_for_status()
        with open(input_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)

        emit("overlay", "progress", "Burning text overlays onto video with FFmpeg...")

        # 3. Burn overlays
        font_used = _resolve_font()
        logger.info("Overlay burn starting — font: %s | overlays: %d", font_used or "NONE", len(overlays))
        success, ffmpeg_err = burn_overlays(input_path, output_path, overlays)
        if not success:
            short_err = ffmpeg_err[-300:] if ffmpeg_err else "unknown FFmpeg error"
            emit("overlay", "warning", f"FFmpeg failed: {short_err[:120]}")
            return {"processed_url": raw_video_url, "overlays": overlays, "demo": False,
                    "error": f"ffmpeg burn failed: {short_err}"}

        out_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        logger.info("burn_overlays succeeded — output size: %d bytes", out_size)

        # 4. Upload processed video to R2
        processed_url = upload_processed_video(output_path, emit_event=emit_event)
        if not processed_url:
            logger.error("upload_processed_video returned None — falling back to raw URL")
            emit("overlay", "warning", "Overlays burned but R2 upload failed — video will publish as original.")
            return {"processed_url": raw_video_url, "overlays": overlays, "demo": False,
                    "error": "r2 upload failed"}

        logger.info("Processed video uploaded: %s", processed_url)
        emit("overlay", "progress",
             f"Video processing complete! {len(overlays)} overlays burned in.")

        return {"processed_url": processed_url, "overlays": overlays, "demo": plan.get("demo", False)}

    except Exception as e:
        logger.exception("process_video failed")
        emit("overlay", "warning", f"Video processing error: {str(e)[:120]}")
        return {"processed_url": raw_video_url, "overlays": [], "demo": False, "error": str(e)}
    finally:
        for path in [input_path, output_path]:
            try:
                os.unlink(path)
            except OSError:
                pass
