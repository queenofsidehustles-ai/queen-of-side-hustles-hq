"""
pipeline.py — Pipeline Orchestrator
======================================
This is the CORE of the app. It runs the 6-stage content automation pipeline
and emits SSE events at every step so the "Automation X-ray" can visualize it.

Pipeline stages:
  1. scrape   — FireCrawl (skip if input_type='idea')
  2. script   — OpenRouter LLM generates post text
  3. image    — Kie.ai Nano Banana Pro generates image
  4. video    — Kie.ai Veo 3.1 generates video (optional)
  5. caption  — OpenRouter LLM generates platform-specific captions
  6. publish  — GetLate.dev posts to social media (only when explicitly triggered)

Each stage:
  - Updates the content_item status in the database
  - Logs to pipeline_logs
  - Emits SSE events via the emit_event callback
  - Tracks duration and cost
"""

import json
import time
from datetime import datetime

from models import ContentItem, PipelineLog, Setting
from extensions import db
from services.firecrawl import scrape_url
from services.openrouter import generate_script, generate_image_prompt, generate_captions, generate_pbh_script, generate_pbh_captions
from services.kie_ai import generate_image, generate_video, generate_video_with_reference
from services.getlate import publish_post
from services.r2_storage import upload_image as r2_upload_image, upload_video as r2_upload_video, is_configured as r2_is_configured
from services.transcribe import transcribe_video
from services.video_processor import process_video


# ---------------------------------------------------------------------------
# Helper: record per-stage duration and cost as JSON on content item
# ---------------------------------------------------------------------------
def _record_stage_metric(content_id, stage_name, duration=0.0, cost=0.0):
    """
    Append a stage's duration and cost to the JSON blobs stored on the content item.
    Reads existing stage_durations / stage_costs (or starts from {}), adds the new
    values, and writes back.
    """
    item = db.session.get(ContentItem, content_id)
    if not item:
        return

    # Parse existing JSON or start fresh
    try:
        durations = json.loads(item.stage_durations or "{}")
    except (json.JSONDecodeError, TypeError):
        durations = {}
    try:
        costs = json.loads(item.stage_costs or "{}")
    except (json.JSONDecodeError, TypeError):
        costs = {}

    durations[stage_name] = duration
    costs[stage_name] = cost

    item.stage_durations = json.dumps(durations)
    item.stage_costs = json.dumps(costs)
    db.session.commit()


# ---------------------------------------------------------------------------
# Internal helper: add a pipeline log entry
# ---------------------------------------------------------------------------
def _add_log(content_id, stage, status, message, detail=None):
    log = PipelineLog(
        content_id=content_id,
        stage=stage,
        status=status,
        message=message,
        detail=detail,
    )
    db.session.add(log)
    db.session.commit()


