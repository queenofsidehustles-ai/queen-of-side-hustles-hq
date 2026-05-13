from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from extensions import db
from auth import login_required, check_credentials
from models import Contact, Deal, Note, ActivityLog, Purchase
from sqlalchemy import func
from datetime import date, timedelta

admin_bp = Blueprint("admin", __name__)


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("admin.dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if check_credentials(username, password):
            session["logged_in"] = True
            session.permanent = True
            next_url = request.args.get("next", url_for("admin.dashboard"))
            return redirect(next_url)
        else:
            flash("Invalid credentials. Please try again.", "danger")

    return render_template("admin/login.html")


@admin_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin.login"))


@admin_bp.route("/dashboard")
@login_required
def dashboard():
    active_stages = ["Checklist Downloaded", "Warming Up", "Lead", "Course Purchased",
                     "Hub Activated", "Active Subscriber"]
    stats = {
        "total_contacts": Contact.query.count(),
        "total_leads": Contact.query.filter(Contact.status.in_(active_stages)).count(),
        "pipeline_value": float(db.session.query(func.coalesce(func.sum(Deal.value), 0)).filter(
            Deal.stage.notin_(["Won", "Lost"])
        ).scalar()),
        "total_revenue": float(db.session.query(func.coalesce(func.sum(Deal.value), 0)).filter(
            Deal.stage == "Won"
        ).scalar()),
        "total_deals": Deal.query.count(),
        "won_deals": Deal.query.filter(Deal.stage == "Won").count(),
    }

    today = date.today()
    follow_ups = (Contact.query
                  .filter(Contact.follow_up_date <= today)
                  .order_by(Contact.follow_up_date.asc())
                  .limit(20).all())

    recent_activity = ActivityLog.query.order_by(ActivityLog.created_at.desc()).limit(10).all()

    from models import ContentItem
    content_total = ContentItem.query.count()
    content_ready = ContentItem.query.filter_by(status="ReadyToPost").count()
    content_published = ContentItem.query.filter_by(status="published").count()

    from datetime import datetime
    import json

    # Platform / lead source breakdown (real data)
    platform_rows = db.session.query(
        Contact.lead_source, func.count(Contact.id)
    ).group_by(Contact.lead_source).order_by(func.count(Contact.id).desc()).all()
    platform_breakdown_json = json.dumps([
        {"source": (row[0] or "Other"), "count": row[1]} for row in platform_rows
    ])

    # Real conversion funnel (all time)
    all_count = stats["total_contacts"]
    leads_count = Contact.query.filter(
        Contact.status.in_(["Checklist Downloaded", "Warming Up", "Lead"])
    ).count()
    converted_count = Contact.query.filter(
        Contact.status.in_(["Course Purchased", "Hub Activated", "Active Subscriber", "VIP Client"])
    ).count()
    funnel = {
        "all_contacts": all_count,
        "active_leads": leads_count,
        "converted": converted_count,
        "lead_rate": round(leads_count / all_count * 100) if all_count > 0 else 0,
        "convert_rate": round(converted_count / leads_count * 100) if leads_count > 0 else 0,
    }

    # Revenue by week — real Won deal data, last 13 weeks
    now_dt = datetime.utcnow()
    revenue_weeks_data = []
    for i in range(13):
        wk_start = now_dt - timedelta(weeks=12 - i)
        wk_end = wk_start + timedelta(weeks=1)
        total = db.session.query(func.coalesce(func.sum(Deal.value), 0)).filter(
            Deal.stage == "Won",
            Deal.created_at >= wk_start,
            Deal.created_at < wk_end,
        ).scalar()
        revenue_weeks_data.append({
            "label": f"{wk_start.month}/{wk_start.day}",
            "value": float(total or 0),
        })
    revenue_weeks_json = json.dumps(revenue_weeks_data)

    # Real Won deals for transactions panel
    won_rows = (
        db.session.query(Deal, Contact)
        .outerjoin(Contact, Deal.contact_id == Contact.id)
        .filter(Deal.stage == "Won")
        .order_by(Deal.created_at.desc())
        .limit(10)
        .all()
    )
    won_deals = []
    for deal, contact in won_rows:
        nm = (contact.name if contact else deal.title or "Unknown")
        won_deals.append({
            "name": nm,
            "initials": "".join(w[0].upper() for w in nm.split()[:2]) if nm else "?",
            "amount": f"{float(deal.value):,.2f}" if deal.value else "0.00",
            "date": deal.created_at.strftime("%b %d, %Y") if deal.created_at else "",
        })
    won_deals_json = json.dumps(won_deals)

    return render_template(
        "admin/dashboard.html",
        stats=stats,
        follow_ups=follow_ups,
        today=today,
        recent_activity=recent_activity,
        content_total=content_total,
        content_ready=content_ready,
        content_published=content_published,
        funnel=funnel,
        platform_breakdown_json=platform_breakdown_json,
        revenue_weeks_json=revenue_weeks_json,
        won_deals_json=won_deals_json,
    )


@admin_bp.route("/contacts")
@login_required
def contacts():
    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "").strip()

    query = Contact.query
    if q:
        search = f"%{q}%"
        query = query.filter(
            db.or_(
                Contact.name.ilike(search),
                Contact.email.ilike(search),
                Contact.company.ilike(search),
            )
        )
    if status_filter:
        query = query.filter(Contact.status == status_filter)

    contacts_list = query.order_by(Contact.created_at.desc()).all()
    return render_template("admin/contacts.html", contacts=contacts_list, q=q, status_filter=status_filter, today=date.today())


