"""
tests/test_stage2_pipeline.py — Stage 2 Pipeline Diagnostic
=============================================================
Tests Groq API connection, FFmpeg availability, and the overlay
AI plan generator. Run this before doing a live video test.

Usage:
    python3 tests/test_stage2_pipeline.py
"""

import os
import sys

# Make sure imports resolve from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "

results = []


# ---------------------------------------------------------------------------
# Test 1: GROQ_API_KEY is set
# ---------------------------------------------------------------------------
def test_groq_key_set():
    key = os.getenv("GROQ_API_KEY", "")
    if key.startswith("gsk_"):
        results.append((PASS, "GROQ_API_KEY is set", f"...{key[-6:]}"))
        return True
    results.append((FAIL, "GROQ_API_KEY missing or wrong format", "Add it to your .env file"))
    return False


# ---------------------------------------------------------------------------
# Test 2: Groq API is reachable and key is valid
# ---------------------------------------------------------------------------
def test_groq_connection():
    import requests
    key = os.getenv("GROQ_API_KEY", "")
    if not key:
        results.append((WARN, "Groq connection skipped", "No API key"))
        return False
    try:
        resp = requests.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        if resp.ok:
            models = [m["id"] for m in resp.json().get("data", []) if "whisper" in m["id"].lower()]
            results.append((PASS, "Groq API connection OK", f"Whisper models available: {models}"))
            return True
        else:
            results.append((FAIL, "Groq API rejected key", f"HTTP {resp.status_code}: {resp.text[:100]}"))
            return False
    except Exception as e:
        results.append((FAIL, "Groq API unreachable", str(e)[:100]))
        return False


# ---------------------------------------------------------------------------
# Test 3: FFmpeg is installed
# ---------------------------------------------------------------------------
def test_ffmpeg():
    import subprocess
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        version_line = r.stdout.decode().split("\n")[0] if r.stdout else "unknown"
        results.append((PASS, "FFmpeg is installed", version_line[:60]))
        return True
    except FileNotFoundError:
        results.append((WARN, "FFmpeg not found locally",
                         "This is OK — FFmpeg will be installed on Railway via nixpacks.toml"))
        return False
    except Exception as e:
        results.append((WARN, "FFmpeg check failed", str(e)[:80]))
        return False


# ---------------------------------------------------------------------------
# Test 4: OpenRouter API key is set (needed for overlay AI)
# ---------------------------------------------------------------------------
def test_openrouter_key():
    key = os.getenv("OPENROUTER_API_KEY", "")
    if key:
        results.append((PASS, "OPENROUTER_API_KEY is set", f"...{key[-6:]}"))
        return True
    results.append((FAIL, "OPENROUTER_API_KEY missing", "Needed for AI overlay generation"))
    return False


# ---------------------------------------------------------------------------
# Test 5: R2 storage is configured (needed to store processed video)
# ---------------------------------------------------------------------------
def test_r2_config():
    required = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"]
    missing = [v for v in required if not os.getenv(v)]
    if not missing:
        results.append((PASS, "Cloudflare R2 configured", "All 4 required vars are set"))
        return True
    results.append((WARN, "R2 partially configured", f"Missing: {', '.join(missing)}"))
    return False


# ---------------------------------------------------------------------------
# Test 6: Overlay AI plan generator works (real call to Gemini)
# ---------------------------------------------------------------------------
def test_overlay_plan():
    key = os.getenv("OPENROUTER_API_KEY", "")
    if not key:
        results.append((WARN, "Overlay AI test skipped", "No OpenRouter key"))
        return False

    from services.video_processor import generate_overlay_plan

    sample_transcript = (
        "Hey party moms! So I wanted to share how I started my kids party business with under five hundred dollars. "
        "I went to the dollar store, got some balloons and decorations, and did my first event for a neighbor. "
        "She paid me three hundred dollars for a two hour setup. That was my proof of concept. "
        "If you want to start your own party business, you do not need thousands of dollars. "
        "You just need to start. Follow me for more tips on building your party business from scratch."
    )

    def fake_emit(stage, status, msg, *a, **kw):
        print(f"   [{stage}] {status}: {msg[:80]}")

    plan = generate_overlay_plan(
        transcript=sample_transcript,
        duration=45.0,
        script="Starting a kids party business with under $500",
        emit_event=fake_emit,
    )

    overlays = plan.get("overlays", [])
    if overlays:
        results.append((PASS, f"Overlay AI plan generated", f"{len(overlays)} overlays created"))
        for i, ov in enumerate(overlays):
            print(f"   Overlay {i+1}: [{ov.get('position','?')}] {ov.get('start',0):.1f}s–{ov.get('end',0):.1f}s → \"{ov.get('text','')}\"")
        return True
    else:
        err = plan.get("error", "no overlays returned")
        results.append((FAIL, "Overlay AI plan failed", err[:100]))
        return False


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("\n" + "="*60)
    print("  Stage 2 Pipeline Diagnostic")
    print("="*60 + "\n")

    test_groq_key_set()
    test_groq_connection()
    test_ffmpeg()
    test_openrouter_key()
    test_r2_config()

    print("\n--- Running overlay AI test (live API call) ---")
    test_overlay_plan()

    print("\n" + "="*60)
    print("  Results")
    print("="*60)
    for icon, label, detail in results:
        print(f"{icon}  {label}")
        if detail:
            print(f"     → {detail}")

    failures = [r for r in results if r[0] == FAIL]
    warnings = [r for r in results if r[0] == WARN]

    print(f"\n{len(results) - len(failures) - len(warnings)} passed  |  {len(warnings)} warnings  |  {len(failures)} failed")

    if not failures:
        print("\n🎉 All critical checks passed — pipeline is ready!")
    else:
        print("\n⚠️  Fix the failures above before testing with a real video.")
    print()