# ---------------------------------------------------------------------------
# run_pipeline() — Execute stages 1-5 (everything except publish)
# ---------------------------------------------------------------------------
def run_pipeline(content_id, emit_event):
    """
    Run the full content pipeline for a content item.
    Executes stages 1-5 sequentially, emitting SSE events at each step.

    Stage 6 (publish) is NOT run here — it's triggered separately via /api/publish/<id>.

    Args:
        content_id: ID of the content_item to process
        emit_event: Callback function(stage, status, message, detail=None)
    """
    # Load the content item
    item = db.session.get(ContentItem, content_id)
    if not item:
        emit_event("pipeline", "error", f"Content item {content_id} not found")
        return

    total_cost = 0.0
    pipeline_start = time.time()

    emit_event("pipeline", "started",
               f"Kicking off your content machine! We'll turn your {'link' if item.input_type == 'url' else 'idea'} into a ready-to-post piece of content.",
               {"content_id": content_id, "input_type": item.input_type})

    try:
        # ==================================================================
        # STAGE 1: SCRAPE — Fetch article from URL (skip if 'idea')
        # ==================================================================
        cost = stage_scrape(content_id, item, emit_event)
        total_cost += cost
        # Reload item to get updated fields
        item = db.session.get(ContentItem, content_id)

        # ==================================================================
        # STAGE 2: SCRIPT — Generate social media post text
        # ==================================================================
        cost = stage_script(content_id, item, emit_event)
        total_cost += cost
        item = db.session.get(ContentItem, content_id)

        # ==================================================================
        # STAGE 3: IMAGE — Skip if user already uploaded their own media
        # ==================================================================
        item = db.session.get(ContentItem, content_id)
        has_own_media = bool(item.r2_image_url or item.r2_video_url or item.video_url)
        if has_own_media:
            emit_event("image", "skipped", "You uploaded your own media — skipping AI image generation and using yours instead.")
            _add_log(content_id, "image", "skipped", "User media uploaded — AI image skipped")
        else:
            try:
                cost = stage_image(content_id, item, emit_event)
                total_cost += cost
            except Exception as img_err:
                emit_event("image", "skipped", f"Image generation skipped ({str(img_err)[:80]}) — continuing to captions.")
                _add_log(content_id, "image", "skipped", f"Image error (skipped): {str(img_err)[:120]}")
        item = db.session.get(ContentItem, content_id)

        # ==================================================================
        # STAGE 4: VIDEO — Generate video (optional, skip if user has media)
        # ==================================================================
        if has_own_media or not item.include_video:
            if item.include_video and not has_own_media:
                pass  # will fall through to normal video stage below
            else:
                if item.include_video:
                    emit_event("video", "skipped", "Using your uploaded video — skipping AI video generation.")
                    _add_log(content_id, "video", "skipped", "User media uploaded — AI video skipped")
        if not has_own_media and item.include_video:
            try:
                cost = stage_video(content_id, item, emit_event)
                total_cost += cost
            except Exception as vid_err:
                emit_event("video", "skipped", f"Video generation skipped ({str(vid_err)[:80]}) — continuing.")
                _add_log(content_id, "video", "skipped", f"Video error (skipped): {str(vid_err)[:120]}")
        item = db.session.get(ContentItem, content_id)

        # ==================================================================
        # STAGE 5: CAPTION — Generate platform-specific captions
        # ==================================================================
        cost = stage_caption(content_id, item, emit_event)
        total_cost += cost
        item = db.session.get(ContentItem, content_id)

        # ==================================================================
        # STAGE 6: R2 UPLOAD
        # ==================================================================
        cost = stage_r2_upload(content_id, item, emit_event)
        total_cost += cost
        item = db.session.get(ContentItem, content_id)

        # ==================================================================
        # STAGE 7: TRANSCRIBE — speech-to-text via Groq Whisper
        # (only runs when the item has an uploaded video)
        # Wrapped in try/except — a transcription failure must NOT kill voiceover
        # ==================================================================
        video_source = item.r2_video_url or item.video_url
        if video_source:
            try:
                cost = stage_transcribe(content_id, item, emit_event)
                total_cost += cost
                item = db.session.get(ContentItem, content_id)
            except Exception as transcribe_err:
                emit_event("transcribe", "warning",
                           f"Transcription skipped ({str(transcribe_err)[:80]}) — continuing to voiceover.")
                _add_log(content_id, "transcribe", "warning",
                         f"Transcription error (non-fatal): {str(transcribe_err)[:200]}")
                item = db.session.get(ContentItem, content_id)

            # ==============================================================
            # STAGE 8: OVERLAY — burn AI-generated text onto video via FFmpeg
            # (only runs when transcription succeeded)
            # ==============================================================
            if item.transcript:
                try:
                    cost = stage_overlay(content_id, item, emit_event)
                    total_cost += cost
                    item = db.session.get(ContentItem, content_id)
                except Exception as overlay_err:
                    emit_event("overlay", "warning",
                               f"Overlay skipped ({str(overlay_err)[:80]}) — continuing to voiceover.")
                    item = db.session.get(ContentItem, content_id)

        # ==================================================================
        # STAGE 9: VOICEOVER — ElevenLabs reads the script over the video
        # Always runs (voiceover does not depend on transcription succeeding)
        # ==================================================================
        cost = stage_voiceover(content_id, item, emit_event)
        total_cost += cost
        item = db.session.get(ContentItem, content_id)

        # ==================================================================
        # PIPELINE COMPLETE
        # ==================================================================
        total_duration = round(time.time() - pipeline_start, 1)

        item = db.session.get(ContentItem, content_id)
        # PBH items stay in "review" so Monica can preview before posting
        if getattr(item, "brand", None) != "pbh":
            item.status = "ready"
        item.cost_total = total_cost
        db.session.commit()

        emit_event("pipeline", "complete",
                   f"All done! Your content is ready to post. The whole thing took {total_duration}s and cost ${total_cost:.4f}. That's the power of automation — what would take you an hour, the machine did in under a minute.",
                   {"total_duration": total_duration, "total_cost": total_cost})

    except Exception as e:
        # If any stage fails, mark the item as failed
        item = db.session.get(ContentItem, content_id)
        if item:
            item.status = "failed"
            db.session.commit()
        error_msg = str(e)
        if "timed out" in error_msg.lower():
            friendly = f"Pipeline stopped: {error_msg}"
        else:
            friendly = f"Something went wrong: {error_msg}. Check your API keys in Settings, or try again in a few minutes."
        # Log the error to the database so it shows in Pipeline Log
        try:
            _add_log(content_id, "pipeline", "error", error_msg[:500])
        except Exception:
            pass
        emit_event("pipeline", "error", friendly, {"error": error_msg})


# ---------------------------------------------------------------------------
# STAGE 1: SCRAPE
# ---------------------------------------------------------------------------
def stage_scrape(content_id, item, emit_event):
    """Scrape article from URL. Skip if input_type is 'idea'."""
    stage = "scrape"

    if item.input_type != "url":
        emit_event(stage, "skipped", "You typed an idea (not a link), so we don't need to go grab an article from the internet. Skipping this step!")
        _add_log(content_id, stage, "skipped", "Input is an idea, no URL to scrape")
        return 0.0

    start = time.time()
    emit_event(stage, "started", "Sending your link to FireCrawl — it will visit the webpage and pull out just the article text (no ads, no menus, just the good stuff).")
    _add_log(content_id, stage, "started", f"Scraping: {item.input_text}")

    row = db.session.get(ContentItem, content_id)
    row.status = "scraping"
    db.session.commit()

    result = scrape_url(item.input_text, emit_event=emit_event)

    duration = round(time.time() - start, 1)

    # Save scraped content to database
    row = db.session.get(ContentItem, content_id)
    row.status = "scraped"
    row.article_text = result["markdown"]
    row.article_title = result.get("title", "")
    row.word_count = result.get("word_count", 0)
    db.session.commit()

    detail = {
        "duration": duration,
        "word_count": result.get("word_count", 0),
        "title": result.get("title", ""),
        "demo": result.get("demo", False),
        "cost": 0.0
    }

    emit_event(stage, "complete",
               f"Got it! FireCrawl pulled {result.get('word_count', 0):,} words from the article in {duration}s. Now we'll send this text to AI to turn it into a post.",
               detail)
    _add_log(content_id, stage, "complete",
             f"Scraped {result.get('word_count', 0)} words", json.dumps(detail))

    _record_stage_metric(content_id, stage, duration=duration, cost=0.0)
    return 0.0  # FireCrawl cost is tracked per-account, not per-call


