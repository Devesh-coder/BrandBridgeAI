import re
from urllib.parse import urlparse

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from models import EMAIL_RE, Organisation, User
from workflow import audit

bp = Blueprint("auth", __name__)


def password_problem(pw: str):
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        return "Password must contain at least one letter and one number."
    return None


def _safe_next(target):
    if not target:
        return None
    p = urlparse(target)
    return target if not p.netloc and not p.scheme and target.startswith("/") and not target.startswith("//") else None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first() if email else None
        if user and user.check_password(password):
            if not user.is_active:
                flash("This account has been deactivated. Contact your administrator.", "danger")
            else:
                login_user(user)
                audit("auth.login", "user", user.id, user=user)
                db.session.commit()
                return redirect(_safe_next(request.args.get("next")) or url_for("dashboard.index"))
        else:
            flash("Invalid email or password.", "danger")
    return render_template("login.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        f = request.form
        name, email = f.get("name", "").strip(), f.get("email", "").strip().lower()
        pw, pw2 = f.get("password", ""), f.get("confirm", "")
        mode = f.get("mode", "join")
        org_name, code = f.get("org_name", "").strip(), f.get("invite_code", "").strip().upper()
        error = None
        if not name or len(name) > 120:
            error = "Please enter your name (max 120 characters)."
        elif not EMAIL_RE.match(email) or len(email) > 255:
            error = "Please enter a valid email address."
        elif password_problem(pw):
            error = password_problem(pw)
        elif pw != pw2:
            error = "Passwords do not match."
        elif User.query.filter_by(email=email).first():
            error = "An account with this email already exists. Please sign in."
        org = None
        if not error:
            if mode == "create":
                if not org_name or len(org_name) > 150:
                    error = "Please enter an organisation name."
                elif Organisation.query.filter(db.func.lower(Organisation.name) == org_name.lower()).first():
                    error = "An organisation with this name already exists. Ask its administrator for an invite code."
            else:
                org = Organisation.query.filter_by(invite_code=code).first() if code else None
                if not org:
                    error = "Invalid invite code. Ask your administrator for the organisation's invite code."
        if error:
            flash(error, "danger")
            return render_template("register.html", form=f), 400
        try:
            if mode == "create":
                org = Organisation(name=org_name)
                db.session.add(org)
                db.session.flush()
                role = "admin"  # creator of a new organisation becomes its administrator
            else:
                role = "creator"  # joiners start as creators; admins can promote them in Settings
            user = User(org_id=org.id, name=name, email=email, role=role)
            user.set_password(pw)
            db.session.add(user)
            db.session.flush()
            audit("auth.register", "user", user.id, f"role={role}", user=user)
            db.session.commit()
        except (SQLAlchemyError, ValueError):
            db.session.rollback()
            flash("Registration failed. Please check your details and try again.", "danger")
            return render_template("register.html", form=f), 400
        login_user(user)
        flash(f"Welcome to BrandBridge AI, {user.name}!", "success")
        return redirect(url_for("dashboard.index"))
    return render_template("register.html", form={})


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit("auth.logout", "user", current_user.id)
    db.session.commit()
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))