@admin_bp.route("/contacts/<int:contact_id>")
@login_required
def contact_detail(contact_id):
    contact = Contact.query.get_or_404(contact_id)
    activities = ActivityLog.query.filter_by(contact_id=contact_id).order_by(ActivityLog.created_at.desc()).limit(20).all()
    all_deals = Deal.query.filter_by(contact_id=contact_id).all()

    return render_template("admin/contact_detail.html", contact=contact, activities=activities, deals=all_deals, today=date.today())


@admin_bp.route("/settings")
@login_required
def settings():
    from models import Setting
    # Load all settings
    settings = {}
    for s in Setting.query.all():
        settings[s.key] = s.value
    return render_template("admin/settings.html", settings=settings)


@admin_bp.route("/settings", methods=["POST"])
@login_required
def save_settings():
    from models import Setting
    import os
    # List of setting keys to save
    keys = [
        "BUSINESS_NAME", "OPENROUTER_API_KEY", "FIRECRAWL_API_KEY",
        "KIE_AI_API_KEY", "GETLATE_API_KEY", "STRIPE_SECRET_KEY",
        "STRIPE_PUBLISHABLE_KEY", "RESEND_API_KEY", "RESEND_FROM_EMAIL",
        "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
        "R2_BUCKET_NAME", "R2_PUBLIC_URL",
    ]
    for key in keys:
        value = request.form.get(key, "").strip()
        if value:  # Only save non-empty values
            Setting.set(key, value)
            os.environ[key] = value  # Immediate pickup by services
    flash("Settings saved successfully!", "success")
    return redirect(url_for("admin.settings"))


@admin_bp.route("/deals")
@login_required
def deals():
    stages = ["Checklist Downloaded", "Warming Up", "Course Purchased", "Hub Activated", "Active Subscriber", "VIP Client", "Churned"]
    stage_filter = request.args.get("stage", "")
    search = request.args.get("q", "").strip()

    query = Deal.query
    if stage_filter:
        query = query.filter_by(stage=stage_filter)
    if search:
        query = query.filter(Deal.title.ilike(f"%{search}%"))

    all_deals = query.order_by(Deal.created_at.desc()).all()
    all_contacts = Contact.query.order_by(Contact.name).all()

    # Stage summary for filter badges
    stage_counts = {}
    for stage in stages:
        stage_counts[stage] = Deal.query.filter_by(stage=stage).count()

    pipeline_total = sum(float(d.value or 0) for d in Deal.query.filter(Deal.stage.notin_(["Active Subscriber", "VIP Client", "Churned"])).all())

    return render_template("admin/deals.html",
        deals=all_deals, stages=stages, contacts=all_contacts,
        stage_filter=stage_filter, search=search,
        stage_counts=stage_counts, pipeline_total=pipeline_total)