# ---------------------------------------------------------------------------
# STAGE 2: SCRIPT
# ---------------------------------------------------------------------------
def stage_script(content_id, item, emit_event):
    """Generate social media post script via LLM."""
    stage = "script"
    start = time.time()

    emit_event(stage, "started", f"Sending your content to AI (via OpenRouter) to write a {item.platform} post. Think of OpenRouter as a switchboard — it picks the best AI model for the job.")
    _add_log(content_id, stage, "started", "Calling OpenRouter LLM")

    row = db.session.get(ContentItem, content_id)
    row.status = "scripting"
    db.session.commit()

    # Use article text if available, otherwise use the raw input
    source_text = item.article_text or item.input_text
    input_type = item.input_type

    # PBH uses its own script generator — completely separate system prompt
    # to prevent KPPS hooks ("undercharging", pricing coaching) from bleeding in
    if getattr(item, "brand", None) == "pbh":
        from models import PBHKnowledge
        knowledge = PBHKnowledge.get()
        result = generate_pbh_script(
            source_text,
            platform=item.platform,
            knowledge_base=knowledge,
            emit_event=emit_event,
        )
    else:
        result = generate_script(
            source_text,
            platform=item.platform,
            input_type=input_type,
            content_type=getattr(item, "content_type", None),
            emit_event=emit_event,
        )

    duration = round(time.time() - start, 1)

    # Save script to database
    row = db.session.get(ContentItem, content_id)
    row.status = "scripted"
    row.script = result["text"]
    db.session.commit()

    detail = {
        "duration": duration,
        "model": result.get("model", ""),
        "tokens_in": result.get("tokens_in", 0),
        "tokens_out": result.get("tokens_out", 0),
        "cost": result.get("cost", 0.0),
        "demo": result.get("demo", False)
    }

    emit_event(stage, "complete",
               f"Script is done! The AI wrote {result.get('tokens_out', 0)} tokens (words/pieces) in {duration}s. Cost: ${result.get('cost', 0):.4f}. Next up: creating a matching image.",
               detail)
    _add_log(content_id, stage, "complete",
             f"Script: {result.get('tokens_out', 0)} tokens", json.dumps(detail))

    cost = result.get("cost", 0.0)
    _record_stage_metric(content_id, stage, duration=duration, cost=cost)
    return cost


# ---------------------------------------------------------------------------
# STAGE 3: IMAGE
# ---------------------------------------------------------------------------
def stage_image(content_id, item, emit_event):
    """Generate image: first create a prompt via LLM, then generate via Kie.ai."""
    stage = "image"
    start = time.time()

    emit_event(stage, "started", "Time to create an image! First, AI will describe what the image should look like (a 'prompt'). Then we send that description to Kie.ai, which actually draws the picture.")
    _add_log(content_id, stage, "started", "Generating image prompt + image")

    row = db.session.get(ContentItem, content_id)
    row.status = "imaging"
    db.session.commit()

    # Step 1: Generate an image prompt from the script
    prompt_result = generate_image_prompt(item.script or "", emit_event=emit_event)
    image_prompt = prompt_result["text"]

    # Save the prompt
    row = db.session.get(ContentItem, content_id)
    row.image_prompt = image_prompt
    db.session.commit()

    # Step 2: Generate the actual image via Kie.ai
    image_result = generate_image(image_prompt, emit_event=emit_event)

    duration = round(time.time() - start, 1)

    # Save image URL and task ID — skip demo/placeholder URLs (placehold.co)
    row = db.session.get(ContentItem, content_id)
    row.status = "imaged"
    generated_url = image_result.get("image_url", "")
    if not image_result.get("demo") and "placehold" not in generated_url:
        row.image_url = generated_url
    row.image_task_id = image_result.get("task_id", "")
    db.session.commit()

    # Total cost = LLM prompt cost + image generation cost
    total_cost = prompt_result.get("cost", 0.0) + image_result.get("cost", 0.0)

    detail = {
        "duration": duration,
        "image_url": image_result.get("image_url", ""),
        "task_id": image_result.get("task_id", ""),
        "prompt_cost": prompt_result.get("cost", 0.0),
        "image_cost": image_result.get("cost", 0.0),
        "total_cost": total_cost,
        "demo": image_result.get("demo", False)
    }

    emit_event(stage, "complete",
               f"Image is ready! It took {duration}s because Kie.ai had to actually render the picture (that's why we kept checking on it). Cost: ${total_cost:.4f}.",
               detail)
    _add_log(content_id, stage, "complete",
             f"Image ready: {image_result.get('task_id', 'N/A')}", json.dumps(detail))

    _record_stage_metric(content_id, stage, duration=duration, cost=total_cost)
    return total_cost


