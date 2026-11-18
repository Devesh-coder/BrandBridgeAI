"""Business logic shared by the routes: checking, suggestions, revisions, audit, permissions.

The AI / rule engine only *suggests*. Status changes to approved / rejected happen exclusively in
`record_review`, which requires a human reviewer who is not the author.
"""
from __future__ import annotations

import os

from flask import current_app, request
from flask_login import current_user

import ai_service
import checker
import visual_checker
from extensions import db
from models import (AuditLog, BrandGuideline, ContentIssue, ContentSubmission, CorrectionSuggestion, Review,
                    RevisionHistory, utcnow)


class WorkflowError(Exception):
    """User-facing error for an invalid workflow action."""


# ---------------------------------------------------------------- audit & revisions
def audit(action, entity_type=None, entity_id=None, details="", user=None, org_id=None):
    u = user or (current_user if current_user and current_user.is_authenticated else None)
    try:
        ip = request.remote_addr if request else None
    except RuntimeError:
        ip = None
    db.session.add(AuditLog(org_id=org_id or (u.org_id if u else None), user_id=u.id if u else None, action=action,
                            entity_type=entity_type, entity_id=entity_id, details=(details or "")[:1000],
                            ip_address=ip))


def add_revision(sub: ContentSubmission, event: str, user=None, details: str = ""):
    db.session.add(RevisionHistory(
        submission_id=sub.id, version=sub.version, event=event, details=details, content_snapshot=sub.content_text,
        status_snapshot=sub.status, score=sub.score, user_id=user.id if user else None))


EVENT_LABELS = {
    "submitted": "Original submission", "content_edited": "Content edited", "check_run": "Brand check run",
    "suggestion_accepted": "Suggestion accepted", "suggestion_rejected": "Suggestion rejected",
    "suggestion_edited": "Suggestion edited manually", "suggestion_acknowledged": "Suggestion acknowledged",
    "sent_for_review": "Sent for review", "review_approved": "Approved by reviewer",
    "review_rejected": "Rejected by reviewer", "review_changes_requested": "Changes requested by reviewer",
    "review_comment": "Reviewer comment",
}


# ---------------------------------------------------------------- access control
def get_submission_or_none(sub_id: int):
    """Return a submission only if the current user may see it (same org; creators only their own)."""
    sub = ContentSubmission.query.filter_by(id=sub_id, org_id=current_user.org_id).first()
    if sub is None:
        return None
    if current_user.role == "creator" and sub.author_id != current_user.id:
        return None
    return sub


def active_guidelines(org_id: int):
    return BrandGuideline.query.filter_by(org_id=org_id, is_active=True).order_by(BrandGuideline.id).all()


# ---------------------------------------------------------------- checking
def perform_check(sub: ContentSubmission, user) -> dict:
    guidelines = active_guidelines(sub.org_id)
    if not guidelines:
        raise WorkflowError("No active brand guidelines exist for your organisation. Ask an administrator to add them.")
    cfg = current_app.config
    profile = checker.build_profile(guidelines)

    findings = checker.check_text(sub.content_text, profile, sub.content_type)
    info = {}
    if sub.image_filename:
        logo_paths = []
        for g in guidelines:
            if g.logo:
                p = os.path.join(cfg["UPLOAD_FOLDER"], g.logo.stored_filename)
                if os.path.exists(p):
                    logo_paths.append(p)
        img_path = os.path.join(cfg["UPLOAD_FOLDER"], sub.image_filename)
        vf, info = visual_checker.analyse_image(img_path, profile.colours, logo_paths, sub.image_alt or "",
                                                sub.image_original or "image")
        findings += vf
    ai_findings, ai_status = ai_service.ai_review(cfg, sub.content_text, profile, sub.content_type, findings)
    findings += ai_findings

    for old in sub.issues:
        old.is_current = False
    for f in findings:
        issue = ContentIssue(
            submission_id=sub.id, check_version=sub.version, is_current=True, title=f.title, category=f.category,
            severity=f.severity, original_text=f.original_text, explanation=f.explanation,
            guideline_violated=f.guideline, suggested_correction=f.suggestion, related_source=f.related_source,
            verification=f.verification, source=f.source, rule_id=f.rule_id)
        issue.suggestion = CorrectionSuggestion(original_text=f.original_text, suggested_text=f.suggested_text)
        db.session.add(issue)

    result = checker.compute_score(findings)
    sub.score = result["score"]
    sub.review_label = result["label"]
    sub.checked_version = sub.version
    sub.ai_status = ai_status
    db.session.flush()
    add_revision(sub, "check_run", user, f"{result['total']} issue(s); score {result['score']}/100. {ai_status}")
    audit("content.check", "submission", sub.id, f"score={result['score']} issues={result['total']}", user=user)
    return {"findings": findings, "score": result, "visual": info}


