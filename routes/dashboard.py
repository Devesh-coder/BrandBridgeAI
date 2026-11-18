from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import SQLAlchemyError

import ai_service
from extensions import db
from models import ROLES, AuditLog, ContentSubmission, Organisation, User, new_invite_code
from routes.analytics import compute_stats
from routes.auth import password_problem
from security import roles_required
from workflow import audit

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def home():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    return redirect(url_for("auth.login"))


@bp.route("/dashboard")
@login_required
def index():
    q = ContentSubmission.query.filter_by(org_id=current_user.org_id)
    if current_user.role == "creator":
        q = q.filter_by(author_id=current_user.id)
    recent = q.order_by(ContentSubmission.updated_at.desc()).limit(6).all()
    return render_template("dashboard.html", stats=compute_stats(current_user), recent=recent)


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        new_pw = request.form.get("new_password", "")
        if not name or len(name) > 120:
            flash("Please enter a valid name.", "danger")
        else:
            if new_pw:
                if not current_user.check_password(request.form.get("current_password", "")):
                    flash("Current password is incorrect.", "danger")
                    return redirect(url_for("dashboard.settings"))
                problem = password_problem(new_pw)
                if problem:
                    flash(problem, "danger")
                    return redirect(url_for("dashboard.settings"))
                current_user.set_password(new_pw)
            current_user.name = name
            try:
                audit("settings.profile", "user", current_user.id)
                db.session.commit()
                flash("Profile updated.", "success")
            except SQLAlchemyError:
                db.session.rollback()
                flash("Could not save your profile.", "danger")
        return redirect(url_for("dashboard.settings"))
    org = db.session.get(Organisation, current_user.org_id)
    users = logs = None
    if current_user.is_admin:
        users = User.query.filter_by(org_id=current_user.org_id).order_by(User.id).all()
        logs = AuditLog.query.filter_by(org_id=current_user.org_id).order_by(AuditLog.id.desc()).limit(25).all()
    from flask import current_app
    return render_template("settings.html", org=org, users=users, logs=logs, roles=ROLES,
                           ai=ai_service.status_summary(current_app.config))


@bp.route("/settings/users/<int:user_id>/role", methods=["POST"])
@roles_required("admin")
def set_role(user_id):
    user = User.query.filter_by(id=user_id, org_id=current_user.org_id).first() or abort(404)
    role = request.form.get("role")
    if role not in ROLES:
        flash("Invalid role.", "danger")
    elif user.id == current_user.id:
        flash("You cannot change your own role.", "warning")
    else:
        user.role = role
        audit("user.role_change", "user", user.id, f"role={role}")
        db.session.commit()
        flash(f"{user.name} is now {user.role_label}.", "success")
    return redirect(url_for("dashboard.settings"))


@bp.route("/settings/users/<int:user_id>/active", methods=["POST"])
@roles_required("admin")
def toggle_active(user_id):
    user = User.query.filter_by(id=user_id, org_id=current_user.org_id).first() or abort(404)
    if user.id == current_user.id:
        flash("You cannot deactivate your own account.", "warning")
    else:
        user.active = not user.active
        audit("user.active_toggle", "user", user.id, f"active={user.active}")
        db.session.commit()
        flash(f"{user.name} {'activated' if user.active else 'deactivated'}.", "success")
    return redirect(url_for("dashboard.settings"))


@bp.route("/settings/invite/regenerate", methods=["POST"])
@roles_required("admin")
def regenerate_invite():
    org = db.session.get(Organisation, current_user.org_id)
    org.invite_code = new_invite_code()
    audit("org.invite_regenerated", "organisation", org.id)
    db.session.commit()
    flash("New invite code generated.", "success")
    return redirect(url_for("dashboard.settings"))
