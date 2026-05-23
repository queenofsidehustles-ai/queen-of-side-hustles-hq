"""
services/openrouter.py — LLM Integration via OpenRouter
=========================================================
Uses the openai Python library with a base_url swap to hit OpenRouter.
This means any model on OpenRouter works with the same code pattern.

Model: google/gemini-2.5-flash (fast + cheap — great for teaching demos)
"""

import os
import json
from openai import OpenAI

# ---------------------------------------------------------------------------
# Platform-specific character limits (technical max) and ideal lengths
# ---------------------------------------------------------------------------
PLATFORM_LIMITS = {
    "twitter":   280,
    "linkedin":  3000,
    "instagram": 2200,
    "tiktok":    4000,
    "youtube":   5000,
    "facebook":  63206,
}

# Ideal caption lengths — what actually performs well on each platform
PLATFORM_IDEAL = {
    "tiktok":    "100-200 characters. Ultra short. Just the hook + one CTA. Nothing else.",
    "instagram": "300-500 characters. Hook on line 1, 2-3 short lines of body, one CTA, 5 hashtags on a new line.",
    "linkedin":  "600-900 characters. Storytelling format. Hook → personal story → lesson → CTA. No hashtag overload.",
    "youtube":   "400-700 characters. Describe the value + include keywords for search. One CTA at the end.",
    "twitter":   "200-260 characters. One punchy line + link space. No hashtags unless trending.",
    "facebook":  "200-400 characters. Conversational, warm, community feel. One question or CTA.",
    "x":         "200-260 characters. One punchy line. Maximum 2 hashtags.",
}

# Default model — fast and cheap for teaching
DEFAULT_MODEL = "google/gemini-2.5-flash"


def _get_client():
    """
    Create an OpenAI client pointed at OpenRouter.
    Falls back to a demo mode if no API key is set.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None

    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=api_key,
        default_headers={
            "HTTP-Referer": os.getenv("APP_URL", "http://localhost:5000"),
            "X-Title": "Content Automation Demo"
        }
    )


def _demo_response(description):
    """Return a mock response when no API key is configured."""
    return {
        "text": (
            f"Hey! You're running in demo mode right now.\n\n"
            f"To get real AI-generated content, grab a free API key from OpenRouter:\n"
            f"1. Go to https://openrouter.ai/keys\n"
            f"2. Create an account and copy your key\n"
            f"3. Paste it in Settings > OpenRouter > API Key\n\n"
            f"Once you do that, this will generate real scripts, captions, and image prompts!"
        ),
        "model": "demo",
        "tokens_in": 0,
        "tokens_out": 0,
        "cost": 0.0,
        "demo": True
    }


# ---------------------------------------------------------------------------
# generate_script() — Turn an article/idea into a social media post
# ---------------------------------------------------------------------------
def generate_script(article_text_or_idea, platform="instagram", input_type="idea", emit_event=None):
    """
    Generate a social media post script from an article or idea.

    Args:
        article_text_or_idea: The scraped article text or raw idea string
        platform: Target platform (instagram, tiktok, linkedin, etc.)
        input_type: 'url' (article was scraped) or 'idea' (raw input)
        emit_event: Optional callback for SSE logging

    Returns:
        dict with: text, model, tokens_in, tokens_out, cost
    """
    emit = emit_event or (lambda *a, **kw: None)
    client = _get_client()

    if not client:
        emit("script", "progress", "No OpenRouter API key set yet — using demo content so you can see how the pipeline flows. Add your key in Settings to use real AI!")
        return _demo_response("Script generation requires an OpenRouter API key")

    char_limit = PLATFORM_LIMITS.get(platform, 2200)

    # Build the system prompt — psychology-driven, Monica's brand
    system_prompt = f"""You are a biopsychology-driven content strategist for Monica Lewis — Kids Party Business Coach, Queen of Side Hustles (@kidspartybizcoach, partybusinesscoach.com).

BRAND: Monica helps moms launch profitable kids party businesses from scratch or scale what they have.
Products: Kids Party Profit System ($497 Skool course) | Party Biz Hub software ($97/year founders)
Voice: empowering, warm, real-talk, boss-energy — speaks from lived experience, never preachy

