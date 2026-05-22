import os
from flask import Flask, session
from dotenv import load_dotenv

from extensions import db

load_dotenv()


def create_app():
    app = Flask(__name__)

    # --- Database ---
    database_url = os.environ.get("DATABASE_URL", "sqlite:///app.db")
    # Fix legacy postgres:// URLs (Railway still emits these)
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "connect_args": {"connect_timeout": 10},
    }

    # --- Security ---
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-in-production")
    app.config["ADMIN_USER"] = os.environ.get("ADMIN_USER", "admin")
    app.config["ADMIN_PASS"] = os.environ.get("ADMIN_PASS", "admin")

    # --- Branding ---
    app.config["BUSINESS_NAME"] = os.environ.get("BUSINESS_NAME", "All-in-One Business App")

    # --- Feature Toggles ---
    app.config["FEATURE_PRODUCTS"]  = os.environ.get("FEATURE_PRODUCTS",  "true").lower() == "true"
    app.config["FEATURE_CLIENTS"]   = os.environ.get("FEATURE_CLIENTS",   "true").lower() == "true"
    app.config["FEATURE_TASKS"]     = os.environ.get("FEATURE_TASKS",     "true").lower() == "true"
    app.config["FEATURE_EMAIL"]     = os.environ.get("FEATURE_EMAIL",     "true").lower() == "true"
    app.config["FEATURE_ANALYTICS"] = os.environ.get("FEATURE_ANALYTICS", "true").lower() == "true"
    app.config["FEATURE_BOOKINGS"]  = os.environ.get("FEATURE_BOOKINGS",  "true").lower() == "true"

    # --- Jackie AI / OpenAI ---
    app.config["OPENAI_API_KEY"]    = os.environ.get("OPENAI_API_KEY", "")
    app.config["CHAT_PROVIDER"]     = os.environ.get("CHAT_PROVIDER", "openrouter")
    app.config["OPENAI_CHAT_MODEL"] = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")

    # --- Umami Analytics ---
    app.config["UMAMI_WEBSITE_ID"]  = os.environ.get("UMAMI_WEBSITE_ID", "")
    app.config["UMAMI_SCRIPT_URL"]  = os.environ.get("UMAMI_SCRIPT_URL", "https://cloud.umami.is/script.js")

    # --- Zernio (social media publishing, formerly GetLate) ---
    app.config["ZERNIO_API_KEY"]    = os.environ.get("ZERNIO_API_KEY", os.environ.get("GETLATE_API_KEY", ""))

    # --- CRM Integration Keys ---
    app.config["STRIPE_CHECKOUT_URL"]   = os.environ.get("STRIPE_CHECKOUT_URL", "")
    app.config["LEAD_MAGNET_URL"]       = os.environ.get("LEAD_MAGNET_URL", "")
    app.config["OPENROUTER_API_KEY"]    = os.environ.get("OPENROUTER_API_KEY", "")

    # --- Content Automation Keys ---
    app.config["FIRECRAWL_API_KEY"]     = os.environ.get("FIRECRAWL_API_KEY", "")
    app.config["KIE_AI_API_KEY"]        = os.environ.get("KIE_AI_API_KEY", "")
    app.config["GETLATE_API_KEY"]       = os.environ.get("GETLATE_API_KEY", "")

    # --- Stripe ---
    app.config["STRIPE_SECRET_KEY"]      = os.environ.get("STRIPE_SECRET_KEY", "")
    app.config["STRIPE_PUBLISHABLE_KEY"] = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")

    # --- Resend (email) ---
    app.config["RESEND_API_KEY"]    = os.environ.get("RESEND_API_KEY", "")
    app.config["RESEND_FROM_EMAIL"] = os.environ.get("RESEND_FROM_EMAIL", "")

    # --- Cloudflare R2 Storage ---
    app.config["R2_ACCOUNT_ID"]        = os.environ.get("R2_ACCOUNT_ID", "")
    app.config["R2_ACCESS_KEY_ID"]     = os.environ.get("R2_ACCESS_KEY_ID", "")
    app.config["R2_SECRET_ACCESS_KEY"] = os.environ.get("R2_SECRET_ACCESS_KEY", "")
    app.config["R2_BUCKET_NAME"]       = os.environ.get("R2_BUCKET_NAME", "")
    app.config["R2_PUBLIC_URL"]        = os.environ.get("R2_PUBLIC_URL", "")

    # --- Init extensions ---
    db.init_app(app)

    # --- Register blueprints ---
    from blueprints.public import public_bp
    from blueprints.admin import admin_bp
    from blueprints.api import api_bp
    from blueprints.content import content_bp
    from blueprints.content_api import content_api_bp
    from blueprints.help import help_bp

    app.register_blueprint(public_bp,     url_prefix="/")
    app.register_blueprint(admin_bp,      url_prefix="/admin")
    app.register_blueprint(api_bp,        url_prefix="/api")
    app.register_blueprint(content_bp,    url_prefix="/content")
    app.register_blueprint(content_api_bp, url_prefix="/content/api")
    app.register_blueprint(help_bp,       url_prefix="/help")

    from blueprints.library import library_bp
    app.register_blueprint(library_bp,   url_prefix="/admin/library")

    from blueprints.webhooks import webhooks_bp
    app.register_blueprint(webhooks_bp,  url_prefix="/webhooks")

    from blueprints.jackie import jackie_bp
    app.register_blueprint(jackie_bp,    url_prefix="/jackie")

    from blueprints.onboarding import onboarding_bp
    app.register_blueprint(onboarding_bp, url_prefix="/onboarding")

    from blueprints.pbh import pbh_bp
    from blueprints.pbh_api import pbh_api_bp
    from blueprints.pbh_dashboard import pbh_dashboard_bp
    app.register_blueprint(pbh_bp,           url_prefix="/pbh")
    app.register_blueprint(pbh_api_bp,       url_prefix="/pbh/api")
    app.register_blueprint(pbh_dashboard_bp, url_prefix="/pbh/dashboard")

    if app.config["FEATURE_BOOKINGS"]:
        from blueprints.bookings import bookings_bp
        app.register_blueprint(bookings_bp, url_prefix="/bookings")

    if app.config["FEATURE_PRODUCTS"]:
        from blueprints.products import products_bp
        app.register_blueprint(products_bp, url_prefix="/products")

    if app.config["FEATURE_CLIENTS"]:
        from blueprints.clients import clients_bp
        app.register_blueprint(clients_bp, url_prefix="/clients")

    if app.config["FEATURE_TASKS"]:
        from blueprints.tasks import tasks_bp
        app.register_blueprint(tasks_bp, url_prefix="/tasks")

    if app.config["FEATURE_EMAIL"]:
        from blueprints.email import email_bp
        app.register_blueprint(email_bp, url_prefix="/email")

    # --- Context processor ---
    @app.context_processor
    def inject_globals():
        return {
            "business_name": app.config["BUSINESS_NAME"],
            "stripe_checkout_url": app.config["STRIPE_CHECKOUT_URL"],
            "lead_magnet_url": app.config["LEAD_MAGNET_URL"],
            "features": {
                "products":  app.config["FEATURE_PRODUCTS"],
                "clients":   app.config["FEATURE_CLIENTS"],
                "tasks":     app.config["FEATURE_TASKS"],
                "email":     app.config["FEATURE_EMAIL"],
                "analytics": app.config["FEATURE_ANALYTICS"],
                "bookings":  app.config["FEATURE_BOOKINGS"],
            },
        }

    # --- Create tables + safe migrations ---
    with app.app_context():
        db.create_all()
        _migrate_columns()
        _migrate_email_templates()

    return app


