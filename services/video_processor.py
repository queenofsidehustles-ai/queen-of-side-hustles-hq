"""
services/video_processor.py — AI text overlay burning
======================================================
Downloads a raw video from R2, burns on-screen text overlays using
PIL (Pillow) for text rendering + FFmpeg overlay filter for compositing,
then re-uploads the processed video to R2.

Brand style: white bold text, hot-pink (#EC4899) stroke, hook at top,
key points + CTA in the bottom third.

Why PIL instead of FFmpeg drawtext:
  drawtext requires system fonts and has a fragile filter-string parser.
  PIL renders each text segment as a transparent PNG image; FFmpeg then
  composites those images onto the video with the overlay filter. This
  separates text rendering (reliable, font bundled in repo) from video
  compositing (FFmpeg's strongest suit).
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
# Brand constants
# ---------------------------------------------------------------------------
_STROKE_COLOR_RGBA = (236, 72, 153, 255)   # #EC4899 hot-pink, fully opaque
_STROKE_WIDTH      = 4
_MAX_CHARS         = 52                    # hard cap per overlay line

# y-position as fraction of video height
_Y_TOP_FRAC    = 0.08
_Y_BOTTOM_FRAC = 0.72

# Bundled font — committed to repo, always available on Railway
_BUNDLED_FONT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "fonts", "Arial-Bold.ttf"
)
_RESOLVED_FONT = ""   # cached after first call


# ---------------------------------------------------------------------------
# Font resolution
# ---------------------------------------------------------------------------
def _resolve_font() -> str:
    """Return absolute path to a usable TTF font. Tries bundled font first."""
    global _RESOLVED_FONT
    if _RESOLVED_FONT:
        return _RESOLVED_FONT

    # 1. Bundled font in repo — most reliable
    if os.path.isfile(_BUNDLED_FONT):
        _RESOLVED_FONT = _BUNDLED_FONT
        logger.info("Font (bundled): %s", _RESOLVED_FONT)
        return _RESOLVED_FONT

    # 2. fc-match (fontconfig + liberation_ttf installed via nixpacks)
    try:
        r = subprocess.run(
            ["fc-match", "--format=%{file}", "sans:bold"],
            capture_output=True, text=True, timeout=5,
        )
        p = r.stdout.strip()
        if p and os.path.isfile(p):
            _RESOLVED_FONT = p
            logger.info("Font (fc-match): %s", _RESOLVED_FONT)
            return _RESOLVED_FONT
    except Exception:
        pass

    # 3. Search nix store
    try:
        r = subprocess.run(
            ["find", "/nix/store", "-name", "LiberationSans-Bold.ttf", "-type", "f"],
            capture_output=True, text=True, timeout=10,
        )
        for line in r.stdout.strip().splitlines():
            line = line.strip()
            if line and os.path.isfile(line):
                _RESOLVED_FONT = line
                logger.info("Font (nix): %s", _RESOLVED_FONT)
                return _RESOLVED_FONT
    except Exception:
        pass

    logger.warning("No font found — text overlays will not render")
    return ""


# ---------------------------------------------------------------------------
# FFmpeg availability
# ---------------------------------------------------------------------------
def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Text sanitization (keep text clean for display)
# ---------------------------------------------------------------------------
def _sanitize(text: str) -> str:
    """Remove characters that could cause display issues."""
    for ch in ("\\", "'", "\"", "\n", "\r", "\t"):
        text = text.replace(ch, " ")
    return " ".join(text.split())[:_MAX_CHARS]


# ---------------------------------------------------------------------------
# PIL helpers
# ---------------------------------------------------------------------------
def _get_video_size(input_path: str) -> tuple:
    """Return (width, height) via ffprobe. Defaults to 1080x1920 (TikTok)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=s=x:p=0", input_path],
            capture_output=True, text=True, timeout=10,
        )
        parts = r.stdout.strip().split("x")
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    except Exception:
        pass
    return 1080, 1920


