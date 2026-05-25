"""
PBH Content Studio — UX Audit
Goal: simulate a brand-new party biz owner going through PBH for the first time.
Checks for confusion points, missing guidance, overwhelming UI, and broken flows.
Run: pytest tests/test_pbh_ux_audit.py -v -s
"""
import re
import time
import pytest


# ── Shared helpers ─────────────────────────────────────────────────────────

def login(page, base_url):
    page.goto(f"{base_url}/admin/login")
    page.fill("input[name='username']", "admin")
    page.fill("input[name='password']", "testpass123")
    page.click("button[type='submit']")
    page.wait_for_url(re.compile(r"/admin/dashboard"), timeout=8000)
    return page


def section(title):
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


def check(label, passed, note=""):
    status = "✅ PASS" if passed else "❌ FAIL"
    suffix = f"  → {note}" if note else ""
    print(f"  {status}  {label}{suffix}")
    return passed


# ── AUDIT 1: PBH Settings (First-time setup) ──────────────────────────────

def test_pbh_settings_first_time_experience(page, live_server):
    """
    New user lands on PBH settings for first time.
    Goal: Can they understand what to do in <30 seconds?
    """
    section("AUDIT: PBH Settings — First-Time Setup")
    login(page, live_server)
    page.goto(f"{live_server}/pbh/settings")
    page.wait_for_load_state("networkidle")

    issues = []

    # 1. Is there a clear page title / purpose statement?
    body_text = page.locator("body").inner_text()
    has_purpose = any(kw in body_text.lower() for kw in ["business name", "set up", "profile", "voice"])
    if not check("Page has clear purpose (business setup)", has_purpose):
        issues.append("No clear purpose statement visible on settings page")

    # 2. Is there a numbered step or visual guide?
    has_steps = bool(page.locator("text=/step|1 —|2 —|first|start/i").count())
    check("Has step-by-step structure or numbering", has_steps,
          note="" if has_steps else "No numbered steps found — could feel like a wall of forms")

    # 3. Does the Business Name field have a helpful placeholder?
    name_input = page.locator("input[placeholder*='Bella'], input[placeholder*='business'], input[placeholder*='name']").first
    has_name_placeholder = name_input.count() > 0
    check("Business Name field has example placeholder", has_name_placeholder,
          note="" if has_name_placeholder else "No placeholder example — new users won't know what to type")

    # 4. Is 'Go to Studio' CTA visible without scrolling?
    studio_link = page.locator("a[href*='/pbh/studio'], a:has-text('Studio')")
    has_studio_cta = studio_link.count() > 0
    check("'Go to Studio' CTA is visible", has_studio_cta)

    # 5. ElevenLabs — is the optional label clear?
    has_optional_label = "optional" in body_text.lower()
    check("ElevenLabs section clearly marked as optional", has_optional_label,
          note="" if has_optional_label else "Nothing says ElevenLabs is optional — new users may think they need it to continue")

    # 6. Is the save button obvious and accessible?
    save_btn = page.locator("button:has-text('Save')").first
    has_save = save_btn.count() > 0
    check("Save button is present", has_save)

    # 7. Count form fields — is it overwhelming?
    fields = page.locator("input, select, textarea").count()
    manageable = fields <= 6
    check(f"Form field count manageable ({fields} fields)", manageable,
          note="" if manageable else f"{fields} fields visible — may overwhelm new users")

    # 8. Any visible error states or broken elements?
    error_texts = page.locator("text=/error|something went wrong|undefined|null/i").count()
    check("No visible error/broken text on page", error_texts == 0,
          note="" if error_texts == 0 else f"Found {error_texts} error-like text(s) on page")

    print(f"\n  Settings page issues to fix: {len(issues)}")


# ── AUDIT 2: PBH Studio — New User Flow ───────────────────────────────────

