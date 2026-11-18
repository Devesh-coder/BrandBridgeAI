from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import SQLAlchemyError

import checker
from extensions import db
from models import ContentSubmission, Review
from security import roles_required
from workflow import EVENT_LABELS, WorkflowError, get_submission_or_none, record_review

bp = Blueprint("reviews", __name__)


@bp.route("/reviews")
@login_required
def queue():
    base = ContentSubmission.query.filter_by(org_id=current_user.org_id)
    if current_user.can_review:
        pending = (base.filter_by(status="pending_review").filter(ContentSubmission.author_id != current_user.id)
                   .order_by(ContentSubmission.submitted_at).all())
        own = base.filter_by(author_id=current_user.id).order_by(ContentSubmission.id.desc()).limit(20).all() \
            if current_user.role == "admin" else []
        return render_template("reviews.html", pending=pending, mine=own, reviewer_view=True)
    mine = base.filter_by(author_id=current_user.id).order_by(ContentSubmission.updated_at.desc()).all()
    return render_template("reviews.html", pending=[], mine=mine, reviewer_view=False)


@bp.route("/reviews/<int:sub_id>")
@roles_required("admin", "reviewer")
def detail(sub_id):
    sub = get_submission_or_none(sub_id) or abort(404)
    issues = sub.current_issues()
    return render_template("review_detail.html", sub=sub, issues=issues, summary=checker.compute_score(issues),
                           events=EVENT_LABELS)


@bp.route("/reviews/<int:sub_id>/decide", methods=["POST"])
@roles_required("admin", "reviewer")
def decide(sub_id):
    sub = get_submission_or_none(sub_id) or abort(404)
    decision = request.form.get("decision", "")
    try:
        record_review(sub, current_user, decision, request.form.get("comment", ""))
        db.session.commit()
        msg = {"approved": "Content approved.", "rejected": "Content rejected.",
               "changes_requested": "Changes requested – the creator has been notified in their review list.",
               "comment": "Comment added."}[decision]
        flash(msg, "success")
        return redirect(url_for("reviews.queue") if decision != "comment" else url_for("reviews.detail", sub_id=sub.id))
    except WorkflowError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    except SQLAlchemyError:
        db.session.rollback()
        flash("Database error while saving the review.", "danger")
    return redirect(url_for("reviews.detail", sub_id=sub.id))


@bp.route("/history")
@login_required
def history():
    q = (Review.query.join(ContentSubmission).filter(ContentSubmission.org_id == current_user.org_id))
    if current_user.role == "creator":
        q = q.filter(ContentSubmission.author_id == current_user.id)
    rows = q.order_by(Review.id.desc()).limit(200).all()
    return render_template("history.html", rows=rows)
