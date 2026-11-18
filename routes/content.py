import difflib

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_from_directory, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import SQLAlchemyError

import checker
from document_processor import UploadError, delete_file, extract_pdf_text, save_upload
from extensions import db
from models import CATEGORIES, CONTENT_TYPES, BrandAsset, ContentSubmission, CorrectionSuggestion
from security import roles_required
from workflow import (EVENT_LABELS, WorkflowError, active_guidelines, add_revision, audit, decide_suggestion,
                      get_submission_or_none, perform_check, send_for_review, update_content)

bp = Blueprint("content", __name__, url_prefix="/content")
files_bp = Blueprint("files", __name__)


def _sub(sub_id):
    return get_submission_or_none(sub_id) or abort(404)


def _recent():
    q = ContentSubmission.query.filter_by(org_id=current_user.org_id)
    if current_user.role == "creator" or current_user.role == "admin":
        q = q.filter_by(author_id=current_user.id)
    return q.order_by(ContentSubmission.id.desc()).limit(8).all()


@bp.route("/submit", methods=["GET", "POST"])
@roles_required("admin", "creator")
def submit():
    cfg = current_app.config
    has_guidelines = bool(active_guidelines(current_user.org_id))
    if request.method == "GET":
        return render_template("submit_content.html", types=CONTENT_TYPES, recent=_recent(), form={},
                               has_guidelines=has_guidelines)
    f = request.form
    title, ctype, text = f.get("title", "").strip(), f.get("content_type", ""), f.get("content_text", "").strip()
    alt = f.get("image_alt", "").strip()[:500]
    img, pdf = request.files.get("image"), request.files.get("pdf")
    has_img, has_pdf = bool(img and img.filename), bool(pdf and pdf.filename)

    def fail(msg, code=400):
        flash(msg, "danger")
        return render_template("submit_content.html", types=CONTENT_TYPES, recent=_recent(), form=f,
                               has_guidelines=has_guidelines), code

    if not has_guidelines:
        return fail("No brand guidelines exist yet. Ask an administrator to add them before submitting content.")
    if not title or len(title) > 200:
        return fail("Please enter a content title (max 200 characters).")
    if ctype not in CONTENT_TYPES:
        return fail("Please choose a valid content type.")
    if len(text) > cfg["MAX_CONTENT_CHARS"]:
        return fail(f"Content is too long (maximum {cfg['MAX_CONTENT_CHARS']:,} characters).")
    if not text and not has_pdf:
        return fail("Content is empty. Paste your text or upload a PDF.")

    stored = []
    try:
        pdf_info = img_info = None
        if has_pdf:
            pdf_info = save_upload(pdf, "pdf", cfg)
            stored.append(pdf_info["stored"])
            text = extract_pdf_text(pdf_info["path"], cfg["MAX_PDF_PAGES"], cfg["MAX_CONTENT_CHARS"])
        if has_img:
            img_info = save_upload(img, "image", cfg)
            stored.append(img_info["stored"])
        sub = ContentSubmission(
            org_id=current_user.org_id, author_id=current_user.id, title=title, content_type=ctype,
            content_text=text, original_text=text, image_alt=alt,
            image_filename=img_info["stored"] if img_info else None, image_original=img_info["original"] if img_info else None,
            pdf_filename=pdf_info["stored"] if pdf_info else None, pdf_original=pdf_info["original"] if pdf_info else None,
            status="checked", version=1)
        db.session.add(sub)
        db.session.flush()
        add_revision(sub, "submitted", current_user, "Original submission.")
        perform_check(sub, current_user)
        audit("content.submit", "submission", sub.id, title)
        db.session.commit()
    except UploadError as exc:
        db.session.rollback()
        for s in stored:
            delete_file(cfg["UPLOAD_FOLDER"], s)
        return fail(str(exc))
    except WorkflowError as exc:
        db.session.rollback()
        for s in stored:
            delete_file(cfg["UPLOAD_FOLDER"], s)
        return fail(str(exc))
    except (SQLAlchemyError, ValueError):
        db.session.rollback()
        for s in stored:
            delete_file(cfg["UPLOAD_FOLDER"], s)
        current_app.logger.exception("submission failed")
        return fail("Your submission could not be saved because of a database error. Please try again.", 500)
    flash("Content checked. Review the issues and suggested corrections below.", "success")
    return redirect(url_for("content.results", sub_id=sub.id))


@bp.route("/<int:sub_id>")
@login_required
def results(sub_id):
    sub = _sub(sub_id)
    issues = sub.current_issues()
    summary = checker.compute_score(issues)
    by_cat = {c: sum(1 for i in issues if i.category == c) for c in CATEGORIES}
    preview = sub.content_text
    for i in issues:
        s = i.suggestion
        if s and s.status == "pending" and s.suggested_text is not None and s.original_text and s.original_text in preview:
            preview = preview.replace(s.original_text, s.suggested_text, 1)
    pending = sum(1 for i in issues if i.suggestion and i.suggestion.status == "pending")
    return render_template("results.html", sub=sub, issues=issues, summary=summary, by_cat=by_cat, preview=preview,
                           pending=pending, score_text=checker.SCORE_EXPLANATION, events=EVENT_LABELS)


