"""
services/elevenlabs.py — Text-to-Speech via ElevenLabs
========================================================
Converts a script to MP3 audio using Monica's cloned voice.
Falls back gracefully to None if no API key is configured.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"

# Rachel — a professional female voice available on ElevenLabs free tier.
# Used as placeholder until Monica's cloned voice is set.
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"


def is_configured() -> bool:
    return bool(os.getenv("ELEVENLABS_API_KEY"))


def synthesize(text: str) -> bytes | None:
    """
    Convert text to MP3 audio bytes using the configured ElevenLabs voice.
    Returns None if no API key is set (clip will keep its original audio instead).
    """
    api_key  = os.getenv("ELEVENLABS_API_KEY", "")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "") or DEFAULT_VOICE_ID

    if not api_key:
        logger.info("ElevenLabs not configured — skipping TTS, keeping original audio")
        return None

    try:
        resp = requests.post(
            f"{ELEVENLABS_BASE}/text-to-speech/{voice_id}",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": text,
                "model_id": "eleven_monolingual_v1",
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.75,
                },
            },
            timeout=30,
        )
        resp.raise_for_status()
        logger.info("ElevenLabs TTS: %d bytes for %d chars", len(resp.content), len(text))
        return resp.content

    except requests.exceptions.HTTPError as e:
        logger.error("ElevenLabs HTTP error: %s — %s", e, resp.text[:200] if resp else "")
        return None
    except Exception as e:
        logger.exception("ElevenLabs TTS failed")
        return None
