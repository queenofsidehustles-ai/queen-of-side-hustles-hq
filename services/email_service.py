import os
import requests
import logging

logger = logging.getLogger(__name__)


def send_welcome_email(to_email: str, name: str) -> bool:
    api_key = os.getenv("RESEND_API_KEY", "")
    from_email = os.getenv("RESEND_FROM_EMAIL", "")

    if not api_key or not from_email:
        logger.warning("send_welcome_email: RESEND_API_KEY or RESEND_FROM_EMAIL not set")
        return False

    first_name = (name or "").split()[0] if name else "there"

    html = f"""
<!DOCTYPE html>
<html>
<body style="font-family: Georgia, serif; max-width: 560px; margin: 0 auto; padding: 32px 24px; background: #fff; color: #1a1a2e;">
  <h2 style="color: #6b21a8; margin-bottom: 8px;">Welcome to the Kids Party Profit System! 🎉</h2>
  <p style="font-size: 1rem; line-height: 1.6;">Hi {first_name},</p>
  <p style="font-size: 1rem; line-height: 1.6;">
    You just made one of the best investments in your future. I'm so proud of you for saying yes to yourself!
  </p>
  <p style="font-size: 1rem; line-height: 1.6;"><strong>Here's what to do right now:</strong></p>
  <ol style="font-size: 1rem; line-height: 2;">
    <li>Log into your Skool community — your login is the email you used to purchase</li>
    <li>Head to <strong>Module 1</strong> and download your Quick-Start Checklist</li>
    <li>Introduce yourself in <strong>#introductions</strong> — say hi, I read every single one</li>
  </ol>
  <div style="background: #f5f0ff; border-left: 4px solid #6b21a8; padding: 16px 20px; margin: 24px 0; border-radius: 0 8px 8px 0;">
    <p style="margin: 0; font-size: 0.9375rem; color: #4c1d95; font-style: italic;">
      "You're not just starting a business — you're building something your kids will be proud of."
    </p>
  </div>
  <p style="font-size: 1rem; line-height: 1.6;">
    Questions? Reply to this email. I read every message personally.
  </p>
  <p style="font-size: 1rem; line-height: 1.6;">
    Let's build your empire,<br>
    <strong>Monica Lewis</strong><br>
    <span style="color: #6b21a8;">Queen of Side Hustles | Kids Party Business Coach</span>
  </p>
</body>
</html>
"""

    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_email,
                "to": [to_email],
                "subject": f"Welcome to Kids Party Profit System, {first_name}! 🎉",
                "html": html,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info("Welcome email sent to %s", to_email)
            return True
        else:
            logger.warning("Resend returned %s: %s", resp.status_code, resp.text)
            return False
    except Exception as e:
        logger.error("send_welcome_email exception: %s", e)
        return False
