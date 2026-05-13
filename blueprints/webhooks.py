import json
import logging
from flask import Blueprint, jsonify, request
from extensions import db
from models import Contact, Deal, log_activity
from sqlalchemy import func

logger = logging.getLogger(__name__)

webhooks_bp = Blueprint("webhooks", __name__)


@webhooks_bp.route("/mailerlite", methods=["POST"])
def mailerlite_webhook():
    """
    Receives subscriber events from MailerLite.

    MailerLite POST body shape (simplified):
    {
        "events": [{"type": "subscriber.created", ...}],
        "subscriber": {
            "email": "jane@example.com",
            "name": "Jane Doe",
            "fields": {"name": "Jane", ...}
        }
    }

    We always return 200 so MailerLite does not endlessly retry.
    """
    try:
        data = request.get_json(silent=True) or {}

        # --- Extract subscriber info ---
        subscriber = data.get("subscriber", {})
        email = (subscriber.get("email") or "").strip().lower()

        if not email:
            logger.warning("MailerLite webhook: payload had no email — ignoring")
            return jsonify({"success": True, "skipped": "no email"})

        # Prefer the dedicated first-name field, fall back to full name, then email prefix
        fields = subscriber.get("fields") or {}
        first_name = (fields.get("name") or "").strip()
        full_name  = (subscriber.get("name") or "").strip()
        name = first_name or full_name or email.split("@")[0]

        # --- Upsert logic ---
        contact = Contact.query.filter(
            db.func.lower(Contact.email) == email
        ).first()

        if contact is None:
            # Brand-new subscriber — create a Contact
            contact = Contact(
                name=name,
                email=email,
                status="Checklist Downloaded",
                lead_source="MailerLite",
            )
            db.session.add(contact)
            db.session.flush()
            log_activity(
                "contact_created",
                f"MailerLite webhook created contact: {name} ({email})",
                contact_id=contact.id,
            )
            logger.info("MailerLite webhook: created contact %s", email)
        else:
            # Existing contact — only bump status if they're still at the entry level
            # so we never downgrade someone who has already progressed further
            if contact.status == "Lead":
                contact.status = "Checklist Downloaded"
                log_activity(
                    "contact_updated",
                    f"MailerLite webhook updated status to 'Checklist Downloaded' for {email}",
                    contact_id=contact.id,
                )
                logger.info("MailerLite webhook: updated status for existing contact %s", email)
            else:
                logger.info(
                    "MailerLite webhook: contact %s already at status '%s' — no change",
                    email,
                    contact.status,
                )

        db.session.commit()

    except Exception:
        # Log the error but still return 200 so MailerLite does not retry forever
        logger.exception("MailerLite webhook: unexpected error")
        db.session.rollback()

    return jsonify({"success": True})


@webhooks_bp.route("/stripe", methods=["POST"])
def stripe_webhook():
    """
    Handles Stripe checkout.session.completed events.
    Auto-creates or moves contact to 'Course Purchased' and sends welcome email.
    Webhook URL to paste in Stripe: https://your-railway-domain.up.railway.app/webhooks/stripe
    """
    try:
        data = request.get_json(silent=True) or {}
        event_type = data.get("type", "")

        if event_type != "checkout.session.completed":
            return jsonify({"success": True, "skipped": event_type})

        session_obj = data.get("data", {}).get("object", {})
        customer_details = session_obj.get("customer_details") or {}
        email = (session_obj.get("customer_email") or customer_details.get("email") or "").strip().lower()
        customer_name = (customer_details.get("name") or "").strip()
        amount = (session_obj.get("amount_total") or 0) / 100  # cents → dollars

        if not email:
            logger.warning("Stripe webhook: no email in payload")
            return jsonify({"success": True, "skipped": "no email"})

        contact = Contact.query.filter(func.lower(Contact.email) == email).first()

        if contact:
            if contact.status not in ("Course Purchased", "Hub Activated", "Active Subscriber", "VIP Client"):
                old_stage = contact.status
                contact.status = "Course Purchased"
                log_activity("contact_moved",
                    f"Stripe purchase ${amount:.0f}: moved {contact.name} from {old_stage} to Course Purchased",
                    contact_id=contact.id)
        else:
            contact = Contact(
                name=customer_name or email.split("@")[0],
                email=email,
                status="Course Purchased",
                lead_source="Stripe",
            )
            db.session.add(contact)
            db.session.flush()
            log_activity("contact_created",
                f"New Stripe customer: {email} (${amount:.0f})",
                contact_id=contact.id)

        # Create a Won deal to track the revenue
        if amount > 0:
            deal = Deal(
                title=f"Kids Party Profit System — {contact.name}",
                contact_id=contact.id,
                value=amount,
                stage="Won",
            )
            db.session.add(deal)

        db.session.commit()

        # Send purchase confirmation using the existing email template system
        try:
            from blueprints.email import send_trigger_email
            send_trigger_email("purchase_confirmation", contact)
        except Exception as e:
            logger.warning("Purchase confirmation email failed: %s", e)

    except Exception:
        logger.exception("Stripe webhook: unexpected error")
        db.session.rollback()

    return jsonify({"success": True})
