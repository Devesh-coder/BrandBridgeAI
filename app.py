"""BrandBridge AI – application factory.

Run locally:   flask --app app run --debug      (or: python app.py)
Seed demo:     flask --app app seed-demo
"""
import os

import click
from flask import Flask, flash, redirect, render_template, request, url_for
from flask_login import current_user
from flask_wtf.csrf import CSRFError
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import RequestEntityTooLarge

import ai_service
from config import Config, validate_production
from extensions import csrf, db, login_manager
from models import ROLE_LABELS, STATUS_LABELS, User


def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)
    validate_production(app.config)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from routes import analytics, auth, content, dashboard, guidelines, reviews
    for bp in (auth.bp, dashboard.bp, guidelines.bp, content.bp, content.files_bp, reviews.bp, analytics.bp):
        app.register_blueprint(bp)

    with app.app_context():
        db.create_all()

    # ------------------------------------------------------------ template helpers
    @app.template_filter("dt")
    def fmt_dt(value, fmt="%d %b %Y, %H:%M"):
        return value.strftime(fmt) if value else "—"

    @app.context_processor
    def inject():
        from models import CONTENT_TYPES, SEVERITY_ORDER
        pending = 0
        if current_user.is_authenticated and current_user.can_review:
            from models import ContentSubmission
            pending = (ContentSubmission.query.filter_by(org_id=current_user.org_id, status="pending_review")
                       .filter(ContentSubmission.author_id != current_user.id).count())
        return {"STATUS_LABELS": STATUS_LABELS, "ROLE_LABELS": ROLE_LABELS, "CONTENT_TYPES": CONTENT_TYPES,
                "nav_pending": pending, "ai_info": ai_service.status_summary(app.config)}

    # ------------------------------------------------------------ security headers
    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:; "
            "font-src 'self' https://cdn.jsdelivr.net; connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
        if request.endpoint not in ("static",) and current_user.is_authenticated:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ------------------------------------------------------------ error handlers
    def _err(code, title, message):
        return render_template("error.html", code=code, title=title, message=message), code

    @app.errorhandler(403)
    def forbidden(_e):
        return _err(403, "Access denied", "You do not have permission to perform this action or view this page.")

    @app.errorhandler(404)
    def not_found(_e):
        return _err(404, "Page not found", "The page or record you are looking for does not exist or is not available to you.")

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_e):
        flash("The upload is too large. Maximum request size is 12 MB.", "danger")
        return redirect(request.referrer or url_for("dashboard.index"))

    @app.errorhandler(CSRFError)
    def csrf_error(_e):
        flash("Your session expired or the form was invalid. Please try again.", "warning")
        return redirect(request.referrer or url_for("auth.login"))

    @app.errorhandler(SQLAlchemyError)
    def db_error(_e):
        db.session.rollback()
        app.logger.exception("database error")
        return _err(500, "Database error", "A database error occurred. Your changes were not saved. Please try again.")

    @app.errorhandler(500)
    def server_error(_e):
        db.session.rollback()
        return _err(500, "Something went wrong", "An unexpected error occurred. Please try again.")

    # ------------------------------------------------------------ CLI
    @app.cli.command("init-db")
    def init_db():
        db.create_all()
        click.echo("Database initialised.")

    @app.cli.command("seed-demo")
    def seed_demo_cmd():
        from seed import seed_demo
        seed_demo()

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