# ---------------------------------------------------------------- suggestions
def decide_suggestion(sub: ContentSubmission, sug: CorrectionSuggestion, action: str, user, custom_text: str = ""):
    if not sub.is_editable:
        raise WorkflowError("This submission is locked and can no longer be edited.")
    if sug.status != "pending":
        raise WorkflowError("This suggestion has already been handled.")
    issue = sug.issue
    now = utcnow()

    if action == "reject":
        sug.status = "rejected"
        event = "suggestion_rejected"
        detail = f"Rejected: {issue.title}"
    elif action == "acknowledge":
        sug.status = "acknowledged"
        event = "suggestion_acknowledged"
        detail = f"Acknowledged (no automatic change): {issue.title}"
    elif action in ("accept", "edit"):
        new_text = sug.suggested_text if action == "accept" else (custom_text or "")
        if action == "accept" and sug.suggested_text is None:
            raise WorkflowError("This issue has no automatic correction. Edit it manually or acknowledge it.")
        if action == "edit" and not custom_text.strip() and custom_text != "":
            raise WorkflowError("Enter replacement text.")
        orig = sug.original_text or ""
        if not orig or orig not in sub.content_text:
            raise WorkflowError("The original text is no longer in the content (it was changed earlier). "
                                "Re-run the check to refresh suggestions.")
        sub.content_text = sub.content_text.replace(orig, new_text, 1)
        sub.version += 1
        sug.status = "accepted" if action == "accept" else "edited"
        sug.final_text = new_text
        event = "suggestion_accepted" if action == "accept" else "suggestion_edited"
        detail = f"{issue.title}: \"{orig[:120]}\" → \"{new_text[:120]}\""
    else:
        raise WorkflowError("Unknown action.")
    sug.decided_by, sug.decided_at = user.id, now
    db.session.flush()
    add_revision(sub, event, user, detail)
    audit(f"suggestion.{action}", "suggestion", sug.id, detail, user=user)


def update_content(sub: ContentSubmission, new_text: str, alt_text: str | None, user):
    if not sub.is_editable:
        raise WorkflowError("This submission is locked and can no longer be edited.")
    max_chars = current_app.config["MAX_CONTENT_CHARS"]
    new_text = (new_text or "").strip()
    if not new_text:
        raise WorkflowError("Content cannot be empty.")
    if len(new_text) > max_chars:
        raise WorkflowError(f"Content is too long (maximum {max_chars:,} characters).")
    changed = False
    if new_text != sub.content_text:
        sub.content_text = new_text
        changed = True
    if alt_text is not None and alt_text.strip() != (sub.image_alt or ""):
        sub.image_alt = alt_text.strip()[:500]
        changed = True
    if not changed:
        raise WorkflowError("No changes were made.")
    sub.version += 1
    db.session.flush()
    add_revision(sub, "content_edited", user, "Content edited manually by the creator.")
    audit("content.edit", "submission", sub.id, user=user)


# ---------------------------------------------------------------- review workflow
def send_for_review(sub: ContentSubmission, user):
    if sub.author_id != user.id and user.role != "admin":
        raise WorkflowError("Only the author can send this content for review.")
    if sub.status not in ("checked", "changes_requested"):
        raise WorkflowError("This submission cannot be sent for review in its current state.")
    if not sub.is_check_current:
        raise WorkflowError("The content changed since the last check. Re-run the brand check before sending for review.")
    sub.status = "pending_review"
    sub.submitted_at = utcnow()
    db.session.flush()
    add_revision(sub, "sent_for_review", user, f"Sent for review at version {sub.version}.")
    audit("content.send_for_review", "submission", sub.id, user=user)


def record_review(sub: ContentSubmission, reviewer, decision: str, comment: str):
    """The ONLY place where approved / rejected / changes_requested are set – always by a human."""
    if not reviewer.can_review:
        raise WorkflowError("You are not authorised to review content.")
    if sub.author_id == reviewer.id:
        raise WorkflowError("You cannot review your own submission.")
    if decision not in ("approved", "rejected", "changes_requested", "comment"):
        raise WorkflowError("Invalid decision.")
    comment = (comment or "").strip()
    if decision == "comment":
        if not comment:
            raise WorkflowError("Please enter a comment.")
    else:
        if sub.status != "pending_review":
            raise WorkflowError("Only submissions that are pending review can be decided.")
        if decision in ("rejected", "changes_requested") and not comment:
            raise WorkflowError("A comment is required when rejecting or requesting changes.")
        if sub.checked_version != sub.version:
            raise WorkflowError("This submission has unchecked edits and cannot be decided.")
    db.session.add(Review(submission_id=sub.id, reviewer_id=reviewer.id, decision=decision, comment=comment,
                          content_version=sub.version))
    if decision != "comment":
        sub.status = decision
    db.session.flush()
    add_revision(sub, f"review_{decision}", reviewer, comment[:500])
    audit(f"review.{decision}", "submission", sub.id, comment[:300], user=reviewer)