def test_pbh_studio_new_user_flow(page, live_server):
    """
    New user hits PBH Studio.
    Can they understand what to do in <60 seconds without reading a manual?
    """
    section("AUDIT: PBH Studio — New User First Impression")
    login(page, live_server)
    page.goto(f"{live_server}/pbh/studio")
    page.wait_for_load_state("networkidle")

    body_text = page.locator("body").inner_text()

    # 1. Is there a setup reminder if settings not filled?
    has_setup_banner = bool(page.locator("[style*='warning'], .alert, text=/set up|settings|business name/i").count())
    check("Shows setup reminder if no business configured", has_setup_banner,
          note="New users benefit from a 'finish your setup first' nudge")

    # 2. Are the 5 steps clearly visible and numbered?
    has_step1 = bool(page.locator("text=/step 1|step1/i").count())
    has_step2 = bool(page.locator("text=/step 2|step2/i").count())
    has_step3 = bool(page.locator("text=/step 3|step3/i").count())
    check("Step 1 is labeled", has_step1)
    check("Step 2 is labeled", has_step2)
    check("Step 3 is labeled", has_step3)

    # 3. Content type cards — are they understandable for a non-techy party biz owner?
    confusing_labels = ["JSON", "API", "webhook", "payload", "endpoint"]
    has_confusing = any(c.lower() in body_text.lower() for c in confusing_labels)
    check("No technical jargon in content type labels", not has_confusing,
          note="" if not has_confusing else "Found technical terms that may confuse party biz owners")

    # 4. Platform selector visible and labeled?
    platform_btns = page.locator("button:has-text('TikTok'), button:has-text('Instagram'), button:has-text('Facebook')")
    has_platforms = platform_btns.count() >= 2
    check("Platform selector (TikTok/Instagram/Facebook) is visible", has_platforms)

    # 5. Upload section — is it labeled clearly?
    has_upload = bool(page.locator("input[type='file']").count())
    check("File upload input is present (Step 3)", has_upload)

    # 6. The "optional" label for video — does it communicate clearly?
    has_optional = "optional" in body_text.lower()
    check("Something is labeled as optional (reduces friction)", has_optional)

    # 7. Generate / Create button — is it prominent and action-oriented?
    create_btn = page.locator("button:has-text('Create'), button:has-text('Generate')")
    has_create = create_btn.count() > 0
    check("'Create My Post' button is present", has_create)

    if has_create:
        # Check it's big/prominent — look for inline styles suggesting size
        btn_html = create_btn.first.evaluate("el => el.outerHTML")
        is_big = "1rem" in btn_html or "1.0rem" in btn_html or "width:100%" in btn_html
        check("Create button is full-width / prominent", is_big)

    # 8. Result panel — is the idle state helpful?
    idle_text = page.locator("text=/appears here|fill out|create my post/i")
    has_idle_guide = idle_text.count() > 0
    check("Right panel shows helpful idle state (not just blank)", has_idle_guide)

    # 9. Count interactive elements — is it overwhelming?
    interactive = page.locator("button, input, select, textarea").count()
    manageable = interactive <= 20
    check(f"Interactive element count is manageable ({interactive} elements)", manageable,
          note="" if manageable else f"{interactive} interactive elements — likely overwhelming")

    # 10. Mic button visible with clear label?
    mic_btn = page.locator("button[title*='mic'], button[title*='Speak'], button:has-text('Speak it')")
    has_mic = mic_btn.count() > 0
    check("Mic / voice input button is visible with label", has_mic)


# ── AUDIT 3: PBH Queue (the main index) ───────────────────────────────────

def test_pbh_queue_empty_state(page, live_server):
    """
    New user with no content yet — does the queue feel useful or dead?
    """
    section("AUDIT: PBH Queue — Empty State Experience")
    login(page, live_server)
    page.goto(f"{live_server}/pbh/")
    page.wait_for_load_state("networkidle")

    body_text = page.locator("body").inner_text()

    # 1. Does the empty state guide the user to take action?
    has_empty_cta = bool(page.locator("text=/create|make your first|get started|studio/i").count())
    check("Empty queue shows actionable CTA (not just blank)", has_empty_cta,
          note="" if has_empty_cta else "Blank empty state — new users don't know what to do next")

    # 2. Is there a visible link to the Studio?
    studio_link = page.locator("a[href*='/pbh/studio']")
    has_studio_link = studio_link.count() > 0
    check("Link to Studio is visible from Queue page", has_studio_link,
          note="" if has_studio_link else "No path to Studio from Queue — users are stuck")

    # 3. Page has a clear title/purpose?
    has_title = bool(page.locator("h1, [class*='page-title'], text=/queue|content|posts/i").count())
    check("Page has clear title or section label", has_title)

    # 4. No broken/undefined text
    bad = page.locator("text=/undefined|null|NaN|\[object/i").count()
    check("No broken template output (undefined/null)", bad == 0,
          note="" if bad == 0 else f"Found {bad} broken template value(s)")


# ── AUDIT 4: PBH Assets Library ───────────────────────────────────────────

