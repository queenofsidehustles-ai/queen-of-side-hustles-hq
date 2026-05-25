"""blueprints/pbh.py — Party Biz Hub Content Engine (page routes)"""

from flask import Blueprint, render_template
from auth import login_required
from models import ContentItem, PBHAsset, Setting

pbh_bp = Blueprint("pbh", __name__)


@pbh_bp.route("/")
@login_required
def index():
    items = (ContentItem.query
             .filter(ContentItem.brand.in_(["pbh", "pbh_user"]))
             .order_by(ContentItem.created_at.desc())
             .all())
    return render_template("pbh/index.html", items=items)


@pbh_bp.route("/generate")
@login_required
def generate():
    assets = PBHAsset.query.order_by(PBHAsset.created_at.desc()).all()
    return render_template("pbh/generate.html", assets=assets)


@pbh_bp.route("/assets")
@login_required
def assets():
    assets = PBHAsset.query.order_by(PBHAsset.created_at.desc()).all()
    return render_template("pbh/assets.html", assets=assets)


@pbh_bp.route("/studio")
@login_required
def studio():
    """Content creation studio for party biz owners — their own social media."""
    assets = PBHAsset.query.order_by(PBHAsset.created_at.desc()).all()
    settings = {
        "business_name": Setting.get("pbh_business_name", ""),
        "business_type": Setting.get("pbh_business_type", ""),
        "has_elevenlabs": bool(Setting.get("pbh_elevenlabs_key")),
    }
    return render_template("pbh/studio.html", assets=[a.to_dict() for a in assets], settings=settings)


@pbh_bp.route("/settings")
@login_required
def settings():
    """Business profile + API keys for PBH content studio."""
    return render_template("pbh/settings.html",
        business_name  = Setting.get("pbh_business_name", ""),
        business_type  = Setting.get("pbh_business_type", ""),
        elevenlabs_key = Setting.get("pbh_elevenlabs_key", ""),
        voice_id       = Setting.get("pbh_voice_id", ""),
    )
