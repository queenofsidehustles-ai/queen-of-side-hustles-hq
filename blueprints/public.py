from flask import Blueprint, render_template, request, redirect, url_for, flash
from extensions import db
from models import Contact, Deal, log_activity

public_bp = Blueprint("public", __name__)


@public_bp.route("/")
def index():
    return redirect(url_for("public.landing"))


@public_bp.route("/lp")
def landing():
    return render_template("public/landing.html")


@public_bp.route("/lp/submit", methods=["POST"])
def landing_submit():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()

    if not name or not email:
        flash("Please provide your name and email.", "danger")
        return redirect(url_for("public.landing"))

    # Check if contact already exists
    existing = Contact.query.filter_by(email=email).first()
    if not existing:
        contact = Contact(
            name=name,
            email=email,
            status="Lead",
            lead_source="Website Form",
        )
        db.session.add(contact)
        log_activity("contact_created", f"New lead from landing page: {name}", contact_id=None)
        db.session.commit()
        # Update activity with actual contact_id
        from models import ActivityLog
        last = ActivityLog.query.order_by(ActivityLog.id.desc()).first()
        if last:
            last.contact_id = contact.id
            db.session.commit()

    return redirect(url_for("public.thank_you"))


@public_bp.route("/lp/thank-you")
def thank_you():
    return render_template("public/thank_you.html")


@public_bp.route("/free")
def free_checklist():
    return render_template("public/free.html")


@public_bp.route("/free/submit", methods=["POST"])
def free_submit():
    name  = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip()

    if not name or not email:
        flash("Please enter your name and email.", "danger")
        return redirect(url_for("public.free_checklist"))

    # Save to HQ database
    contact = Contact.query.filter_by(email=email).first()
    if not contact:
        contact = Contact(
            name=name,
            email=email,
            status="Lead",
            lead_source="Free Checklist",
        )
        db.session.add(contact)
        db.session.flush()

        # Create a Deal so they appear in the Sales Pipeline
        deal = Deal(
            title=f"{name} — Free Checklist",
            contact_id=contact.id,
            value=0,
            stage="Checklist Downloaded",
        )
        db.session.add(deal)
        log_activity("contact_created", f"New lead from /free checklist: {name}", contact_id=contact.id)
        db.session.commit()

    # Add to MailerLite
    try:
        import os
        from services.mailerlite import add_subscriber
        group = os.environ.get("MAILERLITE_GROUP", "Queen of Side Hustles")
        add_subscriber(email=email, name=name, group_name=group)
    except Exception:
        pass  # Never crash the form if MailerLite is down

    return redirect(url_for("public.free_thank_you"))


@public_bp.route("/free/thank-you")
def free_thank_you():
    return render_template("public/free_thank_you.html")


@public_bp.route("/sales")
def sales():
    return render_template("public/sales.html")
