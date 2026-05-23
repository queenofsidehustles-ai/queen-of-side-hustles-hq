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
def generate_script(article_text_or_idea, platform="instagram", input_type="idea",
                    content_type=None, emit_event=None):
    """
    Generate a social media post script from an article or idea.

    Args:
        article_text_or_idea: The scraped article text or raw idea string
        platform: Target platform (instagram, tiktok, linkedin, etc.)
        input_type: 'url' (article was scraped) or 'idea' (raw input)
        content_type: Optional post angle — party_highlight, party_tip, vendor_spotlight,
                      hook_post, call_to_action (from KPPS generate page)
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

    # Parse content_type if encoded in the input text as [KPPS_TYPE:xxx]
    import re as _re
    _type_match = _re.match(r'^\[KPPS_TYPE:(\w+)\]\s*', article_text_or_idea or '')
    if _type_match and not content_type:
        content_type = _type_match.group(1)
        article_text_or_idea = article_text_or_idea[_type_match.end():]

    # Map content_type to a specific angle directive for the AI
    _content_type_angles = {
        "party_highlight": "Show a real, joyful party moment. Let the scene speak — make the viewer feel the room. Subtly plant: 'What if YOU could get paid to create moments like this?'",
        "party_tip":       "Teach ONE practical tip that makes her look smart and feel capable. This is a Dreamer post — she's never thought about charging for this skill yet.",
        "vendor_spotlight":"Feature a specific party product, decoration, or service. Show how a regular person could offer this as a service and get paid.",
        "hook_post":       "Write ONLY a scroll-stopping hook — bold, provocative, identity-based. No soft sell. Just the pattern interrupt that makes her stop and say 'that's me.'",
        "call_to_action":  "Drive one clear action. Acknowledge her hesitation, tell her why now is different, and give her ONE low-friction step. Mention Kids Party Profit System only if it fits naturally.",
    }
    angle_directive = _content_type_angles.get(content_type or "", "")

    # ── System prompt — full KPPS avatar, biopsychology framework ──────────────
    system_prompt = f"""You are a biopsychology-driven content strategist for Monica Lewis — Kids Party Business Coach, Queen of Side Hustles (@kidspartybizcoach, partybusinesscoach.com).

BRAND MISSION: Help women discover that the parties they throw FOR FREE can become their business — a plug-and-play income that fits around their family.
Products: Kids Party Profit System ($497 Skool — complete business in a box) | Party Biz Hub software ($97/year)
Voice: empowering, warm, real-talk, boss-energy — speaks FROM lived experience, never preachy, NEVER salesy

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
THE WOMAN WE ARE WRITING FOR
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
She is a working woman — corporate job, stay-at-home mom, or somewhere between.
She throws beautiful parties for FREE because it's her gift — she has NO idea this could be a business.
She has tried other side hustles before — affiliate marketing, dropshipping, MLM — and failed.
She carries the shame of those failures quietly. She wonders if she's just "not a business person."
She watches other women post Stripe screenshots and feels a mix of awe and defeat: "Why not me?"
She is tired: tired of the 9-to-5, tired of watching her kids grow up while she's in meetings,
tired of trading her best hours for someone else's dream.
She does NOT want to quit her job overnight. She wants something that fits AROUND her life.
She has not connected the dots yet: her gift IS the business. She just needs the system.

WHAT EVERY POST MUST MAKE HER FEEL:
1. "She is talking DIRECTLY to me — she knows what my Monday morning looks like."
2. "I am not alone. Other women exactly like me are doing this."
3. "This time it's different — the system is already built. I'm not starting from scratch."
4. "I don't have to choose between my family and financial freedom."
5. "I'm already qualified. I've been practicing for free for years."

HER DAILY PAIN (rotate, never force all of them):
• Throwing gorgeous parties for free while vendors charge hundreds for less
• Watching women with less talent get more bookings and visibility
• Having a gift that nobody pays her for — yet
• Sunday scaries → Monday dread → Thursday counting down to the weekend
• Sitting in a meeting thinking "I should be running my own thing by now"
• Her kids growing up fast while she misses moments for work obligations
• Seeing someone else's "I quit my job today" post and feeling a gut punch
• Tried other businesses, failed, feels embarrassed to "try again"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SCROLL-STOPPING HOOK — the most important line
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
First 3 words carry 80% of the weight. Choose ONE formula:
• IDENTITY CALL-OUT:   "If you're a mom doing parties for free, read this."
• PAIN-FIRST:          "You've tried 3 side hustles this year. None of them paid you."
• SPECIFICITY SHOCK:   "She made $2,400 her first weekend. She used to do it for free."
• CURIOSITY GAP:       "The one reason your gift isn't paying you yet."
• COUNTER-INTUITIVE:   "Stop trying to find a business idea. You already have one."
• BEFORE/AFTER:        "6 months ago she had no clients. Now she has a waitlist."

WHAT KILLS A HOOK: "Today I want to share", "In this post I'll", "Tips for", vague positivity.

BIOPSYCHOLOGY FRAMEWORK — weave through every post naturally:
1. HOOK → Pattern interrupt: stop the scroll before the brain can skip
2. PAIN → Name the EXACT moment (not "stress" — "Sunday night dread", "watching her Stripe post")
3. AGITATE → What does staying stuck cost her emotionally — not just financially?
4. TRANSFORM → Paint the sensory win: the first booking text, the Stripe sound, dropping her kid at school AND running a business that same day
5. COMMUNITY → "Women exactly like you are doing this right now. You're not late."
6. IDENTITY → "You're not just good at parties. You're a CEO who hasn't been paid yet."
7. CTA → ONE action. Frictionless: "comment SYSTEM", "save this", "DM me READY"

