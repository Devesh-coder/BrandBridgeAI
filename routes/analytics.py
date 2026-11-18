"""Analytics: every number comes from database records scoped to the user's organisation."""
from collections import Counter, defaultdict
from datetime import datetime

from flask import Blueprint, jsonify, render_template
from flask_login import current_user, login_required

from models import ContentIssue, ContentSubmission, Review, RevisionHistory

bp = Blueprint("analytics", __name__)


def _month_keys(n=6):
    now = datetime.utcnow()
    keys = []
    y, m = now.year, now.month
    for _ in range(n):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(keys))


def compute_stats(user) -> dict:
    subq = ContentSubmission.query.filter_by(org_id=user.org_id)
    if user.role == "creator":
        subq = subq.filter_by(author_id=user.id)
    subs = subq.all()
    ids = [s.id for s in subs]
    status = Counter(s.status for s in subs)
    issues = ContentIssue.query.filter(ContentIssue.submission_id.in_(ids), ContentIssue.is_current.is_(True)).all() if ids else []
    sev = Counter(i.severity for i in issues)
    cat = Counter(i.category for i in issues)
    common = Counter((i.title, i.category) for i in issues).most_common(8)
    verification = sum(1 for i in issues if i.verification != "none")

    months = _month_keys()
    sub_by_month = Counter(s.created_at.strftime("%Y-%m") for s in subs)
    reviews = Review.query.filter(Review.submission_id.in_(ids), Review.decision != "comment").all() if ids else []
    rev_by_month = Counter(r.created_at.strftime("%Y-%m") for r in reviews)

    trend_rows = RevisionHistory.query.filter(RevisionHistory.submission_id.in_(ids), RevisionHistory.event == "check_run",
                                              RevisionHistory.score.isnot(None)).order_by(RevisionHistory.id).all() if ids else []
    by_day = defaultdict(list)
    for r in trend_rows:
        by_day[r.created_at.strftime("%Y-%m-%d")].append(r.score)
    scores = [s.score for s in subs if s.score is not None]

    return {
        "totals": {
            "submissions": len(subs), "approved": status.get("approved", 0),
            "pending": status.get("pending_review", 0), "rejected": status.get("rejected", 0),
            "changes_requested": status.get("changes_requested", 0), "checked": status.get("checked", 0),
            "issues": len(issues), "high": sev.get("High", 0), "needs_verification": verification,
            "avg_score": round(sum(scores) / len(scores)) if scores else None,
        },
        "status": {k: status.get(k, 0) for k in ("checked", "pending_review", "changes_requested", "approved", "rejected")},
        "severity": {k: sev.get(k, 0) for k in ("High", "Medium", "Low")},
        "categories": dict(cat),
        "common_mistakes": [{"title": t, "category": c, "count": n} for (t, c), n in common],
        "monthly": {"labels": months, "submissions": [sub_by_month.get(m, 0) for m in months],
                    "reviews": [rev_by_month.get(m, 0) for m in months]},
        "score_trend": {"labels": sorted(by_day), "values": [round(sum(by_day[d]) / len(by_day[d]), 1) for d in sorted(by_day)]},
    }


@bp.route("/analytics")
@login_required
def page():
    return render_template("analytics.html", stats=compute_stats(current_user))


@bp.route("/analytics/data")
@login_required
def data():
    return jsonify(compute_stats(current_user))
