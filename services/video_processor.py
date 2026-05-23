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


def _drawtext_available() -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-filters"], capture_output=True, text=True, timeout=5)
        return "drawtext" in r.stdout
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Text sanitization (keep text clean for display)
# ---------------------------------------------------------------------------
def _sanitize(text: str) -> str:
    """Remove characters that break FFmpeg's drawtext filter option parser."""
    for ch in ("\\", "'", "\"", "\n", "\r", "\t", ":", ";", "%", "{", "}"):
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


def _get_video_duration(input_path: str) -> float:
    """Return video duration in seconds via ffprobe. Defaults to 300s."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", input_path],
            capture_output=True, text=True, timeout=10,
        )
        return float(r.stdout.strip())
    except Exception:
        pass
    return 300.0


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
# Core overlay burn: FFmpeg drawtext filter (single input, no PNG files)
# ---------------------------------------------------------------------------
def burn_overlays(input_path: str, output_path: str, overlays: list) -> tuple:
    """
    Burn text overlays onto video using FFmpeg drawtext in filter_complex.
    Single input, no PNG files, no overlay filter — works on all FFmpeg versions.
    Returns (success: bool, error_msg: str).
    """
    if not overlays:
        import shutil
        shutil.copy2(input_path, output_path)
        return True, ""

    font_path = _resolve_font()
    if not font_path:
        return False, "No font file found — cannot render text"

    if not _drawtext_available():
        return False, "FFmpeg drawtext filter not available (needs libfreetype)"

    vid_w, vid_h = _get_video_size(input_path)
    vid_w = (vid_w // 2) * 2
    vid_h = (vid_h // 2) * 2
    logger.info("Video %dx%d | font: %s | %d overlays", vid_w, vid_h, font_path, len(overlays))

    # Build filter_complex: scale once, then chain drawtext nodes.
    # Using filter_complex (';' separator) so commas inside between(t,s,e) are safe.
    # format=yuv420p first: phone cameras record yuvj420p (full-range, deprecated).
    # Converting early avoids filter chain failures on FFmpeg 7.x.
    nodes = [f"[0:v]format=yuv420p,scale={vid_w}:{vid_h}[s0]"]
    prev = "[s0]"
    drawn = 0
    for ov in overlays:
        text = _sanitize(ov.get("text", ""))
        if not text:
            continue
        fontsize   = int(ov.get("fontsize", 50))
        start      = float(ov.get("start", 0))
        end        = float(ov.get("end", 3))
        y_frac     = _Y_TOP_FRAC if ov.get("position") == "top" else _Y_BOTTOM_FRAC
        out        = f"[t{drawn}]"
        nodes.append(
            f"{prev}drawtext="
            f"fontfile={font_path}"
            f":text='{text}'"
            f":fontsize={fontsize}"
            f":fontcolor=white"
            f":x=(w-text_w)/2"
            f":y=h*{y_frac}"
            f":enable='between(t,{start},{end})'"
            f":borderw={_STROKE_WIDTH}"
            f":bordercolor=0xEC4899"
            f"{out}"
        )
        prev = out
        drawn += 1
        logger.info("drawtext: %s | t=%s-%s", text, start, end)

    if drawn == 0:
        import shutil
        shutil.copy2(input_path, output_path)
        return True, ""

    filter_complex = ";".join(nodes)
    logger.info("filter_complex: %s", filter_complex[:300])

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-filter_complex", filter_complex,
        "-map", prev,
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        output_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            full_err = result.stderr
            logger.error("FFmpeg FULL stderr (rc=%d):\n%s", result.returncode, full_err)
            lines = full_err.splitlines()
            # Find first '[' line to skip the banner
            proc_start = next((i for i, l in enumerate(lines) if l.startswith("[")), 0)
            proc_lines = lines[proc_start:]
            # Extract lines containing actual error keywords
            error_kws = ["Error", "error", "Invalid", "invalid", "No such", "not found",
                         "failed", "Failed", "cannot", "Cannot", "Could not", "Permission",
                         "Conversion failed", "does not exist", "Unrecognized", "Unknown",
                         "unsupported", "wrong type", "not open", "corrupt"]
            err_lines = [l for l in proc_lines if any(kw in l for kw in error_kws)]
            if err_lines:
                err = "\n".join(err_lines[:10])
            else:
                err = "\n".join(proc_lines[:40])
            return False, err
        out_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        logger.info("Overlay complete — output %d bytes", out_size)
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "FFmpeg timed out after 300s"
    except Exception as e:
        logger.exception("burn_overlays failed")
        return False, str(e)


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
# add_voiceover() — layer ElevenLabs audio onto a silent video
# ---------------------------------------------------------------------------
def add_voiceover(video_url: str, audio_bytes: bytes, emit_event=None) -> str | None:
    """
    Download a silent video from R2, add audio from ElevenLabs, upload back to R2.
    Loops the video if it's shorter than the audio.
    Returns the new R2 URL, or None on failure.
    """
    emit = emit_event or (lambda *a, **kw: None)

    if not _ffmpeg_available():
        emit("voiceover", "warning", "FFmpeg not available — skipping voiceover.")
        return None

    tmp_video = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp_audio = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_out   = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp_video.close(); tmp_audio.close(); tmp_out.close()

    try:
        # Download video
        emit("voiceover", "progress", "Downloading video for voiceover...")
        resp = requests.get(video_url, stream=True, timeout=120)
        resp.raise_for_status()
        with open(tmp_video.name, "wb") as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)

        # Write audio
        with open(tmp_audio.name, "wb") as f:
            f.write(audio_bytes)

        vid_dur   = _get_video_duration(tmp_video.name)
        vid_w, vid_h = _get_video_size(tmp_video.name)
        vid_w = (vid_w // 2) * 2
        vid_h = (vid_h // 2) * 2

        emit("voiceover", "progress", f"Combining voice ({len(audio_bytes)//1024}KB) with video ({vid_dur:.0f}s)...")

        # If video is shorter than audio, loop it; otherwise just replace audio
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", tmp_video.name,
            "-i", tmp_audio.name,
            "-vf", f"scale={vid_w}:{vid_h},format=yuv420p",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            tmp_out.name,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.error("FFmpeg voiceover error: %s", result.stderr[-400:])
            emit("voiceover", "warning", "Voiceover combine failed — posting without audio.")
            return None

        emit("voiceover", "progress", "Voiceover combined! Uploading final video...")
        url = upload_processed_video(tmp_out.name, emit_event=emit_event)
        if url:
            emit("voiceover", "progress", "Voiceover video ready!")
        return url

    except Exception as e:
        logger.exception("add_voiceover failed")
        emit("voiceover", "warning", f"Voiceover error: {str(e)[:120]}")
        return None
    finally:
        for p in [tmp_video.name, tmp_audio.name, tmp_out.name]:
            try:
                os.unlink(p)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# add_voiceover_with_captions() — voice + synced on-screen text in one pass
# ---------------------------------------------------------------------------
def add_voiceover_with_captions(video_url: str, audio_bytes: bytes,
                                caption_chunks: list, emit_event=None) -> str | None:
    """
    Download video, replace audio with ElevenLabs voiceover, burn synced
    TikTok-style captions onto the video, upload to R2.

    caption_chunks: [{"text": str, "start": float, "end": float}]
    Returns new R2 URL or None on failure.
    """
    emit = emit_event or (lambda *a, **kw: None)

    if not _ffmpeg_available():
        emit("voiceover", "warning", "FFmpeg not available — skipping captions.")
        return add_voiceover(video_url, audio_bytes, emit_event=emit_event)

    font_path = _resolve_font()

    tmp_video  = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp_audio  = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_voiced = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp_out    = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    for f in [tmp_video, tmp_audio, tmp_voiced, tmp_out]:
        f.close()

    try:
        # 1. Download video
        emit("voiceover", "progress", "Downloading video...")
        resp = requests.get(video_url, stream=True, timeout=120)
        resp.raise_for_status()
        with open(tmp_video.name, "wb") as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)

        # 2. Write audio
        with open(tmp_audio.name, "wb") as f:
            f.write(audio_bytes)

        vid_dur   = _get_video_duration(tmp_video.name)
        vid_w, vid_h = _get_video_size(tmp_video.name)
        vid_w = (vid_w // 2) * 2
        vid_h = (vid_h // 2) * 2

        emit("voiceover", "progress", f"Adding voice to video ({vid_dur:.0f}s, {vid_w}x{vid_h})...")

        # 3. Combine video + audio (loop video if shorter than audio)
        cmd = [
            "ffmpeg", "-y",
            "-stream_loop", "-1", "-i", tmp_video.name,
            "-i", tmp_audio.name,
            "-vf", f"scale={vid_w}:{vid_h},format=yuv420p",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", tmp_voiced.name,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            logger.error("FFmpeg voice combine error: %s", r.stderr[-300:])
            emit("voiceover", "warning", "Voice combine failed — trying without captions.")
            return None

        # 4. Burn captions if we have a font and caption chunks
        if font_path and caption_chunks:
            emit("voiceover", "progress", f"Burning {len(caption_chunks)} caption chunks onto video...")
            overlays = []
            for chunk in caption_chunks:
                text = _sanitize(chunk["text"])
                if text:
                    overlays.append({
                        "text": text,
                        "start": chunk["start"],
                        "end": chunk["end"],
                        "fontsize": 52,
                        "position": "bottom",
                    })
            success, err = burn_overlays(tmp_voiced.name, tmp_out.name, overlays)
            if not success:
                logger.warning("Caption burn failed: %s — uploading without captions", err[:200])
                emit("voiceover", "warning", "Caption burn failed — uploading voice-only video.")
                import shutil
                shutil.copy2(tmp_voiced.name, tmp_out.name)
        else:
            import shutil
            shutil.copy2(tmp_voiced.name, tmp_out.name)
            if not font_path:
                emit("voiceover", "warning", "No font found — captions skipped, voice added.")

        # 5. Upload to R2
        emit("voiceover", "progress", "Uploading finished video to R2...")
        url = upload_processed_video(tmp_out.name, emit_event=emit_event)
        if url:
            emit("voiceover", "complete", "Video ready — your voice + captions are baked in!")
        return url

    except Exception as e:
        logger.exception("add_voiceover_with_captions failed")
        emit("voiceover", "warning", f"Error: {str(e)[:120]}")
        return None
    finally:
        for p in [tmp_video.name, tmp_audio.name, tmp_voiced.name, tmp_out.name]:
            try:
                os.unlink(p)
            except OSError:
                pass


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

    # Video text overlay is disabled — add text in TikTok/CapCut after downloading.
    emit("overlay", "progress", "Text overlay skipped — add captions in TikTok or CapCut.")
    return {"processed_url": raw_video_url, "overlays": [], "demo": False}

    # 1. Generate overlay plan via AI  # noqa: unreachable
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
            emit("overlay", "warning", f"Overlay burn failed: {burn_err[:400]}")
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