Platform: {platform} | Max: {char_limit} characters
FORMATTING: 2-4 intentional emojis | short mobile paragraphs | ONE CTA only
HASHTAGS (Instagram/TikTok only): #KidsPartyProfitSystem #KidsPartyBusiness #SideHustleMom #MomBoss #PartyBusinessCoach

OUTPUT: Return ONLY the post text, starting directly with the hook. No labels. No preamble. Just the post."""

    # ── User prompt — differs by input_type ───────────────────────────────────
    angle_instruction = f"\n\nCONTENT ANGLE FOR THIS POST: {angle_directive}" if angle_directive else ""

    if input_type == "url":
        user_prompt = f"""Turn this article into a {platform} post for Monica's audience:{angle_instruction}

---
{article_text_or_idea[:4000]}
---

Apply the full psychology framework. Write in Monica's warm, boss-energy voice. Speak directly to the woman described in the system prompt."""
    else:
        hook_text = article_text_or_idea.strip()
        if hook_text:
            user_prompt = f"""Monica recorded a video. Here is what's on screen or what she said:{angle_instruction}

"{hook_text}"

Write a {platform} caption that:
1. OPENS with Monica's exact words or a very close variation — do NOT replace her hook
2. Builds from there: adds a relatable moment, a quick story, or "why this matters" for the woman described in the system prompt
3. Ends with ONE soft CTA that fits the energy — "comment SYSTEM", "save this", "follow for more"

CRITICAL: Do NOT add pricing or product pitches unless Monica's input already includes them. Match her energy, not a sales agenda. This caption should feel like a natural extension of what she said."""
        else:
            user_prompt = f"""Write a {platform} post for Monica's kids party business audience.{angle_instruction}

Speak directly to the woman described in the system prompt — the working mom who throws parties for free and doesn't know she could get paid for it.
Apply the full biopsychology framework. Make her feel SEEN. End with ONE low-friction CTA."""

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

BRAND MISSION: Help women discover their gift (parties they throw for FREE) is worth hundreds per booking — and give them a plug-and-play system to get there.
Products: Kids Party Profit System ($497 Skool — complete business in a box) | Party Biz Hub software ($97/year)
VOICE: Empowering, warm, real-talk, boss-energy — from lived experience, NEVER preachy, NEVER salesy

━━ THE WOMAN READING THIS CAPTION ━━
She is a working or stay-at-home mom throwing kids parties for FREE right now.
She has tried side hustles before and failed. She carries that shame quietly.
She watches other women post income screenshots and thinks "why not me?"
She is tired: tired of the 9-to-5, tired of missing family moments for work.
She wants a business that fits AROUND her life — not one that takes her away from it.
She has no idea her party-throwing gift is already a business. She just needs the system.

WHAT EACH CAPTION MUST DO: Make her feel seen. Not sold to. Seen.

━━ BUYER PSYCHOLOGY FRAMEWORK ━━
1. SCROLL-STOP HOOK — identity call-out, pain-first, or curiosity gap
2. PAIN MIRROR — name her EXACT internal dialogue ("you've been doing this for free for years")
3. POSSIBILITY BRIDGE — show the transformation is real: "women just like you did it"
4. IDENTITY UPGRADE — "You're not a hobbyist. You're a CEO who hasn't been paid yet."
5. LOW-FRICTION CTA — one action only: "comment SYSTEM", "save this", "DM me READY"

SCROLL-STOPPING HOOK FORMULAS:
• Identity call-out: "If you're throwing parties for free, read this."
• Pain-first:        "You've tried 3 side hustles this year. None of them paid you."
• Specificity shock: "She made $2,400 her first weekend — from parties she used to do for free."
• Curiosity gap:     "The one reason your gift isn't paying you yet."
• Counter-intuitive: "Stop looking for a business idea. You already have one."

BIOPSYCHOLOGY TRIGGERS:
- Dopamine: the first booking text, the Stripe notification, dropping her kid off AND running her business that day
- Oxytocin: "women exactly like you are doing this right now"
- Cortisol relief: name the Sunday dread, then immediately offer a path out
- Mirror neurons: tell a story she recognizes as her own

CRITICAL LENGTH RULES — follow exactly:
{platform_instructions}

HASHTAGS (Instagram + TikTok only): #KidsPartyProfitSystem #KidsPartyBusiness #SideHustleMom #MomBoss #PartyBusinessCoach

OUTPUT FORMAT: Valid flat JSON only.
Keys = platform names. Values = ONE caption string.
Example: {{"instagram": "If you've been doing parties for free... 🎉", "tiktok": "This mom made $2,400 her first weekend..."}}
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

PLATFORM-SPECIFIC CALL TO ACTION — use the exact CTA for the platform, no exceptions:
- TikTok:    "Comment PARTY BIZ for more details" + "DM me" + "Follow for more party biz tips" — NO external links, NO URLs (TikTok suppresses them)
- Instagram: "Link in bio → partybizhub.com"
- Facebook:  "Get started at partybizhub.com"
- YouTube:   "Link in the description — partybizhub.com"

IMPORTANT: Party Biz Hub does NOT have a free version. Never say "free trial", "try for free", or "start free". They visit the site and purchase directly.

CRITICAL RULES:
- Start with the PROBLEM, not a feature list
- Do NOT write about undercharging, pricing strategy, or starting a business from scratch
- Do NOT write coaching content — this is a software product post{kb_section}

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