TWO AUDIENCES — pick ONE per post:
• THE DREAMER: Wants to start a kids party biz. Craves freedom from the 9-to-5. Fears: starting from zero, not being qualified, not knowing what to charge, being judged.
• THE OPERATOR: Already runs kids parties or events. Wants better systems, more bookings, higher rates, less burnout. Fears: staying stuck, being underpriced, doing it all alone.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SCROLL-STOPPING HOOK — the single most important line
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
First 3 words carry 80% of the weight. Choose ONE formula:
• CURIOSITY GAP:      "The one thing stopping your first booking..."
• PAIN-FIRST:         "You're undercharging and you don't even know it."
• SPECIFICITY SHOCK:  "How one mom made $3,200 from 4 balloon setups."
• IDENTITY CALL-OUT:  "If you're a mom who wants out of the 9-to-5..."
• COUNTER-INTUITIVE:  "Stop posting pretty party photos. Here's why."
• BEFORE/AFTER:       "6 months ago I had zero clients. Now I have a waitlist."

WHAT KILLS A HOOK: Generic openers ("Today I want to", "In this post", "Tips for"), vague adjectives ("amazing", "great", "awesome"), and asking permission to teach.

BIOPSYCHOLOGY FRAMEWORK — embed in every post:
1. HOOK → Pattern interrupt: bypass the scroll reflex with something the brain cannot ignore
2. PAIN NAMING → Cortisol activation: name the exact daily frustration (not generic stress — specific moment)
3. AGITATION → Amygdala: make them feel what staying stuck costs them emotionally
4. TRANSFORMATION → Dopamine: paint the exact sensory experience of the win (the Stripe notification sound, the kid's face, the client saying "yes")
5. OXYTOCIN BRIDGE → "You don't have to figure this out alone. Moms just like you are doing it."
6. IDENTITY SHIFT → "You're not just a mom — you're a CEO building something your kids will be proud of."
7. LOSS AVERSION → Real scarcity only: "Founders pricing ends when we hit 100 members."
8. CTA → ONE action only. Make it low-friction: "comment PARTY", "save this", "DM me READY"

PAIN POINTS TO DRAW FROM (use when relevant, never force):
- "I don't know what to charge" / constant underpricing out of fear
- "I have no clients yet / crickets on social media"
- "I do everything myself and I'm exhausted"
- "I'm not sure I'm qualified enough"
- "I see other planners busy and I don't know what I'm missing"
- "I can't figure out how to get consistent bookings"

Platform: {platform} | Max: {char_limit} characters

FORMATTING: 2-4 intentional emojis | mobile line breaks after every 1-2 sentences | ONE CTA only
HASHTAGS (Instagram/TikTok only): #KidsPartyBusiness #PartyBizHub #SideHustle #MomBoss #KidsPartyPlanner

OUTPUT: Return ONLY the post text, starting directly with the hook. No labels like "AUDIENCE:" or "HOOK:". No explanations. No preamble. Just the post."""

    if input_type == "url":
        user_prompt = f"""Turn this article into a {platform} post for Monica's kids party business audience:

---
{article_text_or_idea[:4000]}
---

Apply the full psychology framework. Pick Dreamer OR Operator audience. Write in Monica's warm, boss-energy voice."""
    else:
        user_prompt = f"""Monica has already recorded a video with this hook or topic on screen:

"{article_text_or_idea}"

Write a {platform} caption that:
1. OPENS with Monica's exact words or a very close variation — do NOT invent a new hook to replace hers
2. Expands on what she said: adds a relatable moment, a quick story, or the "why this matters" for her audience
3. Picks ONE audience: Dreamer (tired of 9-to-5, wants to start a kids party biz from scratch) or Operator (already running parties, wants to grow)
4. Ends with ONE soft CTA that fits the mood — "follow for more", "save this", "comment PARTY", etc.

CRITICAL RULES:
- Do NOT mention pricing, courses, or products unless Monica's input already mentions them
- Do NOT replace her hook with a sales angle — match the energy of what she wrote
- The caption should feel like a natural continuation of what's already on her screen
- Write in Monica's warm, real-talk voice — like texting a friend who gets it"""

    emit("script", "progress", f"Calling OpenRouter → using the {DEFAULT_MODEL} model. OpenRouter is like a phone operator — it connects us to whichever AI model we pick.")

    try:
        response = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=1000,
            temperature=0.8,
        )

        # Extract the response
        text = response.choices[0].message.content.strip()
        usage = response.usage

        result = {
            "text": text,
            "model": DEFAULT_MODEL,
            "tokens_in": usage.prompt_tokens if usage else 0,
            "tokens_out": usage.completion_tokens if usage else 0,
            "cost": _estimate_cost(usage.prompt_tokens, usage.completion_tokens) if usage else 0.0,
            "demo": False
        }

        emit("script", "progress",
             f"AI wrote back! {result['tokens_out']} tokens (think of tokens like word-pieces). This call cost ${result['cost']:.4f} — pennies per post.")

        return result

    except Exception as e:
        emit("script", "error", f"OpenRouter error: {str(e)}")
        raise


