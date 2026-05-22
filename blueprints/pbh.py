"""blueprints/pbh.py — Party Biz Hub Content Engine (page routes)"""

from flask import Blueprint, render_template
from auth import login_required
from models import ContentItem, PBHAsset

pbh_bp = Blueprint("pbh", __name__)


@pbh_bp.route("/")
@login_required
def index():
    items = (ContentItem.query
             .filter_by(brand="pbh")
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