@admin_bp.route("/pages")
@login_required
def pages():
    """Website pages management — show/hide pages, funnel grouping."""
    website_pages = [
        {"name": "Landing Page", "path": "/lp", "active": True, "type": "funnel"},
        {"name": "Sales Page", "path": "/sales", "active": True, "type": "funnel"},
        {"name": "Thank You", "path": "/lp/thank-you", "active": True, "type": "funnel"},
        {"name": "Store", "path": "/products/store", "active": True, "type": "standalone"},
        {"name": "Public Booking", "path": "/bookings/book", "active": True, "type": "standalone"},
        {"name": "Onboarding Survey", "path": "/onboarding/", "active": True, "type": "standalone"},
    ]
    return render_template("admin/pages.html", pages=website_pages)


@admin_bp.route("/analytics")
@login_required
def analytics():
    """Umami-style analytics dashboard with demo data."""
    from models import PageView, Contact
    from datetime import datetime, timedelta
    import random

    # Generate demo analytics data
    now = datetime.utcnow()
    hours_data = []
    for i in range(24):
        h = now - timedelta(hours=23 - i)
        visitors = random.randint(5, 80)
        views = visitors + random.randint(10, 120)
        hours_data.append({
            "hour": h.strftime("%I %p"),
            "visitors": visitors,
            "views": views,
        })

    total_views = sum(h["views"] for h in hours_data)
    total_visitors = sum(h["visitors"] for h in hours_data)
    total_visits = int(total_visitors * 1.3)
    bounce_rate = random.randint(35, 65)
    avg_duration = f"{random.randint(1, 4)}m {random.randint(0, 59)}s"

    # Top pages
    top_pages = [
        {"page": "/", "views": random.randint(200, 500), "pct": 0},
        {"page": "/lp", "views": random.randint(100, 300), "pct": 0},
        {"page": "/sales", "views": random.randint(50, 200), "pct": 0},
        {"page": "/products/store", "views": random.randint(30, 150), "pct": 0},
        {"page": "/onboarding/", "views": random.randint(20, 100), "pct": 0},
        {"page": "/bookings/book", "views": random.randint(10, 80), "pct": 0},
    ]
    tp_total = sum(p["views"] for p in top_pages)
    for p in top_pages:
        p["pct"] = round(p["views"] / tp_total * 100) if tp_total else 0

    # Top referrers
    referrers = [
        {"source": "google.com", "views": random.randint(50, 200)},
        {"source": "facebook.com", "views": random.randint(30, 150)},
        {"source": "instagram.com", "views": random.randint(20, 100)},
        {"source": "direct", "views": random.randint(40, 180)},
        {"source": "tiktok.com", "views": random.randint(10, 60)},
    ]
    ref_total = sum(r["views"] for r in referrers)
    for r in referrers:
        r["pct"] = round(r["views"] / ref_total * 100) if ref_total else 0

    return render_template("admin/analytics.html",
        hours_data=hours_data, total_views=total_views,
        total_visitors=total_visitors, total_visits=total_visits,
        bounce_rate=bounce_rate, avg_duration=avg_duration,
        top_pages=top_pages, referrers=referrers)