# ---------------------------------------------------------------------------
# generate_image_prompt() — Create an image prompt from the script
# ---------------------------------------------------------------------------
def generate_image_prompt(script_text, emit_event=None):
    """
    Generate a descriptive image prompt from a social media script.
    This prompt will be sent to Kie.ai for image generation.

    Returns:
        dict with: text (the image prompt), model, tokens_in, tokens_out, cost
    """
    emit = emit_event or (lambda *a, **kw: None)
    client = _get_client()

    if not client:
        emit("image", "progress", "No OpenRouter API key — using a demo image prompt. Add your key in Settings to get AI-generated image descriptions!")
        return _demo_response("Image prompt generation requires an OpenRouter API key")

    system_prompt = """You are an expert at creating AI image generation prompts.
Given a social media post, create a single vivid image prompt that would make
a perfect visual companion for the post.

RULES:
- Describe the scene in detail (lighting, mood, colors, composition)
- Use photographic/artistic style keywords
- Keep it under 200 words
- Make it visually striking and scroll-stopping
- Do NOT include any text or words in the image description
- Describe a SCENE, not text overlays

OUTPUT FORMAT: Return ONLY the image prompt. No explanations."""

    emit("image", "progress", "Asking AI to describe a picture that matches your post — this description is called a 'prompt' and it tells the image AI exactly what to draw.")

    try:
        response = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Create an image prompt for this post:\n\n{script_text}"}
            ],
            max_tokens=300,
            temperature=0.9,
        )

        text = response.choices[0].message.content.strip()
        usage = response.usage

        result = {
            "text": text,
            "model": DEFAULT_MODEL,
            "tokens_in": usage.prompt_tokens if usage else 0,
            "tokens_out": usage.completion_tokens if usage else 0,
            "cost": _estimate_cost(usage.prompt_tokens, usage.completion_tokens) if usage else 0.0,
            "demo": False
        }

        emit("image", "progress", f"Image description is ready ({result['tokens_out']} tokens). Now sending it to Kie.ai to actually create the picture...")
        return result

    except Exception as e:
        emit("image", "error", f"OpenRouter error: {str(e)}")
        raise


