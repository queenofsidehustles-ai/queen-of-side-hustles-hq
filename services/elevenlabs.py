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


def synthesize_with_timestamps(text: str) -> dict | None:
    """
    Convert text to audio AND get word-level timestamps.
    Returns {"audio": bytes, "words": [{"word": str, "start": float, "end": float}]}
    or None on failure.
    """
    api_key  = os.getenv("ELEVENLABS_API_KEY", "")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "") or DEFAULT_VOICE_ID

    if not api_key:
        return None

    model = "eleven_multilingual_v2" if os.getenv("ELEVENLABS_VOICE_ID") else "eleven_monolingual_v1"

    try:
        resp = requests.post(
            f"{ELEVENLABS_BASE}/text-to-speech/{voice_id}/with-timestamps",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "model_id": model,
                "voice_settings": {
                    "stability": 0.45,
                    "similarity_boost": 0.85,
                    "style": 0.2,
                    "use_speaker_boost": True,
                },
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()

        import base64
        audio_bytes = base64.b64decode(data["audio_base64"])
        alignment   = data.get("alignment", {})
        chars       = alignment.get("characters", [])
        starts      = alignment.get("character_start_times_seconds", [])
        ends        = alignment.get("character_end_times_seconds", [])

        # Group characters into words
        words = []
        current_word = ""
        word_start   = None
        for i, ch in enumerate(chars):
            t_start = starts[i] if i < len(starts) else None
            t_end   = ends[i]   if i < len(ends)   else None
            if ch == " " or ch == "\n":
                if current_word and word_start is not None:
                    words.append({"word": current_word, "start": word_start, "end": t_start or t_end})
                current_word = ""
                word_start   = None
            else:
                if word_start is None:
                    word_start = t_start
                current_word += ch
        if current_word and word_start is not None:
            words.append({"word": current_word, "start": word_start, "end": ends[-1] if ends else word_start + 0.3})

        logger.info("ElevenLabs timestamps: %d words, %d bytes", len(words), len(audio_bytes))
        return {"audio": audio_bytes, "words": words}

    except Exception as e:
        logger.exception("ElevenLabs with-timestamps failed")
        return None


def words_to_caption_chunks(words: list, chunk_size: int = 4) -> list:
    """
    Group words into TikTok-style caption chunks of chunk_size words.
    Returns [{"text": str, "start": float, "end": float}]
    """
    chunks = []
    for i in range(0, len(words), chunk_size):
        group = words[i:i + chunk_size]
        text  = " ".join(w["word"] for w in group)
        start = group[0]["start"]
        end   = group[-1]["end"]
        chunks.append({"text": text, "start": start, "end": end})
    return chunks


def synthesize(text: str) -> bytes | None:
    """
    Convert text to MP3 audio bytes using Monica's cloned voice.
    Returns None if no API key is set.
    """
    api_key  = os.getenv("ELEVENLABS_API_KEY", "")
    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "") or DEFAULT_VOICE_ID

    if not api_key:
        logger.info("ElevenLabs not configured — skipping TTS")
        return None

    # eleven_multilingual_v2 is the highest-quality model for cloned voices
    model = "eleven_multilingual_v2" if os.getenv("ELEVENLABS_VOICE_ID") else "eleven_monolingual_v1"

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
                "model_id": model,
                "voice_settings": {
                    "stability": 0.45,
                    "similarity_boost": 0.85,
                    "style": 0.2,
                    "use_speaker_boost": True,
                },
            },
            timeout=60,
        )
        resp.raise_for_status()
        logger.info("ElevenLabs TTS: %d bytes for %d chars (model: %s)", len(resp.content), len(text), model)
        return resp.content

    except requests.exceptions.HTTPError as e:
        logger.error("ElevenLabs HTTP error: %s — %s", e, resp.text[:200] if resp else "")
        return None
    except Exception as e:
        logger.exception("ElevenLabs TTS failed")
        return None
