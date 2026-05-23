"""blueprints/pbh_dashboard.py — Party Biz Hub Business Dashboard"""

import os
import json
from datetime import datetime, timedelta
from flask import Blueprint, render_template, jsonify
from auth import login_required
from models import PBHKnowledge

pbh_dashboard_bp = Blueprint("pbh_dashboard", __name__)

STARTER_PRICE = 27
PRO_PRICE     = 47

# ── Stripe helpers ────────────────────────────────────────────────────────────

def _stripe_get(path, params=None):
    import urllib.request, urllib.parse
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key:
        return None
    url = f"https://api.stripe.com/v1{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    import base64
    creds = base64.b64encode(f"{key}:".encode()).decode()
    req.add_header("Authorization", f"Basic {creds}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _get_pbh_subscriptions():
    """Fetch all active PBH subscriptions from Stripe."""
    result = _stripe_get("/subscriptions", {
        "status": "active",
        "limit":  100,
        "expand[]": "data.customer",
    })
    if not result:
        return []
    subs = result.get("data", [])
    # PBH subs are recurring ($27 or $47) — filter out any non-PBH amounts
    pbh = []
    for s in subs:
        amount = 0
        for item in s.get("items", {}).get("data", []):
            amount += item.get("price", {}).get("unit_amount", 0) // 100
        if amount in (STARTER_PRICE, PRO_PRICE):
            s["_pbh_amount"] = amount
            pbh.append(s)
    return pbh


def _get_stripe_stats():
    subs = _get_pbh_subscriptions()

    starter   = [s for s in subs if s.get("_pbh_amount") == STARTER_PRICE]
    pro       = [s for s in subs if s.get("_pbh_amount") == PRO_PRICE]
    mrr       = len(starter) * STARTER_PRICE + len(pro) * PRO_PRICE

    now       = datetime.utcnow()
    month_ago = now - timedelta(days=30)
    week_ago  = now - timedelta(days=7)

    new_month = [s for s in subs if datetime.utcfromtimestamp(s["created"]) > month_ago]
    new_week  = [s for s in subs if datetime.utcfromtimestamp(s["created"]) > week_ago]

    # Recent 10 signups
    recent = sorted(subs, key=lambda s: s["created"], reverse=True)[:10]
    recent_list = []
    for s in recent:
        cust = s.get("customer") or {}
        name  = cust.get("name") or cust.get("email", "Unknown") if isinstance(cust, dict) else "Unknown"
        email = cust.get("email", "") if isinstance(cust, dict) else ""
        recent_list.append({
            "name":    name,
            "email":   email,
            "plan":    "Pro" if s.get("_pbh_amount") == PRO_PRICE else "Starter",
            "created": datetime.utcfromtimestamp(s["created"]).strftime("%b %d, %Y"),
        })

    return {
        "mrr":        mrr,
        "total":      len(subs),
        "starter":    len(starter),
        "pro":        len(pro),
        "new_month":  len(new_month),
        "new_week":   len(new_week),
        "recent":     recent_list,
    }


# ── MailerLite helpers ────────────────────────────────────────────────────────

def _get_mailerlite_stats():
    key      = os.getenv("MAILER_LITE_API_KEY", "")
    group_id = "188117314058585239"  # Party Biz Hub — Quiz Leads
    if not key:
        return {"quiz_leads": 0, "sources": {}}

    import urllib.request
    url = f"https://connect.mailerlite.com/api/groups/{group_id}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        group = data.get("data", {})
        active = group.get("active_count", 0)
    except Exception:
        active = 0

    # Pull subscribers with pbh_source field to build source breakdown
    sources = {}
    try:
        url2 = f"https://connect.mailerlite.com/api/subscribers?filter[group_id]={group_id}&limit=100"
        req2 = urllib.request.Request(url2)
        req2.add_header("Authorization", f"Bearer {key}")
        req2.add_header("Accept", "application/json")
        with urllib.request.urlopen(req2, timeout=10) as r2:
            data2 = json.loads(r2.read())
        for sub in data2.get("data", []):
            src = (sub.get("fields") or {}).get("pbh_source") or "Direct"
            sources[src] = sources.get(src, 0) + 1
    except Exception:
        pass

    return {"quiz_leads": active, "sources": sources}


# ── Routes ────────────────────────────────────────────────────────────────────

@pbh_dashboard_bp.route("/")
@login_required
def index():
    return render_template("pbh/dashboard.html")


@pbh_dashboard_bp.route("/stats")
@login_required
def stats():
    stripe = _get_stripe_stats()
    ml     = _get_mailerlite_stats()

    # Conversion rate: quiz leads who became paid
    quiz_leads  = ml["quiz_leads"]
    paid        = stripe["total"]
    conv_rate   = round((paid / quiz_leads * 100), 1) if quiz_leads > 0 else 0

    return jsonify({
        "stripe":      stripe,
        "mailerlite":  ml,
        "conv_rate":   conv_rate,
        "quiz_leads":  quiz_leads,
    })


@pbh_dashboard_bp.route("/knowledge")
@login_required
def knowledge():
    content = PBHKnowledge.get()
    return render_template("pbh/knowledge.html", content=content)