# ---------------------------------------------------------------------------
# generate_captions() — Platform-specific captions
# ---------------------------------------------------------------------------
def generate_captions(script_text, platforms=None, emit_event=None):
    """
    Generate platform-specific captions from a script.
    Returns a dict keyed by platform name.

    Returns:
        dict with: captions (dict of platform->caption), model, tokens_in, tokens_out, cost
    """
    emit = emit_event or (lambda *a, **kw: None)
    client = _get_client()

    if not platforms:
        platforms = ["instagram", "tiktok", "linkedin"]

    if not client:
        emit("caption", "progress", "No OpenRouter API key — using demo captions. Add your key in Settings to get real AI-written captions for each platform!")
        demo_captions = {p: f"[DEMO] Caption for {p}" for p in platforms}
        return {
            "captions": demo_captions,
            "model": "demo",
            "tokens_in": 0,
            "tokens_out": 0,
            "cost": 0.0,
            "demo": True
        }

    # Build platform instructions with ideal lengths (not just technical max)
    platform_instructions = "\n".join([
        f"- {p.upper()}: {PLATFORM_IDEAL.get(p, 'Keep it concise and punchy.')}"
        for p in platforms
    ])

    system_prompt = f"""You are a biopsychology-driven buyer conversion strategist for Monica Lewis — Kids Party Business Coach, Queen of Side Hustles (@kidspartybizcoach).

BRAND: Kids Party Profit System ($497 Skool course) | Party Biz Hub software ($97/year founders)
VOICE: Empowering, warm, real-talk, boss-energy — from lived experience, never preachy or pushy

TWO AUDIENCES — pick ONE per caption:
• DREAMER: Wants to start a kids party biz. Craves freedom. Fears starting from zero.
• OPERATOR: Already running events. Wants systems, higher rates, more bookings.

━━ BUYER PSYCHOLOGY FRAMEWORK ━━
Every caption must move someone one step closer to buying without manipulation.
Use this funnel in order (compress for short platforms, expand for long):

1. SCROLL-STOP HOOK — curiosity gap, pain-first, or specificity shock (see below)
2. PAIN MIRROR — reflect their exact internal dialogue back at them so they feel seen
3. POSSIBILITY BRIDGE — "What if you could..." / show the transformation is real and reachable
4. MICRO-COMMITMENT — get a small yes before asking for the big yes ("does this sound familiar?")
5. SOCIAL PROOF ANCHOR — "moms just like you", "first booking in 30 days", "100 founders already inside"
6. IDENTITY UPGRADE — "You're not a hobbyist. You're a CEO who parties for a living."
7. LOW-FRICTION CTA — one action, zero pressure: "comment PARTY", "save this", "DM me READY"

SCROLL-STOPPING HOOK FORMULAS (first line only — pick the best fit):
• Curiosity gap:     "The one thing no one tells you about pricing parties..."
• Pain-first:        "You've been undercharging and you don't even know it."
• Specificity shock: "This mom made $3,200 from 4 balloon setups. Here's how."
• Identity call-out: "If you're a mom who wants out of the 9-to-5, read this."
• Counter-intuitive: "Stop posting pretty party photos. Here's why it's hurting you."

BIOPSYCHOLOGY TRIGGERS TO EMBED:
- Dopamine: the exact sensory win (Stripe ping, sold-out calendar, client saying "you're booked!")
- Oxytocin: belonging — "you don't have to figure this out alone"
- Loss aversion: real, honest scarcity only — "founders pricing ends at 100 members"
- Cortisol relief: name the stressful situation then immediately offer relief
- Mirror neurons: "other moms just like you are doing this right now"

CRITICAL LENGTH RULES — follow exactly:
{platform_instructions}

HASHTAGS (Instagram + TikTok only): #KidsPartyBusiness #PartyBizHub #SideHustle #MomBoss #KidsPartyPlanner

OUTPUT FORMAT: Valid flat JSON only.
Keys = platform names. Values = ONE caption string.
Example: {{"instagram": "Stop undercharging... 🎉", "tiktok": "This changed everything..."}}
Return ONLY the JSON object. No markdown, no nested keys, no explanations."""

    emit("caption", "progress", f"Asking AI to write custom captions for {', '.join(platforms)}. Each platform gets its own version — different length, hashtags, and style.")

    try:
        response = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Adapt this script for each platform:\n\n{script_text}"}
            ],
            max_tokens=1200,
            temperature=0.7,
            timeout=45,
        )

        if not response.choices:
            raise ValueError("OpenRouter returned empty choices — model may be overloaded, try again.")

        raw_text = response.choices[0].message.content.strip()
        usage = response.usage

        # Strip markdown code fences if present
        import re as _re
        raw_text = _re.sub(r"^```(?:json)?\s*", "", raw_text, flags=_re.MULTILINE)
        raw_text = _re.sub(r"\s*```\s*$", "", raw_text, flags=_re.MULTILINE)
        raw_text = raw_text.strip()

        try:
            captions = json.loads(raw_text)
        except json.JSONDecodeError:
            captions = {p: raw_text for p in platforms}

        result = {
            "captions": captions,
            "model": DEFAULT_MODEL,
            "tokens_in": usage.prompt_tokens if usage else 0,
            "tokens_out": usage.completion_tokens if usage else 0,
            "cost": _estimate_cost(usage.prompt_tokens, usage.completion_tokens) if usage else 0.0,
            "demo": False
        }

        emit("caption", "progress",
             f"Got captions for {len(captions)} platforms! Cost: ${result['cost']:.4f}. Notice how one AI call can output multiple results — that's efficiency!")

        return result

    except Exception as e:
        emit("caption", "error", f"OpenRouter error: {str(e)}")
        raise


