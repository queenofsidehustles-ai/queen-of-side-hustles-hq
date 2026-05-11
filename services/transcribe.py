"""
services/transcribe.py — Audio transcription via Groq Whisper
=============================================================
Pulls audio from a video file using FFmpeg, then sends it to
Groq's Whisper API to get a full text transcript with segment timestamps.
"""

import os
import logging
import subprocess
import tempfile

import requests

logger = logging.getLogger(__name__)

GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def extract_audio(video_path: str, audio_path: str) -> bool:
    """Extract audio track from video using FFmpeg. Returns True on success."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", video_path, "-q:a", "0", "-map", "a", audio_path],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.error("FFmpeg audio extraction failed: %s", e)
        return False


def transcribe_audio(audio_path: str, emit_event=None) -> dict:
    """
    Send an audio file to Groq Whisper for transcription with segment timestamps.

    Returns dict: {text, segments: [{text, start, end}], duration, demo}
    """
    emit = emit_event or (lambda *a, **kw: None)
    api_key = os.getenv("GROQ_API_KEY")

    if not api_key:
        emit("transcribe", "warning",
             "No Groq API key — add GROQ_API_KEY in Settings to enable real transcription. Using demo mode.")
        return {
            "text": "Demo transcription — add your Groq API key in Settings.",
            "segments": [{"text": "Demo", "start": 0.0, "end": 3.0}],
            "duration": 30.0,
            "demo": True,
        }

    emit("transcribe", "progress", "Sending audio to Groq Whisper for transcription...")

    try:
        with open(audio_path, "rb") as f:
            response = requests.post(
                GROQ_TRANSCRIPTION_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (os.path.basename(audio_path), f, "audio/mpeg")},
                data={
                    "model": "whisper-large-v3",
                    "response_format": "verbose_json",
                    "timestamp_granularities[]": "segment",
                },
                timeout=120,
            )

        if not response.ok:
            logger.error("Groq transcription error %s: %s", response.status_code, response.text[:300])
            return {"text": "", "segments": [], "duration": 0.0,
                    "error": f"Groq error {response.status_code}: {response.text[:200]}"}

        data = response.json()
        segments = [
            {"text": s.get("text", ""), "start": float(s.get("start", 0)), "end": float(s.get("end", 0))}
            for s in data.get("segments", [])
        ]
        duration = float(data.get("duration", 0.0))
        if not duration and segments:
            duration = segments[-1]["end"] + 1.0

        emit("transcribe", "progress",
             f"Transcribed {len(segments)} segments ({duration:.0f}s of speech).")

        return {
            "text": data.get("text", ""),
            "segments": segments,
            "duration": duration,
            "demo": False,
        }

    except Exception as e:
        logger.exception("Groq transcription unexpected error")
        return {"text": "", "segments": [], "duration": 0.0, "error": str(e)}


def transcribe_video(video_path: str, emit_event=None) -> dict:
    """
    Full transcription flow: extract audio → transcribe with Groq.

    Returns dict: {text, segments, duration, demo, error (if any)}
    """
    emit = emit_event or (lambda *a, **kw: None)

    if not _ffmpeg_available():
        emit("transcribe", "warning", "FFmpeg is not installed — cannot extract audio.")
        return {"text": "", "segments": [], "duration": 0.0, "error": "ffmpeg not found"}

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        audio_path = tmp.name

    try:
        emit("transcribe", "progress", "Extracting audio from your video...")
        if not extract_audio(video_path, audio_path):
            emit("transcribe", "warning", "Could not extract audio — video may have no audio track.")
            return {"text": "", "segments": [], "duration": 0.0, "error": "audio extraction failed"}

        return transcribe_audio(audio_path, emit_event=emit_event)
    finally:
        try:
            os.unlink(audio_path)
        except OSError:
            pass