# ---------------------------------------------------------------------------
# STAGE 4: VIDEO (optional)
# ---------------------------------------------------------------------------
def stage_video(content_id, item, emit_event):
    """Generate video via Kie.ai. Skip if include_video is False. Supports headshot toggle."""
    stage = "video"

    if not item.include_video:
        emit_event(stage, "skipped", "You didn't ask for a video this time, so we're skipping this step. (Videos take longer and cost more — you can turn this on anytime!)")
        _add_log(content_id, stage, "skipped", "include_video is False")
        return 0.0

    start = time.time()
    emit_event(stage, "started", "Creating a video with Kie.ai's Veo model. This takes longer than images (sometimes a few minutes) because video has way more frames to generate. Watch the polling — we keep asking 'is it done yet?' every 20 seconds.")
    _add_log(content_id, stage, "started", "Calling Kie.ai Veo 3.1")

    row = db.session.get(ContentItem, content_id)
    row.status = "videoing"
    db.session.commit()

    # Use the image prompt as the video prompt (with slight modification)
    video_prompt = item.image_prompt or item.script or ""
    row = db.session.get(ContentItem, content_id)
    row.video_prompt = video_prompt
    db.session.commit()

    # Check headshot toggle
    headshot_enabled = Setting.get("headshot_enabled")
    headshot_url = Setting.get("headshot_url")

    if headshot_enabled and headshot_url:
        emit_event(stage, "info", "Headshot reference detected — generating video with your face for a personal touch.")
        video_result = generate_video_with_reference(video_prompt, headshot_url, emit_event=emit_event)
        row = db.session.get(ContentItem, content_id)
        row.headshot_used = True
        db.session.commit()
    else:
        video_result = generate_video(video_prompt, emit_event=emit_event)

    duration = round(time.time() - start, 1)

    # Handle graceful timeout: log warning but continue to caption stage
    if video_result.get("timed_out"):
        emit_event(stage, "warning", "Video generation timed out — moving on to captions. You can re-run the video stage separately once it finishes processing.")
        _add_log(content_id, stage, "warning", "Video timed out — skipping gracefully")
        row = db.session.get(ContentItem, content_id)
        row.status = "captioned"
        row.video_task_id = video_result.get("task_id", "")
        db.session.commit()
        _record_stage_metric(content_id, stage, duration=duration, cost=0.0)
        return 0.0

    row = db.session.get(ContentItem, content_id)
    row.status = "videoed"
    row.video_url = video_result.get("video_url", "")
    row.video_task_id = video_result.get("task_id", "")
    db.session.commit()

    cost = video_result.get("cost", 0.0)

    detail = {
        "duration": duration,
        "video_url": video_result.get("video_url", ""),
        "task_id": video_result.get("task_id", ""),
        "cost": cost,
        "demo": video_result.get("demo", False)
    }

    emit_event(stage, "complete",
               f"Video is done! Took {duration}s (see how much longer than the image?). Cost: ${cost:.4f}. Almost there — just need captions now.",
               detail)
    _add_log(content_id, stage, "complete",
             f"Video ready: {video_result.get('task_id', 'N/A')}", json.dumps(detail))

    _record_stage_metric(content_id, stage, duration=duration, cost=cost)
    return cost


# ---------------------------------------------------------------------------
# STAGE 5: CAPTION
# ---------------------------------------------------------------------------
def stage_caption(content_id, item, emit_event):
    """Generate platform-specific captions via LLM."""
    stage = "caption"
    start = time.time()

    row = db.session.get(ContentItem, content_id)
    row.status = "captioning"
    db.session.commit()

    # Generate captions for the target platform + all 4 social platforms
    target = item.platform or "instagram"
    platforms = list(set([target, "instagram", "tiktok", "facebook", "youtube"]))

    emit_event(stage, "started", f"Last step! Each social platform has different rules (character limits, hashtag styles, tone). We're asking AI to write custom captions for {', '.join(platforms)} so each one fits perfectly.")
    _add_log(content_id, stage, "started", "Calling OpenRouter for captions")

    # PBH gets its own caption generator — separate from KPPS
    if getattr(item, "brand", None) == "pbh":
        from models import PBHKnowledge
        knowledge = PBHKnowledge.get()
        result = generate_pbh_captions(
            item.script or "",
            platforms=platforms,
            knowledge_base=knowledge,
            emit_event=emit_event,
        )
    else:
        result = generate_captions(
            item.script or "",
            platforms=platforms,
            emit_event=emit_event,
        )

    duration = round(time.time() - start, 1)

    # Save captions as JSON
    captions_json = json.dumps(result.get("captions", {}))
    row = db.session.get(ContentItem, content_id)
    # PBH items go to "review" so Monica can read them before posting
    row.status = "review" if getattr(row, "brand", None) == "pbh" else "captioned"
    row.captions = captions_json
    db.session.commit()

    cost = result.get("cost", 0.0)

    detail = {
        "duration": duration,
        "platforms": platforms,
        "cost": cost,
        "demo": result.get("demo", False)
    }

    emit_event(stage, "complete",
               f"Captions ready for {len(platforms)} platforms in {duration}s! Each one is tailored — different length, tone, and hashtags for each platform.",
               detail)
    _add_log(content_id, stage, "complete",
             f"Captions for {len(platforms)} platforms", json.dumps(detail))

    _record_stage_metric(content_id, stage, duration=duration, cost=cost)
    return cost