# ---------------------------------------------------------------------------
# generate_pbh_script() — Party Biz Hub content (separate from KPPS)
# ---------------------------------------------------------------------------
def generate_pbh_script(input_text, platform="tiktok", knowledge_base="", emit_event=None):
    """
    Generate a Party Biz Hub post from the structured prompt built by pbh_api._build_input_text().
    Uses a PBH-specific system prompt — completely separate from KPPS to avoid topic bleed.
    knowledge_base: optional brand/avatar guide injected as source of truth.
    """
    emit = emit_event or (lambda *a, **kw: None)
    client = _get_client()

    if not client:
        emit("script", "progress", "No OpenRouter API key — using demo content.")
        return _demo_response("Script generation requires an OpenRouter API key")

    char_limit = PLATFORM_LIMITS.get(platform, 2200)

    kb_section = f"\n\nBRAND KNOWLEDGE BASE — use this as your source of truth for every post:\n{knowledge_base.strip()}" if knowledge_base and knowledge_base.strip() else ""

    system_prompt = f"""You are a social media copywriter for Party Biz Hub — a business management app for kids party business owners.

CORE CONTENT STRATEGY — 3-PART STRUCTURE (follow this every time):
1. PROBLEM: Open with the pain, chaos, or embarrassment the avatar feels right now. Make them feel seen.
2. SOLUTION: Position Party Biz Hub as THE answer to that exact problem — not just any software, the specific fix.
3. PROOF: Name the specific feature that solves it. Show HOW it fixes the problem in concrete terms.

Example: "Still sending quotes over text? (PROBLEM) Party Biz Hub fixes that. (SOLUTION) Send a professional, branded quote in 60 seconds — client reviews it and pays the deposit on the spot. (PROOF)"

Every post needs all three parts. Not just feelings. Not just features. The combination is what converts.

TARGET AVATAR: Someone ALREADY running a kids party business. They have clients, they're doing the work — but everything runs on chaos. Text-message bookings, Venmo payments with no records, handwritten contracts that get lost, no way to track income at tax time. They feel like a hobby, not a business. They're embarrassed. They're exhausted. They want to feel like a real CEO.

EMOTIONAL TRANSFORMATION THIS CONTENT MUST DELIVER:
FROM: Chaotic, disorganized, unprofessional, stressed at tax season, chasing invoices, afraid of double-booking
TO: Polished back office, clients see a real business, contracts signed digitally, income tracked automatically, tax season handled, everything in one place

HOOK FORMULAS — lead with feeling, not features:
• Chaos mirror:    "You run a real business. You just don't have a back office that shows it."
• Embarrassment:  "Sending a quote over text isn't unprofessional — it's costing you bookings."
• Tax season:     "When your accountant asks for your profit and loss... do you panic?"
• Before/after:   "Before: 12 DMs to confirm one party. After: client books and pays in 2 minutes."
• Relief:         "Imagine finishing a party weekend knowing every invoice, contract, and deposit is already handled."

PLATFORM: {platform} | Max: {char_limit} characters
HASHTAGS (Instagram/TikTok only): #PartyBizHub #KidsPartyBusiness #PartyBusiness #PartyPlanner

CRITICAL RULES:
- Start with the FEELING, not the feature
- Do NOT write about undercharging, pricing strategy, or starting a business from scratch
- Do NOT write coaching content — this is a software product post
- Features (booking page, quote builder, contracts, etc.) are mentioned to prove the transformation, not as the lead{kb_section}

OUTPUT: Return ONLY the post text, starting directly with the hook. No labels or explanations."""

    user_prompt = f"""Write a {platform} post following these exact instructions:\n\n{input_text}"""

    emit("script", "progress", f"Generating Party Biz Hub {platform} post...")

    try:
        response = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=800,
            temperature=0.75,
        )
        text = response.choices[0].message.content.strip()
        usage = response.usage
        result = {
            "text": text,
            "model": DEFAULT_MODEL,
            "tokens_in": usage.prompt_tokens if usage else 0,
            "tokens_out": usage.completion_tokens if usage else 0,
            "cost": _estimate_cost(usage.prompt_tokens, usage.completion_tokens) if usage else 0.0,
            "demo": False
        }
        emit("script", "progress", f"PBH script done! {result['tokens_out']} tokens.")
        return result
    except Exception as e:
        emit("script", "error", f"OpenRouter error: {str(e)}")
        raise


