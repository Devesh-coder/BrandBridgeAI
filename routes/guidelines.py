import re

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import SQLAlchemyError

import checker
from document_processor import UploadError, delete_file, extract_pdf_text, save_upload
from extensions import db
from models import TONE_STYLES, ApprovedFact, BrandAsset, BrandGuideline
from security import roles_required
from workflow import audit

bp = Blueprint("guidelines", __name__, url_prefix="/guidelines")


def _own(gid):
    return BrandGuideline.query.filter_by(id=gid, org_id=current_user.org_id).first() or abort(404)


@bp.route("/")
@login_required
def index():
    items = BrandGuideline.query.filter_by(org_id=current_user.org_id).order_by(BrandGuideline.id.desc()).all()
    return render_template("guidelines.html", items=items)


@bp.route("/<int:gid>")
@login_required
def view(gid):
    g = _own(gid)
    return render_template("guideline_view.html", g=g, profile=checker.build_profile([g]))


def _validate(f):
    errors = []
    if not f.get("title", "").strip():
        errors.append("Guideline title is required.")
    if not f.get("company_name", "").strip():
        errors.append("Company name is required.")
    if f.get("tone_style") not in TONE_STYLES:
        errors.append("Please choose a tone style.")
    colours = f.get("brand_colours", "")
    for tok in [t for t in re.split(r"[,\s]+", colours) if t]:
        if not re.fullmatch(r"#?([0-9A-Fa-f]{3}|[0-9A-Fa-f]{6})", tok):
            errors.append(f"\"{tok}\" is not a valid hex colour (use e.g. #2563EB).")
            break
    for key, label, n in (("title", "Title", 150), ("company_name", "Company name", 150)):
        if len(f.get(key, "")) > n:
            errors.append(f"{label} is too long (max {n}).")
    for key in ("description", "voice_tone", "approved_terms", "prohibited_words", "unsupported_claims",
                "slogans", "facts"):
        if len(f.get(key, "")) > 20000:
            errors.append(f"{key.replace('_', ' ').capitalize()} is too long.")
    return errors


def _save(g, f):
    g.title = f["title"].strip()
    g.company_name = f["company_name"].strip()
    g.description = f.get("description", "").strip()
    g.voice_tone = f.get("voice_tone", "").strip()
    g.tone_style = f["tone_style"]
    g.approved_terms = f.get("approved_terms", "").strip()
    g.prohibited_words = f.get("prohibited_words", "").strip()
    g.unsupported_claims = f.get("unsupported_claims", "").strip()
    g.brand_colours = ", ".join(checker.parse_colours(f.get("brand_colours", "")))
    g.slogans = f.get("slogans", "").strip()
    g.is_active = f.get("is_active") == "on"
    g.facts = []
    for line in checker.parse_lines(f.get("facts", "")):
        stmt, _, src = line.partition("|")
        if stmt.strip():
            g.facts.append(ApprovedFact(statement=stmt.strip(), source=src.strip()[:300]))


def _handle_uploads(g, files):
    """Validate + store logo / document. Returns list of newly stored filenames (for rollback)."""
    cfg = current_app.config
    stored, replaced = [], []
    logo, doc = files.get("logo"), files.get("guideline_doc")
    if logo and logo.filename:
        info = save_upload(logo, "image", cfg)
        stored.append(info["stored"])
        if g.logo:
            replaced.append(g.logo.stored_filename)
            db.session.delete(g.logo)
        g.assets.append(BrandAsset(org_id=g.org_id, kind="logo", original_filename=info["original"],
                                   stored_filename=info["stored"], mime_type=info["mime"], size_bytes=info["size"],
                                   uploaded_by=current_user.id))
    if doc and doc.filename:
        info = save_upload(doc, "pdf", cfg)
        stored.append(info["stored"])
        try:
            text = extract_pdf_text(info["path"], cfg["MAX_PDF_PAGES"])
        except UploadError:
            delete_file(cfg["UPLOAD_FOLDER"], info["stored"])
            stored.remove(info["stored"])
            raise
        if g.document:
            replaced.append(g.document.stored_filename)
            db.session.delete(g.document)
        g.assets.append(BrandAsset(org_id=g.org_id, kind="guideline_doc", original_filename=info["original"],
                                   stored_filename=info["stored"], mime_type=info["mime"], size_bytes=info["size"],
                                   extracted_text=text, uploaded_by=current_user.id))
    return stored, replaced


def _form(g=None):
    return render_template("guideline_form.html", g=g, tones=TONE_STYLES, form=request.form if request.method == "POST" else None)


def _store(g, is_new):
    errors = _validate(request.form)
    if errors:
        for e in errors:
            flash(e, "danger")
        return None
    cfg = current_app.config
    stored, replaced = [], []
    try:
        if is_new:
            g.org_id = current_user.org_id
            g.created_by = current_user.id
            db.session.add(g)
        _save(g, request.form)
        db.session.flush()
        stored, replaced = _handle_uploads(g, request.files)
        audit("guideline.create" if is_new else "guideline.update", "guideline", g.id, g.title)
        db.session.commit()
    except UploadError as exc:
        db.session.rollback()
        for s in stored:
            delete_file(cfg["UPLOAD_FOLDER"], s)
        flash(str(exc), "danger")
        return None
    except SQLAlchemyError:
        db.session.rollback()
        for s in stored:
            delete_file(cfg["UPLOAD_FOLDER"], s)
        flash("The guideline could not be saved because of a database error.", "danger")
        return None
    for r in replaced:
        delete_file(cfg["UPLOAD_FOLDER"], r)
    return g


@bp.route("/new", methods=["GET", "POST"])
@roles_required("admin")
def new():
    if request.method == "POST":
        g = _store(BrandGuideline(), True)
        if g:
            flash("Brand guideline created.", "success")
            return redirect(url_for("guidelines.view", gid=g.id))
        return _form(None), 400
    return _form(None)


@bp.route("/<int:gid>/edit", methods=["GET", "POST"])
@roles_required("admin")
def edit(gid):
    g = _own(gid)
    if request.method == "POST":
        if _store(g, False):
            flash("Brand guideline updated.", "success")
            return redirect(url_for("guidelines.view", gid=g.id))
        db.session.expire_all()
        return _form(g), 400
    return _form(g)


@bp.route("/<int:gid>/delete", methods=["POST"])
@roles_required("admin")
def delete(gid):
    g = _own(gid)
    files = [a.stored_filename for a in g.assets]
    try:
        audit("guideline.delete", "guideline", g.id, g.title)
        db.session.delete(g)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        flash("Could not delete this guideline.", "danger")
        return redirect(url_for("guidelines.index"))
    for fn in files:
        delete_file(current_app.config["UPLOAD_FOLDER"], fn)
    flash("Guideline deleted.", "success")
    return redirect(url_for("guidelines.index"))


@bp.route("/<int:gid>/asset/<int:aid>/delete", methods=["POST"])
@roles_required("admin")
def delete_asset(gid, aid):
    g = _own(gid)
    asset = BrandAsset.query.filter_by(id=aid, guideline_id=g.id, org_id=g.org_id).first() or abort(404)
    fn = asset.stored_filename
    db.session.delete(asset)
    audit("guideline.asset_delete", "asset", aid)
    db.session.commit()
    delete_file(current_app.config["UPLOAD_FOLDER"], fn)
    flash("File removed.", "success")
    return redirect(url_for("guidelines.view", gid=g.id))
