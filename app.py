"""GV POWERS corporate website — Flask application entry point.

Run locally:
    python app.py

Production:
    gunicorn -c gunicorn.conf.py app:app
"""
import logging
import os
import secrets
from logging.handlers import RotatingFileHandler

import click
from flask import Flask, render_template, request, g, session
from dotenv import load_dotenv

import database as db

from config import BASE_DIR, get_config
from database import init_db, close_db
from routes.main import main
from routes.contact import contact_bp
from routes.admin import admin_bp
from icons import ICONS
from site_data import COMPANY, NAV_LINKS, SERVICE_CHOICES
from security import generate_csrf_token, RateLimiter

# Load the project's .env explicitly. `load_dotenv()` without a path depends on
# the process working directory, which is a common cause of "SMTP not working"
# when Gunicorn/systemd starts the app from another directory. Existing
# environment variables (systemd EnvironmentFile, container env) still win.
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _add_file_handler(logger: logging.Logger, log_file: str) -> None:
    """Attach one rotating file handler, avoiding duplicates on re-create."""
    target = os.path.abspath(log_file)
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler):
            existing = os.path.abspath(getattr(handler, "baseFilename", ""))
            if existing == target:
                return
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    handler = RotatingFileHandler(
        target, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)


def _configure_logging(app: Flask) -> None:
    log_file = app.config.get("LOG_FILE")
    if log_file:
        _add_file_handler(app.logger, log_file)
        _add_file_handler(logging.getLogger("gvpowers"), log_file)
    app.logger.setLevel(logging.INFO)
    logging.getLogger("gvpowers").setLevel(logging.INFO)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(get_config())

    _configure_logging(app)
    init_db(app)
    app.teardown_appcontext(close_db)

    # Mail status at startup: makes "emails not sending" instantly diagnosable.
    import mailer
    smtp = mailer._smtp_settings()
    if smtp:
        app.logger.info(
            "Mail: enabled via %s:%s (%s/%s, timeout %ss), leads to %s",
            smtp["host"], smtp["port"], smtp["security"], smtp["timeout"],
            smtp["user"], mailer._company_recipient(),
        )
    else:
        app.logger.warning(
            "Mail: DISABLED — SMTP_USER / SMTP_APP_PASSWORD missing from the "
            "environment (.env). Enquiries are still stored in the database."
        )

    app.register_blueprint(main)
    app.register_blueprint(contact_bp)
    app.register_blueprint(admin_bp)

    # ---- CLI: verify production SMTP without touching the contact form -----
    @app.cli.command("mail-test")
    @click.option("--to", "to_email", required=True,
                  help="Recipient address for the test thank-you email.")
    @click.option("--name", default="GV POWERS Test", show_default=True,
                  help="Recipient display name used in the greeting.")
    def mail_test(to_email: str, name: str) -> None:
        """Send a branded customer thank-you test email."""
        to_email = to_email.strip()
        if "@" not in to_email or to_email.startswith("@") or to_email.endswith("@"):
            raise click.BadParameter(
                "Enter a valid email address.", param_hint="--to"
            )
        if not mailer.is_configured():
            raise click.ClickException(
                "Mail is not configured. Set SMTP_USER and SMTP_APP_PASSWORD "
                "in .env (or the systemd EnvironmentFile)."
            )
        click.echo(f"Sending test email to {to_email} ...")
        if not mailer.send_test_email_sync(to_email, name=name):
            raise click.ClickException(
                "Test email failed. Check the SMTP settings and the app log."
            )
        click.echo("Test email sent. Check the inbox and spam folder.")

    # brute-force protection for the admin login (shared limiter instance)
    app.extensions["login_limiter"] = RateLimiter()

    # ---- Template context --------------------------------------------------
    @app.context_processor
    def inject_globals():
        from markupsafe import Markup

        def icon(name, cls=""):
            svg = ICONS.get(name, "")
            return Markup(
                f'<svg class="icon {cls}" viewBox="0 0 24 24" fill="none" '
                f'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
                f'stroke-linejoin="round" aria-hidden="true" focusable="false">{svg}</svg>'
            )

        return {
            "company": COMPANY,
            "nav_links": NAV_LINKS,
            "service_choices": SERVICE_CHOICES,
            "csrf_token": generate_csrf_token,
            "icon": icon,
            "site_url": app.config["SITE_URL"],
            "catalogue_url": app.config["CATALOGUE_URL"],
            "current_year": 2026,
        }

    # ---- Security headers + strict CSP with per-request nonce ---------------
    @app.before_request
    def set_nonce():
        g.csp_nonce = secrets.token_urlsafe(24)

    @app.after_request
    def set_security_headers(response):
        nonce = g.get("csp_nonce", "")
        csp = (
            "default-src 'self'; "
            "script-src 'self' 'nonce-" + nonce + "'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https://*.tile.openstreetmap.org; "
            "connect-src 'self' https://*.tile.openstreetmap.org; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers["Content-Security-Policy"] = csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if app.config.get("SESSION_COOKIE_SECURE") or session.get("admin_verified"):
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    # ---- Error pages ---------------------------------------------------------
    @app.errorhandler(404)
    def not_found(_err):
        if request.path.startswith(("/static/", "/images/")):
            return {"error": "not found"}, 404
        return render_template("404.html"), 404

    @app.errorhandler(500)
    def server_error(_err):
        return render_template("500.html"), 500

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV", "development") != "production"
    app.run(host="127.0.0.1", port=port, debug=debug)