# ---------------------------------------------------------------------------
# STAGE 6: R2 UPLOAD (optional — uploads assets to Cloudflare R2)
# ---------------------------------------------------------------------------
def stage_r2_upload(content_id, item, emit_event):
    """Upload image/video to Cloudflare R2 for permanent storage. Skips if R2 not configured."""
    stage = "r2_upload"

    if not r2_is_configured():
        emit_event(stage, "skipped", "R2 storage is not configured — skipping cloud upload. Your images/videos will use the original API URLs (which may expire). Add R2 credentials in Settings for permanent hosting.")
        _add_log(content_id, stage, "skipped", "R2 not configured")
        return 0.0

    start = time.time()
    emit_event(stage, "started", "Uploading your assets to Cloudflare R2 for permanent storage. API-generated URLs can expire, so we save copies to your own cloud bucket.")
    _add_log(content_id, stage, "started", "Uploading to R2")

    row = db.session.get(ContentItem, content_id)
    # Don't overwrite PBH "review" status — voiceover + preview still pending
    if getattr(row, "brand", None) != "pbh":
        row.status = "uploading"
    db.session.commit()

    r2_image_url = None
    r2_video_url = None

    # Upload image if it exists and is not a demo placeholder
    image_url = item.image_url or ""
    if image_url and "placehold" not in image_url:
        try:
            img_result = r2_upload_image(image_url, emit_event=emit_event)
            r2_image_url = img_result.get("url")
        except Exception as e:
            emit_event(stage, "warning", f"Image upload to R2 failed: {e}")

    # Upload video if it exists and is not a demo placeholder
    video_url = item.video_url or ""
    if video_url and "placehold" not in video_url:
        try:
            vid_result = r2_upload_video(video_url, emit_event=emit_event)
            r2_video_url = vid_result.get("url")
        except Exception as e:
            emit_event(stage, "warning", f"Video upload to R2 failed: {e}")

    duration = round(time.time() - start, 1)

    row = db.session.get(ContentItem, content_id)
    row.r2_image_url = r2_image_url
    row.r2_video_url = r2_video_url
    db.session.commit()

    detail = {
        "duration": duration,
        "r2_image_url": r2_image_url,
        "r2_video_url": r2_video_url,
        "cost": 0.0,
    }

    emit_event(stage, "complete",
               f"Assets uploaded to R2 in {duration}s. Your content now has permanent URLs that won't expire.",
               detail)
    _add_log(content_id, stage, "complete",
             "Uploaded to R2", json.dumps(detail))

    _record_stage_metric(content_id, stage, duration=duration, cost=0.0)
    return 0.0


# ---------------------------------------------------------------------------
# PUBLISH — triggered separately via /api/publish/<id>, NOT part of run_pipeline()
# ---------------------------------------------------------------------------
def stage_publish(content_id, emit_event):
    """
    Publish content via GetLate.dev.
    This is NOT called by run_pipeline() — it's triggered separately
    via the /api/publish/<id> endpoint.
    """
    stage = "publish"
    start = time.time()

    item = db.session.get(ContentItem, content_id)
    if not item:
        emit_event(stage, "error", f"Content item {content_id} not found")
        return

    emit_event(stage, "started", "Publishing to social media...")
    _add_log(content_id, stage, "started", "Calling GetLate.dev API")

    item.status = "publishing"
    db.session.commit()

    try:
        result = publish_post(item, emit_event=emit_event)

        duration = round(time.time() - start, 1)

        item = db.session.get(ContentItem, content_id)
        item.status = "published"
        item.published_at = datetime.utcnow()
        db.session.commit()

        detail = {
            "duration": duration,
            "post_id": result.get("post_id", ""),
            "platforms": result.get("platforms_published", []),
            "demo": result.get("demo", False)
        }

        emit_event(stage, "complete",
                   f"Published in {duration}s!",
                   detail)
        _add_log(content_id, stage, "complete",
                 f"Published: {result.get('post_id', 'N/A')}", json.dumps(detail))

    except Exception as e:
        item = db.session.get(ContentItem, content_id)
        if item:
            item.status = "failed"
            db.session.commit()
        emit_event(stage, "error", f"Publishing failed: {str(e)}")
        _add_log(content_id, stage, "error", str(e))