@bp.route("/<int:sub_id>/edit", methods=["GET", "POST"])
@roles_required("admin", "creator")
def edit(sub_id):
    sub = _sub(sub_id)
    if sub.author_id != current_user.id:
        abort(403)
    if not sub.is_editable:
        flash("This submission is locked and cannot be edited.", "warning")
        return redirect(url_for("content.results", sub_id=sub.id))
    if request.method == "POST":
        try:
            update_content(sub, request.form.get("content_text", ""), request.form.get("image_alt") if sub.image_filename else None,
                           current_user)
            perform_check(sub, current_user)
            db.session.commit()
            flash("Content updated and re-checked.", "success")
            return redirect(url_for("content.results", sub_id=sub.id))
        except WorkflowError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        except SQLAlchemyError:
            db.session.rollback()
            flash("Database error while saving. Please try again.", "danger")
        sub = _sub(sub_id)
    return render_template("edit_content.html", sub=sub)


@bp.route("/<int:sub_id>/recheck", methods=["POST"])
@roles_required("admin", "creator")
def recheck(sub_id):
    sub = _sub(sub_id)
    if sub.author_id != current_user.id:
        abort(403)
    if not sub.is_editable:
        flash("This submission is locked.", "warning")
        return redirect(url_for("content.results", sub_id=sub.id))
    try:
        perform_check(sub, current_user)
        db.session.commit()
        flash("Brand check re-run on the current content.", "success")
    except WorkflowError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except SQLAlchemyError:
        db.session.rollback()
        flash("Database error while re-running the check.", "danger")
    return redirect(url_for("content.results", sub_id=sub.id))


@bp.route("/<int:sub_id>/suggestion/<int:sug_id>/<action>", methods=["POST"])
@roles_required("admin", "creator")
def suggestion(sub_id, sug_id, action):
    sub = _sub(sub_id)
    if sub.author_id != current_user.id:
        abort(403)
    sug = (CorrectionSuggestion.query.join(CorrectionSuggestion.issue)
           .filter(CorrectionSuggestion.id == sug_id, CorrectionSuggestion.issue.has(submission_id=sub.id)).first()
           or abort(404))
    if not sug.issue.is_current:
        flash("That suggestion belongs to an earlier check.", "warning")
        return redirect(url_for("content.results", sub_id=sub.id))
    try:
        decide_suggestion(sub, sug, action, current_user, request.form.get("custom_text", ""))
        db.session.commit()
        flash({"accept": "Suggestion applied to your content. Re-run the check when you are done.",
               "reject": "Suggestion rejected – original text kept.",
               "edit": "Your manual edit was applied. Re-run the check when you are done.",
               "acknowledge": "Issue acknowledged and left for the reviewer."}.get(action, "Done."), "success")
    except WorkflowError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except SQLAlchemyError:
        db.session.rollback()
        flash("Database error. Please try again.", "danger")
    return redirect(url_for("content.results", sub_id=sub.id) + f"#issue-{sug.issue_id}")


@bp.route("/<int:sub_id>/send-review", methods=["POST"])
@roles_required("admin", "creator")
def send_review(sub_id):
    sub = _sub(sub_id)
    try:
        send_for_review(sub, current_user)
        db.session.commit()
        flash("Sent to a manager for review. Nothing is published until a human approves it.", "success")
    except WorkflowError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except SQLAlchemyError:
        db.session.rollback()
        flash("Database error. Please try again.", "danger")
    return redirect(url_for("content.results", sub_id=sub.id))


@bp.route("/<int:sub_id>/revisions")
@login_required
def revisions(sub_id):
    sub = _sub(sub_id)
    return render_template("revisions.html", sub=sub, events=EVENT_LABELS, rev=None, diff=None)


@bp.route("/<int:sub_id>/revisions/<int:rev_id>")
@login_required
def revision(sub_id, rev_id):
    sub = _sub(sub_id)
    revs = sub.revisions
    rev = next((r for r in revs if r.id == rev_id), None) or abort(404)
    idx = revs.index(rev)
    prev = revs[idx - 1] if idx else None
    diff = None
    if prev and prev.content_snapshot != rev.content_snapshot:
        diff = "\n".join(difflib.unified_diff((prev.content_snapshot or "").splitlines(), (rev.content_snapshot or "").splitlines(),
                                              "previous version", "this version", lineterm="", n=1))
    return render_template("revisions.html", sub=sub, events=EVENT_LABELS, rev=rev, diff=diff)


# --------------------------------------------------------------- authenticated file serving
@files_bp.route("/files/<kind>/<int:ident>")
@login_required
def serve(kind, ident):
    folder = current_app.config["UPLOAD_FOLDER"]
    if kind in ("sub-image", "sub-pdf"):
        sub = _sub(ident)
        name = sub.image_filename if kind == "sub-image" else sub.pdf_filename
        if not name:
            abort(404)
        return send_from_directory(folder, name, as_attachment=(kind == "sub-pdf"),
                                   download_name=(sub.pdf_original if kind == "sub-pdf" else sub.image_original))
    if kind == "asset":
        asset = BrandAsset.query.filter_by(id=ident, org_id=current_user.org_id).first() or abort(404)
        return send_from_directory(folder, asset.stored_filename, as_attachment=(asset.kind != "logo"),
                                   download_name=asset.original_filename)
    abort(404)