@admin_bp.route("/quick-add-lead", methods=["POST"])
@login_required
def quick_add_lead():
    from models import log_activity
    import logging
    try:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({"error": "No data provided"}), 400

        name = (data.get("name") or "").strip()
        handle = (data.get("tiktok_handle") or "").strip().lstrip("@")
        if not name and not handle:
            return jsonify({"error": "Please provide a name or social handle"}), 400
        if not name:
            name = f"@{handle}"

        follow_up = data.get("follow_up_date") or None
        if follow_up and isinstance(follow_up, str):
            try:
                from datetime import date as _date
                follow_up = _date.fromisoformat(follow_up)
            except ValueError:
                follow_up = None

        platform = (data.get("platform") or "TikTok").strip()
        lead_source = f"{platform} DM" if platform not in ("Website", "Referral", "Event", "Other") else platform

        contact = Contact(
            name=name,
            tiktok_handle=handle or None,
            phone=data.get("phone", "").strip() or None,
            status=data.get("status", "Warming Up"),
            lead_source=lead_source,
            follow_up_date=follow_up,
            notes_quick=data.get("notes", "").strip() or None,
        )
        db.session.add(contact)
        db.session.flush()
        log_activity("contact_created", f"Quick-added {platform} lead: {contact.name}", contact_id=contact.id)

        deal_value = data.get("deal_value")
        if deal_value:
            try:
                deal_value = float(deal_value)
            except (ValueError, TypeError):
                deal_value = None
        if deal_value and deal_value > 0:
            deal = Deal(
                title=f"{platform} Interest — {contact.name}",
                contact_id=contact.id,
                value=deal_value,
                stage="Warming Up",
            )
            db.session.add(deal)
            log_activity("deal_created", f"Deal created for {contact.name}: ${deal_value:.0f}", contact_id=contact.id)

        db.session.commit()
        return jsonify({"ok": True, "contact_id": contact.id})

    except Exception as e:
        db.session.rollback()
        logging.exception("quick_add_lead error")
        return jsonify({"error": f"Server error: {str(e)}"}), 500


@admin_bp.route("/extract-contact-image", methods=["POST"])
@login_required
def extract_contact_image():
    import base64, json, re
    img = request.files.get("image")
    if not img:
        return jsonify({"error": "No image provided"}), 400

    b64 = base64.b64encode(img.read()).decode()
    mime = img.content_type or "image/jpeg"

    from services.openrouter import _get_client
    client = _get_client()
    if not client:
        return jsonify({"error": "OpenRouter API key not configured in Settings"}), 400

    try:
        resp = client.chat.completions.create(
            model="google/gemini-2.5-flash",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    {"type": "text", "text": (
                        "Look at this screenshot from a social media app or DM. "
                        "Extract contact info and return ONLY a JSON object with these fields (null if not found): "
                        "name (display name, not username), handle (username WITHOUT @ symbol), "
                        "platform (one of: TikTok, Instagram, Facebook, YouTube, LinkedIn), "
                        "notes (one sentence: what they asked or are interested in). "
                        'Example: {"name":"Sarah J","handle":"sarahparties","platform":"TikTok","notes":"Asked about pricing for balloon setups"}'
                    )}
                ]
            }],
            max_tokens=200,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/sync-mailerlite", methods=["POST"])
@login_required
def sync_mailerlite():
    from services.mailerlite import get_all_subscribers
    from models import log_activity

    subscribers = get_all_subscribers()
    created = 0
    skipped = 0

    for sub in subscribers:
        email = (sub.get("email") or "").strip().lower()
        if not email:
            continue
        fields = sub.get("fields") or {}
        name = (fields.get("name") or sub.get("name") or email.split("@")[0]).strip()

        existing = Contact.query.filter(
            func.lower(Contact.email) == email
        ).first()

        if existing is None:
            contact = Contact(
                name=name,
                email=email,
                status="Checklist Downloaded",
                lead_source="MailerLite",
            )
            db.session.add(contact)
            db.session.flush()
            log_activity("contact_created",
                         f"Imported from MailerLite: {name} ({email})",
                         contact_id=contact.id)
            created += 1
        else:
            skipped += 1

    db.session.commit()
    flash(f"MailerLite sync complete: {created} imported, {skipped} already existed.", "success")
    return redirect(url_for("admin.dashboard"))