# ---------------------------------------------------------------------------
# STAGE 7: TRANSCRIBE (Groq Whisper speech-to-text)
# ---------------------------------------------------------------------------
def stage_transcribe(content_id, item, emit_event):
    """
    Download the uploaded video, extract audio, and transcribe with Groq Whisper.
    Saves the full transcript text to item.transcript.
    """
    stage = "transcribe"
    start = time.time()

    video_url = item.r2_video_url or item.video_url
    if not video_url:
        emit_event(stage, "skipped", "No video found — skipping transcription.")
        _add_log(content_id, stage, "skipped", "No video URL")
        return 0.0

    emit_event(stage, "started",
               "Downloading your video and sending the audio to Groq Whisper — it will turn your spoken words into text so AI knows what you said.")
    _add_log(content_id, stage, "started", f"Transcribing: {video_url[:80]}")

    row = db.session.get(ContentItem, content_id)
    row.status = "transcribing"
    db.session.commit()

    import tempfile
    import requests as _requests

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        video_path = tmp.name

    try:
        resp = _requests.get(video_url, stream=True, timeout=120)
        resp.raise_for_status()
        with open(video_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)

        result = transcribe_video(video_path, emit_event=emit_event)
    finally:
        import os as _os
        try:
            _os.unlink(video_path)
        except OSError:
            pass

    duration = round(time.time() - start, 1)

    transcript_text = result.get("text", "")
    video_duration_secs = result.get("duration", 0.0)

    row = db.session.get(ContentItem, content_id)
    row.status = "transcribed"
    row.transcript = transcript_text
    # Store video duration so the overlay stage can use it for text timing
    try:
        existing_durations = json.loads(row.stage_durations or "{}")
    except Exception:
        existing_durations = {}
    existing_durations["video_seconds"] = video_duration_secs
    row.stage_durations = json.dumps(existing_durations)
    db.session.commit()

    detail = {
        "duration": duration,
        "transcript_length": len(transcript_text),
        "segments": len(result.get("segments", [])),
        "video_duration": video_duration_secs,
        "demo": result.get("demo", False),
        "error": result.get("error", ""),
    }

    if result.get("error") and not transcript_text:
        emit_event(stage, "warning",
                   f"Transcription ran into an issue: {result['error'][:100]}. Skipping overlay stage.")
        _add_log(content_id, stage, "warning", result["error"][:200])
    else:
        emit_event(stage, "complete",
                   f"Transcribed {len(result.get('segments', []))} spoken segments in {duration}s. Now AI will plan the on-screen text.",
                   detail)
        _add_log(content_id, stage, "complete",
                 f"{len(transcript_text)} chars transcribed", json.dumps(detail))

    _record_stage_metric(content_id, stage, duration=duration, cost=0.0)
    return 0.0   # Groq free tier


# ---------------------------------------------------------------------------
# STAGE 8: OVERLAY (FFmpeg text overlay burning)
# ---------------------------------------------------------------------------
def stage_overlay(content_id, item, emit_event):
    """
    Use AI to plan on-screen text overlays based on the transcript,
    then burn them onto the video with FFmpeg and upload to R2.
    """
    stage = "overlay"
    start = time.time()

    video_url = item.r2_video_url or item.video_url
    if not video_url:
        emit_event(stage, "skipped", "No video to overlay — skipping.")
        return 0.0

    emit_event(stage, "started",
               "AI is designing your on-screen text (hook + key points + CTA) and FFmpeg is going to burn it right onto the video — white text, hot-pink outline, centered in the bottom third.")
    _add_log(content_id, stage, "started", "Generating overlay plan + FFmpeg burn")

    row = db.session.get(ContentItem, content_id)
    row.status = "overlaying"
    db.session.commit()

    # Read video duration stored by stage_transcribe
    try:
        seg_data = json.loads(item.stage_durations or "{}")
        vid_dur = float(seg_data.get("video_seconds", 0.0))
    except Exception:
        vid_dur = 0.0
    if not vid_dur:
        vid_dur = 60.0   # safe default for overlay timing

    result = process_video(
        raw_video_url=video_url,
        transcript=item.transcript or "",
        duration=vid_dur,
        script=item.script or "",
        emit_event=emit_event,
    )

    duration = round(time.time() - start, 1)

    processed_url = result.get("processed_url")
    row = db.session.get(ContentItem, content_id)
    row.status = "overlaid"
    if processed_url and processed_url != video_url:
        row.r2_video_url = processed_url
    db.session.commit()

    detail = {
        "duration": duration,
        "overlays": len(result.get("overlays", [])),
        "processed_url": processed_url,
        "demo": result.get("demo", False),
        "error": result.get("error", ""),
    }

    err = result.get("error", "")
    if err == "ffmpeg not found":
        emit_event(stage, "warning", "FFmpeg is not installed on this server — no overlay possible.")
        _add_log(content_id, stage, "warning", "FFmpeg not found on server")
    elif err.startswith("ffmpeg burn failed"):
        short = err[len("ffmpeg burn failed: "):].strip()[-200:]
        emit_event(stage, "warning", f"FFmpeg error: {short[:120]}")
        _add_log(content_id, stage, "warning", f"FFmpeg error: {short}")
    elif err == "r2 upload failed":
        emit_event(stage, "warning", "Text was burned onto video but could NOT be saved to R2 — check R2 credentials in Railway Variables.")
        _add_log(content_id, stage, "warning", "Overlays burned but R2 upload failed — processed video lost")
    elif not result.get("overlays"):
        emit_event(stage, "warning", f"No overlays generated — video unchanged. {err or 'AI returned empty plan.'}")
        _add_log(content_id, stage, "warning", err or "Empty overlay plan")
    else:
        final_url = result.get("processed_url", "")
        emit_event(stage, "complete",
                   f"Done! {len(result.get('overlays', []))} text overlays burned onto your video in {duration}s.",
                   detail)
        _add_log(content_id, stage, "complete",
                 f"{len(result.get('overlays', []))} overlays applied", json.dumps(detail))
        _add_log(content_id, stage, "info", f"Processed video: {final_url[:120]}")

    _record_stage_metric(content_id, stage, duration=duration, cost=0.0)
    return 0.0