# ---------------------------------------------------------------------------
# generate_pbh_captions() — PBH platform captions (separate from KPPS)
# ---------------------------------------------------------------------------
def generate_pbh_captions(script_text, platforms=None, knowledge_base="", emit_event=None):
    """
    Generate per-platform captions for a PBH post.
    Separate from generate_captions() to avoid KPPS system prompt leaking.
    """
    emit = emit_event or (lambda *a, **kw: None)
    client = _get_client()
    platforms = platforms or ["tiktok", "instagram", "facebook", "youtube"]

    if not client:
        return {"captions": {p: script_text for p in platforms}, "demo": True, "cost": 0.0}

    platform_instructions = "\n".join([
        f"- {p.upper()}: {PLATFORM_IDEAL.get(p, 'Keep it concise and punchy.')}"
        for p in platforms
    ])

    system_prompt = f"""You are a social media copywriter adapting Party Biz Hub content for multiple platforms.

Party Biz Hub is a business management app for kids party businesses — booking pages, quote builder, contracts, invoicing, AI content, client dashboard.

AUDIENCE: Existing party business owners who need better systems. NOT beginners or people starting from scratch.

TONE: Confident, direct, practical. No fluff. No undercharging or pricing coaching content.

ADAPT the provided script for each platform below — keep the Party Biz Hub focus, adjust length and style:
{platform_instructions}

HASHTAGS (Instagram + TikTok only): #PartyBizHub #KidsPartyBusiness #PartyBusiness #PartyPlanner

OUTPUT: Valid flat JSON only. Keys = platform names. Values = ONE caption string per platform.
Example: {{"tiktok": "Book clients without the DM chaos...", "instagram": "..."}}
Return ONLY the JSON. No markdown, no explanations."""

    emit("caption", "progress", f"Writing PBH captions for {', '.join(platforms)}...")

    try:
        response = client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Adapt this Party Biz Hub script for each platform:\n\n{script_text}"}
            ],
            max_tokens=1200,
            temperature=0.7,
            timeout=45,
        )

        raw_text = response.choices[0].message.content.strip()
        usage = response.usage

        import re as _re
        raw_text = _re.sub(r"^```(?:json)?\s*", "", raw_text, flags=_re.MULTILINE)
        raw_text = _re.sub(r"\s*```\s*$", "", raw_text, flags=_re.MULTILINE)
        raw_text = raw_text.strip()

        try:
            captions = json.loads(raw_text)
        except json.JSONDecodeError:
            captions = {p: raw_text for p in platforms}

        result = {
            "captions": captions,
            "model": DEFAULT_MODEL,
            "tokens_in": usage.prompt_tokens if usage else 0,
            "tokens_out": usage.completion_tokens if usage else 0,
            "cost": _estimate_cost(usage.prompt_tokens, usage.completion_tokens) if usage else 0.0,
            "demo": False
        }
        emit("caption", "progress", f"Got PBH captions for {len(captions)} platforms!")
        return result

    except Exception as e:
        emit("caption", "error", f"OpenRouter error: {str(e)}")
        raise


# ---------------------------------------------------------------------------
# Cost estimation helper
# Gemini 2.5 Flash pricing (approximate via OpenRouter)
# ---------------------------------------------------------------------------
def _estimate_cost(tokens_in, tokens_out):
    """
    Estimate the cost of an OpenRouter API call.
    Gemini 2.5 Flash: ~$0.15/M input, ~$0.60/M output (via OpenRouter)
    """
    cost_in = (tokens_in / 1_000_000) * 0.15
    cost_out = (tokens_out / 1_000_000) * 0.60
    return round(cost_in + cost_out, 6)