def _migrate_columns():
    """Safely add new columns to existing tables without dropping data."""
    from sqlalchemy import text as sa_text

    migrations = [
        ("content_items", "transcript",    "TEXT"),
        ("content_items", "brand",         "VARCHAR(10) DEFAULT 'kpps'"),
        ("pbh_assets",    "file_type",     "VARCHAR(10) DEFAULT 'image'"),
        ("contacts",      "tiktok_handle", "VARCHAR(100)"),
        ("contacts",      "follow_up_date","DATE"),
        ("contacts",      "notes_quick",   "TEXT"),
    ]
    for table, col, col_type in migrations:
        try:
            with db.engine.connect() as conn:
                conn.execute(sa_text(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}"
                ))
                conn.commit()
        except Exception:
            try:
                with db.engine.connect() as conn:
                    conn.execute(sa_text(
                        f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"
                    ))
                    conn.commit()
            except Exception:
                pass


def _migrate_email_templates():
    """Update email templates to Monica's branded versions if they still have generic seed content."""
    try:
        from models import EmailTemplate

        purchase_html = """<!DOCTYPE html>
<html>
<head>
  <link href="https://fonts.googleapis.com/css2?family=Dancing+Script:wght@600;700&family=Lato:wght@300;400;700&display=swap" rel="stylesheet"/>
</head>
<body style="margin:0;padding:0;background:#faf9fc;font-family:'Lato',Arial,sans-serif;">
  <div style="background:linear-gradient(135deg,#8a5db7 0%,#7048a0 60%,#5c3688 100%);padding:40px 24px 56px;text-align:center;">
    <div style="font-family:'Dancing Script',cursive;font-size:2rem;color:#e4b94a;margin-bottom:6px;">Welcome to the family, {{name}}!</div>
    <div style="color:rgba(255,255,255,0.88);font-size:1rem;font-weight:300;">Your payment was received. You're officially in.</div>
  </div>
  <div style="max-width:600px;margin:-28px auto 0;background:#fff;border-radius:20px;padding:40px 36px;box-shadow:0 8px 40px rgba(124,77,170,0.12);">

    <p style="font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#8c7a9e;margin-bottom:20px;">Here is what to do right now</p>

    <div style="display:flex;gap:14px;align-items:flex-start;margin-bottom:18px;">
      <div style="min-width:32px;height:32px;border-radius:50%;background:linear-gradient(135deg,#8a5db7,#7048a0);color:#fff;font-size:0.85rem;font-weight:700;display:flex;align-items:center;justify-content:center;margin-top:2px;text-align:center;line-height:32px;">1</div>
      <div><strong style="display:block;font-size:0.95rem;color:#2d1f3d;margin-bottom:3px;">Go to Kids Party Profit Academy on Skool</strong>
      <span style="font-size:0.88rem;color:#5a4570;line-height:1.6;">Click the button below. Create your free Skool account if you don't have one — you'll land straight inside the community.</span></div>
    </div>

    <div style="display:flex;gap:14px;align-items:flex-start;margin-bottom:18px;">
      <div style="min-width:32px;height:32px;border-radius:50%;background:linear-gradient(135deg,#8a5db7,#7048a0);color:#fff;font-size:0.85rem;font-weight:700;display:flex;align-items:center;justify-content:center;margin-top:2px;text-align:center;line-height:32px;">2</div>
      <div><strong style="display:block;font-size:0.95rem;color:#2d1f3d;margin-bottom:3px;">Read the Start Here post in Community first</strong>
      <span style="font-size:0.88rem;color:#5a4570;line-height:1.6;">Find the pinned <strong>Start Here</strong> post in the Community tab. It walks you through everything and sets you up for success.</span></div>
    </div>

    <div style="display:flex;gap:14px;align-items:flex-start;margin-bottom:18px;">
      <div style="min-width:32px;height:32px;border-radius:50%;background:linear-gradient(135deg,#8a5db7,#7048a0);color:#fff;font-size:0.85rem;font-weight:700;display:flex;align-items:center;justify-content:center;margin-top:2px;text-align:center;line-height:32px;">3</div>
      <div><strong style="display:block;font-size:0.95rem;color:#2d1f3d;margin-bottom:3px;">Start Phase 0 — Mindset Reset in the Classroom</strong>
      <span style="font-size:0.88rem;color:#5a4570;line-height:1.6;">All six phases are loaded and ready. Start at Phase 0. Trust the process — each phase builds on the last.</span></div>
    </div>

    <div style="text-align:center;margin:28px 0 20px;">
      <a href="https://www.skool.com/queen-of-side-hustles-academy-5720/about"
         style="display:inline-block;background:linear-gradient(135deg,#c9a84c,#e4b94a);color:#2d1f3d;font-family:'Lato',Arial,sans-serif;font-weight:700;font-size:0.95rem;text-decoration:none;padding:14px 36px;border-radius:50px;box-shadow:0 4px 20px rgba(201,168,76,0.35);">
        Enter Kids Party Profit Academy →
      </a>
      <p style="font-size:0.75rem;color:#8c7a9e;margin-top:8px;">Bookmark this link for easy access later.</p>
    </div>

    <div style="height:1px;background:linear-gradient(90deg,transparent,#9c7bc4,#c9a84c,transparent);opacity:0.3;margin:24px 0;"></div>

    <div style="background:#fdf6e3;border-left:4px solid #c9a84c;border-radius:10px;padding:14px 18px;font-size:0.88rem;color:#5a4570;line-height:1.65;">
      <strong style="color:#2d1f3d;">Questions?</strong> Just reply to this email — I read every single one personally. You can also DM me on TikTok <strong>@kidspartybizcoach</strong>.
    </div>

    <p style="margin-top:28px;font-size:0.9rem;color:#5a4570;line-height:1.7;">So proud of you for betting on yourself.<br>Let's build your empire,</p>
    <p style="font-family:'Dancing Script',cursive;font-size:1.4rem;color:#7048a0;margin-top:6px;">Coach Monica</p>
    <p style="font-size:0.75rem;color:#8c7a9e;text-transform:uppercase;letter-spacing:0.06em;margin-top:2px;">Kids Party Profit System™ · partybusinesscoach.com</p>
  </div>
</body>
</html>"""

        lead_magnet_html = """<!DOCTYPE html>
<html>
<head>
  <link href="https://fonts.googleapis.com/css2?family=Dancing+Script:wght@600;700&family=Lato:wght@300;400;700&display=swap" rel="stylesheet"/>
</head>
<body style="margin:0;padding:0;background:#faf9fc;font-family:'Lato',Arial,sans-serif;">
  <div style="background:linear-gradient(135deg,#8a5db7 0%,#7048a0 60%,#5c3688 100%);padding:40px 24px 56px;text-align:center;">
    <div style="font-family:'Dancing Script',cursive;font-size:2rem;color:#e4b94a;margin-bottom:6px;">Your checklist is here, {{name}}!</div>
    <div style="color:rgba(255,255,255,0.88);font-size:1rem;font-weight:300;">The first step toward your party business empire.</div>
  </div>
  <div style="max-width:600px;margin:-28px auto 0;background:#fff;border-radius:20px;padding:40px 36px;box-shadow:0 8px 40px rgba(124,77,170,0.12);">
    <p style="font-size:0.95rem;color:#5a4570;line-height:1.7;margin-bottom:24px;">
      I put this checklist together because I wish someone had handed it to me when I was starting out. Every item on it is something I learned the hard way so you don't have to.
    </p>

    <div style="text-align:center;margin:28px 0 20px;">
      <a href="{{lead_magnet_url}}"
         style="display:inline-block;background:linear-gradient(135deg,#c9a84c,#e4b94a);color:#2d1f3d;font-family:'Lato',Arial,sans-serif;font-weight:700;font-size:0.95rem;text-decoration:none;padding:14px 36px;border-radius:50px;box-shadow:0 4px 20px rgba(201,168,76,0.35);">
        Download Your Free Checklist →
      </a>
    </div>

    <div style="height:1px;background:linear-gradient(90deg,transparent,#9c7bc4,#c9a84c,transparent);opacity:0.3;margin:24px 0;"></div>

    <p style="font-size:0.88rem;color:#5a4570;line-height:1.7;">
      When you're ready to go deeper — from checklist to fully booked — the <strong>Kids Party Profit System</strong> is your next step. It's everything you need to launch, price, and fill your calendar.
    </p>
    <p style="margin-top:20px;font-size:0.9rem;color:#5a4570;">In your corner,</p>
    <p style="font-family:'Dancing Script',cursive;font-size:1.4rem;color:#7048a0;margin-top:6px;">Coach Monica</p>
    <p style="font-size:0.75rem;color:#8c7a9e;text-transform:uppercase;letter-spacing:0.06em;margin-top:2px;">Kids Party Profit System™ · partybusinesscoach.com</p>
  </div>
</body>
</html>"""

        updates = {
            "purchase_confirmation": {
                "subject": "You're in, {{name}}! Here's how to access Kids Party Profit Academy 🎉",
                "html": purchase_html,
            },
            "lead_magnet": {
                "subject": "Here's your Kids Party Business Starter Checklist, {{name}}! 🎈",
                "html": lead_magnet_html,
            },
        }

        for trigger, content in updates.items():
            t = EmailTemplate.query.filter_by(trigger_type=trigger).first()
            if t:
                t.subject = content["subject"]
                t.body_html = content["html"]
        db.session.commit()
    except Exception:
        pass


# Module-level app instance for gunicorn
app = create_app()

# Start background scheduler (daily auto-generate)
# With --preload, this runs in gunicorn master — threads don't survive fork to workers
from services.scheduler import init_scheduler
init_scheduler(app)

if __name__ == "__main__":
    app.run(debug=True, port=8000)