# ---------------------------------------------------------------------------
# regenerate_image() — Re-run just the image stage with a new/edited prompt
# ---------------------------------------------------------------------------
def regenerate_image(content_id, new_prompt, emit_event):
    """
    Regenerate the image for a content item using a new prompt.
    Called from the /api/regenerate-image/<id> endpoint.
    """
    emit_event("image", "started", "Regenerating image with updated prompt...")
    _add_log(content_id, "image", "started", "Regenerating with new prompt")

    start = time.time()

    # Save the new prompt
    row = db.session.get(ContentItem, content_id)
    if row:
        row.image_prompt = new_prompt
        db.session.commit()

    # Generate new image
    result = generate_image(new_prompt, emit_event=emit_event)

    duration = round(time.time() - start, 1)

    row = db.session.get(ContentItem, content_id)
    if row:
        regen_url = result.get("image_url", "")
        if not result.get("demo") and "placehold" not in regen_url:
            row.image_url = regen_url
        row.image_task_id = result.get("task_id", "")
        db.session.commit()

    detail = {
        "duration": duration,
        "image_url": result.get("image_url", ""),
        "task_id": result.get("task_id", ""),
        "cost": result.get("cost", 0.0),
        "demo": result.get("demo", False)
    }

    emit_event("image", "complete",
               f"Image regenerated in {duration}s",
               detail)
    _add_log(content_id, "image", "complete",
             f"Regenerated: {result.get('task_id', 'N/A')}", json.dumps(detail))


# ---------------------------------------------------------------------------
# Helper: fit script to video duration, always ending with a CTA
# ---------------------------------------------------------------------------
def _fit_script_to_duration(script_text, video_secs, emit_event=None):
    """
    Trim the script so ElevenLabs audio won't outlast the video clip.
    Speaking pace: ~2.2 words/second (natural TikTok/IG Reels pace).
    Always ends with a 'comment PARTY' style call to action.
    """
    import random
    import re as _re

    emit = emit_event or (lambda *a, **kw: None)

    CTA_POOL = [
        "Comment PARTY below!",
        "Comment PARTY for your free guide!",
        "Comment PARTY to get started today!",
        "Drop PARTY in the comments!",
        "Comment PARTY and I'll DM you the details!",
        "Type PARTY below to learn more!",
    ]

    _CTA_KEYWORDS = ["comment party", "drop party", "type party", "dm me", "comment below",
                     "link in bio", "save this", "follow for"]

    def _has_cta(text):
        t = text.lower()
        return any(kw in t for kw in _CTA_KEYWORDS)

    WORDS_PER_SEC = 2.2
    target_total = max(12, int(video_secs * WORDS_PER_SEC))
    cta = random.choice(CTA_POOL)
    cta_words = len(cta.split())
    target_body = max(6, target_total - cta_words - 2)  # -2 for breathing room

    words = script_text.split()
    current_word_count = len(words)

    # Script already fits AND has a CTA — return as-is
    if current_word_count <= target_total and _has_cta(script_text):
        return script_text

    # Script fits but no CTA — just append one
    if current_word_count <= target_total:
        return script_text.rstrip() + "\n\n" + cta

    # Script is too long — trim sentence by sentence, then add CTA
    sentences = _re.split(r'(?<=[.!?\n])\s+', script_text.strip())
    body_parts = []
    word_count = 0

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        w = len(sentence.split())
        if word_count + w <= target_body:
            body_parts.append(sentence)
            word_count += w
        else:
            if not body_parts:
                # Always keep at least the hook (first sentence, truncated if needed)
                body_parts.append(" ".join(sentence.split()[:target_body]))
            break

    trimmed_body = " ".join(body_parts).rstrip(".,;:— ")
    result = trimmed_body + "\n\n" + cta

    emit("voiceover", "progress",
         f"Script trimmed from {current_word_count} to {len(result.split())} words to fit {video_secs:.0f}s clip. CTA added.")

    return result


