import os
import requests

MAILERLITE_API_KEY = os.environ.get("MAILERLITE_API_KEY", "")
BASE_URL = "https://connect.mailerlite.com/api"


def _headers():
    return {
        "Authorization": f"Bearer {MAILERLITE_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def add_subscriber(email, name="", group_name=None):
    """Add a subscriber to MailerLite. Optionally assign them to a group."""
    if not MAILERLITE_API_KEY:
        return {"ok": False, "error": "No MailerLite API key configured"}

    # Create or update the subscriber
    payload = {
        "email": email,
        "fields": {"name": name} if name else {},
        "status": "active",
    }

    resp = requests.post(f"{BASE_URL}/subscribers", json=payload, headers=_headers(), timeout=10)

    if resp.status_code not in (200, 201):
        return {"ok": False, "error": resp.text}

    subscriber_id = resp.json().get("data", {}).get("id")

    # Assign to group if requested
    if group_name and subscriber_id:
        group_id = _get_or_create_group(group_name)
        if group_id:
            requests.post(
                f"{BASE_URL}/subscribers/{subscriber_id}/groups/{group_id}",
                headers=_headers(),
                timeout=10,
            )

    return {"ok": True, "subscriber_id": subscriber_id}


def _get_or_create_group(group_name):
    """Find a MailerLite group by name, or create it if it doesn't exist."""
    resp = requests.get(f"{BASE_URL}/groups", headers=_headers(), timeout=10)
    if resp.status_code != 200:
        return None

    groups = resp.json().get("data", [])
    for g in groups:
        if g.get("name", "").lower() == group_name.lower():
            return g["id"]

    # Group not found — create it
    create_resp = requests.post(
        f"{BASE_URL}/groups",
        json={"name": group_name},
        headers=_headers(),
        timeout=10,
    )
    if create_resp.status_code in (200, 201):
        return create_resp.json().get("data", {}).get("id")

    return None


def get_subscriber_count():
    """Return total active subscriber count for the dashboard."""
    if not MAILERLITE_API_KEY:
        return 0
    resp = requests.get(f"{BASE_URL}/subscribers?filter[status]=active&limit=1", headers=_headers(), timeout=10)
    if resp.status_code == 200:
        return resp.json().get("total", 0)
    return 0