def test_pbh_assets_library(page, live_server):
    """
    Asset library — does a new user understand what this is for?
    """
    section("AUDIT: PBH Asset Library")
    login(page, live_server)
    page.goto(f"{live_server}/pbh/assets")
    page.wait_for_load_state("networkidle")

    body_text = page.locator("body").inner_text()

    # 1. Clear purpose label?
    has_purpose = any(kw in body_text.lower() for kw in ["library", "clips", "photos", "assets", "upload"])
    check("Page communicates purpose (library of clips/photos)", has_purpose)

    # 2. Empty state guidance?
    has_empty_guide = bool(page.locator("text=/upload|add your first|no clips|no assets/i").count())
    check("Empty state is informative (not just blank)", has_empty_guide)

    # 3. Upload button present?
    upload_trigger = page.locator("button:has-text('Upload'), input[type='file'], a:has-text('Upload')")
    has_upload = upload_trigger.count() > 0
    check("Upload action is visible", has_upload)


# ── AUDIT 5: Navigation — Can they find anything? ─────────────────────────

def test_pbh_sidebar_navigation(page, live_server):
    """
    Navigation audit — is the sidebar clear and logical for a party biz owner?
    """
    section("AUDIT: Sidebar Navigation — PBH Section")
    login(page, live_server)
    page.goto(f"{live_server}/pbh/studio")
    page.wait_for_load_state("networkidle")

    # 1. PBH nav links visible in sidebar?
    nav_links = {
        "Studio": page.locator("a[href*='/pbh/studio']").count(),
        "Settings": page.locator("a[href*='/pbh/settings']").count(),
        "Queue/Library": page.locator("a[href*='/pbh/']").count(),
    }
    for label, count in nav_links.items():
        check(f"Sidebar has '{label}' link", count > 0)

    # 2. Active state — does current page show as selected?
    # Look for active/current class on nav items
    active = page.locator("[class*='active'], [aria-current='page'], [style*='font-weight:700']")
    has_active = active.count() > 0
    check("Current page is visually highlighted in nav", has_active,
          note="" if has_active else "No active state in nav — users can't tell where they are")

    # 3. Logo/home link present?
    home_link = page.locator("a[href='/'], a[href*='dashboard']").first
    check("Home / dashboard link accessible", home_link.count() > 0)


# ── AUDIT 6: Mobile-friendliness check (viewport test) ────────────────────

def test_pbh_studio_mobile_viewport(page, live_server):
    """
    Many party biz owners will use this on mobile.
    Check if the studio layout holds up at 390px width.
    """
    section("AUDIT: PBH Studio — Mobile Viewport (390px)")
    login(page, live_server)

    # Switch to mobile viewport
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{live_server}/pbh/studio")
    page.wait_for_load_state("networkidle")

    # 1. Does the page load without horizontal scroll?
    scroll_width = page.evaluate("document.documentElement.scrollWidth")
    viewport_width = page.evaluate("window.innerWidth")
    no_hscroll = scroll_width <= viewport_width + 10  # 10px tolerance
    check(f"No horizontal scroll on mobile ({scroll_width}px content / {viewport_width}px screen)", no_hscroll,
          note="" if no_hscroll else "Page wider than screen — users must scroll sideways (very bad UX)")

    # 2. Create button still visible?
    create_btn = page.locator("button:has-text('Create'), button:has-text('Generate')")
    check("'Create My Post' button visible on mobile", create_btn.count() > 0 and create_btn.first.is_visible())

    # 3. Content type cards — do they stack or go off screen?
    cards = page.locator("div[style*='grid']").first
    if cards.count() > 0:
        box = cards.bounding_box()
        fits = box is None or box["width"] <= 400
        check("Content type grid fits on mobile screen", fits,
              note="" if fits else "Grid is too wide for mobile — cards may be cut off")
    else:
        print("  ⚠️  SKIP  Could not measure grid width on mobile")

    # Reset to desktop
    page.set_viewport_size({"width": 1280, "height": 800})


# ── SUMMARY PRINTER ────────────────────────────────────────────────────────

def test_zzz_print_summary(page, live_server):
    """Prints a final summary — always runs last (zzz prefix sorts it last)."""
    section("UX AUDIT COMPLETE — Review findings above")
    print("""
  Priority fixes to review:
  1. Empty states — do they guide the user to action?
  2. Optional labels — reduce setup anxiety
  3. Mobile layout — many party biz owners are on phones
  4. Active nav state — help users know where they are
  5. First-session 'aha moment' — what gets them to create post #1?
    """)
    assert True  # always passes