def _make_text_png(text: str, vid_w: int, vid_h: int,
                   fontsize: int, position: str, font_path: str) -> str:
    """
    Render white bold text with brand-pink stroke onto a transparent RGBA image.
    Saves to a temp .png and returns the file path.
    """
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (vid_w, vid_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Load font
    try:
        font = ImageFont.truetype(font_path, fontsize)
    except Exception:
        try:
            font = ImageFont.load_default(size=fontsize)
        except Exception:
            font = ImageFont.load_default()

    # Measure text to center it
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=_STROKE_WIDTH)
    text_w = bbox[2] - bbox[0]

    x = max(10, (vid_w - text_w) // 2)
    y = int(vid_h * (_Y_TOP_FRAC if position == "top" else _Y_BOTTOM_FRAC))

    # Draw: white fill + hot-pink stroke
    draw.text(
        (x, y), text, font=font,
        fill=(255, 255, 255, 255),
        stroke_width=_STROKE_WIDTH,
        stroke_fill=_STROKE_COLOR_RGBA,
    )

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    img.save(tmp.name, "PNG")
    tmp.close()
    return tmp.name


# ---------------------------------------------------------------------------
# Core overlay burn: PIL renders text, FFmpeg composites images onto video
# ---------------------------------------------------------------------------
def burn_overlays(input_path: str, output_path: str, overlays: list) -> tuple:
    """
    Burn text overlays onto video.
    Uses PIL to create transparent text PNGs, then FFmpeg overlay filter.
    Returns (success: bool, error_msg: str).
    """
    if not overlays:
        import shutil
        shutil.copy2(input_path, output_path)
        return True, ""

    font_path = _resolve_font()
    if not font_path:
        return False, "No font file found — cannot render text"

    vid_w, vid_h = _get_video_size(input_path)
    logger.info("Video %dx%d | font: %s | %d overlays", vid_w, vid_h, font_path, len(overlays))

    png_files = []   # (path, start_sec, end_sec)
    try:
        for ov in overlays:
            text = _sanitize(ov.get("text", ""))
            if not text:
                continue
            png_path = _make_text_png(
                text=text,
                vid_w=vid_w, vid_h=vid_h,
                fontsize=int(ov.get("fontsize", 50)),
                position=ov.get("position", "bottom"),
                font_path=font_path,
            )
            png_files.append((
                png_path,
                float(ov.get("start", 0)),
                float(ov.get("end", 3)),
            ))
            logger.info("Overlay PNG: %s | t=%s-%s", text, ov.get("start"), ov.get("end"))

        if not png_files:
            import shutil
            shutil.copy2(input_path, output_path)
            return True, ""

        # Build FFmpeg command.
        # IMPORTANT: each PNG input needs -loop 1 so FFmpeg loops the
        # single frame for the full video duration instead of stalling
        # waiting for more frames from the ended image stream.
        cmd = ["ffmpeg", "-y", "-i", input_path]
        for png_path, _, _ in png_files:
            cmd += ["-loop", "1", "-i", png_path]

        # Build filter_complex:
        # scale ensures even dimensions (required by yuv420p/libx264).
        # Each PNG overlay is explicitly converted to rgba before compositing.
        parts = ["[0:v]scale=trunc(iw/2)*2:trunc(ih/2)*2,format=rgba[base]"]
        prev = "[base]"
        for i, (_, start, end) in enumerate(png_files):
            ovr = f"[ovr{i}]"
            out = f"[v{i}]"
            parts.append(f"[{i+1}:v]format=rgba{ovr}")
            parts.append(
                f"{prev}{ovr}overlay=enable='between(t,{start},{end})'{out}"
            )
            prev = out
        parts.append(f"{prev}format=yuv420p[final]")

        filter_complex = ";".join(parts)
        final_label = "[final]"
        logger.info("filter_complex: %s", filter_complex)

        cmd += [
            "-filter_complex", filter_complex,
            "-map", final_label,
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            output_path,
        ]

        logger.info("FFmpeg cmd: %s", " ".join(cmd[:8]))
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        if result.returncode != 0:
            full_err = result.stderr
            logger.error("FFmpeg FULL stderr (rc=%d):\n%s", result.returncode, full_err)
            # FFmpeg stderr: version banner first (~2000 chars), real error after.
            # Skip the banner by finding where the banner ends (first blank line after
            # the configuration line), then take the tail of what remains.
            banner_end = full_err.find("\n\n", 500)
            after_banner = full_err[banner_end:].strip() if banner_end > 0 else full_err
            err = after_banner[-1200:].strip() if after_banner else full_err[-1200:].strip()
            logger.error("FFmpeg error (after banner):\n%s", err)
            return False, err

        out_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        logger.info("Overlay complete — output %d bytes", out_size)
        return True, ""

    except Exception as e:
        logger.exception("burn_overlays failed")
        return False, str(e)
    finally:
        for png_path, _, _ in png_files:
            try:
                os.unlink(png_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# AI overlay plan generation
# ---------------------------------------------------------------------------
def generate_overlay_plan(transcript: str, duration: float,
                          script: str = "", emit_event=None) -> dict:
    """
    Ask Gemini (via OpenRouter) to create a timed overlay plan from the transcript.
    Returns dict: {overlays: [{text, start, end, fontsize, position}], demo: bool}
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

━━ CONTENT OVERLAYS (position: bottom) ━━
2–3 overlays timed to key moments in the transcript.
Each overlay = the single most powerful phrase from that moment.

━━ CTA (position: bottom, last 4s) ━━
One action. Example: "Follow for party biz tips" or "Save this for later"

RULES:
- MAX 6 words per overlay
- No apostrophes, quotes, colons, brackets, or special characters
- All caps OK for 2-3 word punchy overlays only

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
# R2 upload helper (from local file path)
# ---------------------------------------------------------------------------
def upload_processed_video(file_path: str, emit_event=None):
    """
    Upload a locally processed video to Cloudflare R2.
    Returns the public URL on success, None if R2 not configured or upload fails.
    """
    emit = emit_event or (lambda *a, **kw: None)

    required = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"]
    if not all(os.getenv(v) for v in required):
        emit("overlay", "warning", "R2 not configured — processed video won't be saved.")
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
# process_video() — called from pipeline.py stage_overlay
# ---------------------------------------------------------------------------
def process_video(raw_video_url: str, transcript: str, duration: float,
                  script: str = "", emit_event=None) -> dict:
    """
    Full pipeline: download → AI overlay plan → PIL+FFmpeg burn → R2 upload.

    Returns:
        dict with keys: processed_url, overlays, demo, error (if any)
    """
    emit = emit_event or (lambda *a, **kw: None)

    if not _ffmpeg_available():
        emit("overlay", "warning", "FFmpeg is not installed on this server.")
        return {"processed_url": None, "overlays": [], "demo": False,
                "error": "ffmpeg not found"}

    # 1. Generate overlay plan via AI
    plan = generate_overlay_plan(transcript, duration, script=script, emit_event=emit_event)
    overlays = plan.get("overlays", [])

    if not overlays:
        emit("overlay", "warning", "No overlays generated — video unchanged.")
        return {"processed_url": raw_video_url, "overlays": [], "demo": False}

    # 2. Download raw video
    emit("overlay", "progress", "Downloading video for processing...")
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

        emit("overlay", "progress",
             f"Rendering {len(overlays)} text overlays with PIL + compositing with FFmpeg...")

        # 3. Burn overlays (PIL text → FFmpeg overlay filter)
        success, burn_err = burn_overlays(input_path, output_path, overlays)
        if not success:
            emit("overlay", "warning", f"Overlay burn failed: {burn_err[-300:]}")
            return {"processed_url": raw_video_url, "overlays": overlays, "demo": False,
                    "error": f"ffmpeg burn failed: {burn_err}"}

        # 4. Upload processed video to R2
        processed_url = upload_processed_video(output_path, emit_event=emit_event)
        if not processed_url:
            return {"processed_url": raw_video_url, "overlays": overlays, "demo": False,
                    "error": "r2 upload failed"}

        emit("overlay", "progress",
             f"Done! {len(overlays)} overlays burned in and video uploaded.")
        return {"processed_url": processed_url, "overlays": overlays,
                "demo": plan.get("demo", False)}

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
