import logging
from flask import Blueprint, jsonify, request
from extensions import db
from models import Contact, log_activity

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