# ---------------------------------------------------------------------------
# STAGE 9: VOICEOVER — Monica's cloned voice reads the script over the video
# ---------------------------------------------------------------------------
def stage_voiceover(content_id, item, emit_event):
    """
    Generate ElevenLabs TTS with word timestamps, layer audio over the video,
    and burn synced TikTok-style captions onto the screen.
    Silently skips if no video attached or ElevenLabs not configured.
    """
    from services.elevenlabs import is_configured, synthesize_with_timestamps, words_to_caption_chunks
    from services.video_processor import add_voiceover_with_captions

    stage = "voiceover"
    start = time.time()

    if getattr(item, 'skip_voiceover', False):
        emit_event(stage, "skipped", "Skipping AI voiceover — your recorded voice will be used as-is.")
        return 0.0

    if not is_configured():
        emit_event(stage, "skipped", "ElevenLabs not configured — add ELEVENLABS_API_KEY to Railway to enable voiceover.")
        return 0.0

    _VIDEO_EXTS = (".mp4", ".mov", ".webm", ".avi", ".m4v")
    def _is_video_url(u):
        return u and u.split("?")[0].lower().endswith(_VIDEO_EXTS)

    # Check all media fields — Loom uploads often land in r2_image_url
    video_url = (item.r2_video_url or item.video_url
                 or (item.r2_image_url if _is_video_url(item.r2_image_url) else None)
                 or (item.image_url if _is_video_url(item.image_url) else None))
    if not video_url:
        emit_event(stage, "skipped", "No video attached — skipping voiceover.")
        return 0.0

    raw_script = item.script or ""
    if not raw_script:
        emit_event(stage, "skipped", "No script — skipping voiceover.")
        return 0.0

    # Strip production notes before TTS — AI sometimes adds stage directions that sound awful when read aloud
    import re
    script_text = raw_script
    # Remove [bracketed stage directions] like [Hook:], [Transition], [CTA], [Note: ...]
    script_text = re.sub(r'\[.*?\]', '', script_text, flags=re.DOTALL)
    # Remove (parenthetical notes) — producer notes like (pause here), (cut to demo)
    script_text = re.sub(r'\(.*?\)', '', script_text, flags=re.DOTALL)
    # Remove lines that are just labels: "Hook:", "CTA:", "Transition:", "Note:", "VO:", "Scene:"
    script_text = re.sub(r'(?im)^\s*(hook|cta|transition|note|vo|scene|caption|text on screen|on screen|visual|b-?roll)\s*:.*$', '', script_text)
    # Strip hashtags — they sound terrible when read aloud (#partybizhub → silence or robotic spelling)
    script_text = re.sub(r'#\w+', '', script_text)
    # Strip URLs
    script_text = re.sub(r'https?://\S+', '', script_text)
    # Strip emoji characters that ElevenLabs may pronounce oddly
    script_text = re.sub(r'[^\x00-\x7FÀ-ɏḀ-ỿ]', '', script_text)
    # Clean up whitespace
    script_text = re.sub(r'[ \t]+', ' ', script_text)
    script_text = re.sub(r'\n{3,}', '\n\n', script_text).strip()

    # ── Pacing fixes for natural ElevenLabs delivery ──────────────────
    # Ellipses → period so the voice stops cleanly instead of trailing off weirdly
    script_text = re.sub(r'\.{2,}', '.', script_text)
    # Em dash / double dash → comma pause (reads more naturally than a hard stop)
    script_text = re.sub(r'\s*—\s*|\s*--\s*', ', ', script_text)
    # Clean up any excessive newlines — let ElevenLabs read punctuation naturally
    script_text = re.sub(r'\n{3,}', '\n\n', script_text).strip()

    # Auto-shorten script to match video duration so the voice never outlasts the clip.
    # Also guarantees a "comment PARTY" CTA at the end of every voiceover.
    try:
        durations = json.loads(item.stage_durations or "{}")
        video_secs = float(durations.get("video_seconds", 0))
    except Exception:
        video_secs = 0.0

    if video_secs > 0:
        script_text = _fit_script_to_duration(script_text, video_secs, emit_event=emit_event)
    else:
        # No duration info — still ensure CTA is present
        _CTA_KEYWORDS = ["comment party", "drop party", "type party", "dm me", "link in bio", "save this"]
        if not any(kw in script_text.lower() for kw in _CTA_KEYWORDS):
            import random
            _ctas = ["Comment PARTY below!", "Drop PARTY in the comments!", "Comment PARTY to get started!"]
            script_text = script_text.rstrip() + "\n\nComment PARTY below!"

    emit_event(stage, "progress", "Sending script to ElevenLabs — recording your cloned voice...")

    result = synthesize_with_timestamps(script_text)
    caption_chunks = []

    if result:
        audio_bytes    = result["audio"]
        words          = result["words"]
        caption_chunks = words_to_caption_chunks(words, chunk_size=4)
        emit_event(stage, "progress",
                   f"Got {len(audio_bytes)//1024}KB audio, {len(words)} words → {len(caption_chunks)} caption chunks. Combining with video...")
    else:
        # Fallback: try basic synthesis without timestamps
        emit_event(stage, "progress", "Timestamps endpoint failed — trying basic voice synthesis...")
        from services.elevenlabs import synthesize
        audio_bytes = synthesize(script_text)
        if not audio_bytes:
            emit_event(stage, "warning",
                       "ElevenLabs returned no audio. Check that ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID "
                       "are correct in Railway Variables, and that your ElevenLabs account has credits.")
            return 0.0
        emit_event(stage, "progress", f"Basic voice synthesis succeeded ({len(audio_bytes)//1024}KB). Adding to video...")

    row = db.session.get(ContentItem, content_id)
    voiced_url = add_voiceover_with_captions(
        video_url, audio_bytes, caption_chunks, emit_event=emit_event
    )

    if voiced_url:
        row.r2_video_url = voiced_url
        db.session.commit()
        duration = round(time.time() - start, 1)
        emit_event(stage, "complete",
                   f"Done in {duration}s — your voice is reading the script and captions are burned in!")
    else:
        emit_event(stage, "warning", "Voiceover failed — video will post without audio.")

    return 0.0
