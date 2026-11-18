"""SQLAlchemy models. Portable across SQLite / MySQL / PostgreSQL."""
import re
import secrets
from datetime import datetime, timezone

from flask_login import UserMixin
from sqlalchemy.orm import validates
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db

ROLES = ("admin", "creator", "reviewer")
ROLE_LABELS = {"admin": "Administrator", "creator": "Content Creator", "reviewer": "Manager / Reviewer"}
SEVERITIES = ("High", "Medium", "Low")
SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}
CATEGORIES = (
    "Tone Consistency",
    "Terminology Consistency",
    "Fact Verification",
    "Promotional Claims",
    "Visual Consistency",
    "Accessibility",
)
CONTENT_TYPES = {
    "social": "Social media post",
    "advert": "Advertisement",
    "email": "Marketing email",
    "website": "Website content",
    "pdf": "PDF document",
}
TONE_STYLES = {
    "formal": "Formal",
    "professional": "Professional",
    "friendly": "Professional & friendly",
    "casual": "Casual",
}
SUBMISSION_STATUSES = ("checked", "pending_review", "changes_requested", "approved", "rejected")
STATUS_LABELS = {
    "checked": "Checked – not yet sent",
    "pending_review": "Pending review",
    "changes_requested": "Changes requested",
    "approved": "Approved",
    "rejected": "Rejected",
}
VERIFICATION = ("none", "needs_human_verification", "conflicts_with_approved_fact")
SUGGESTION_STATUSES = ("pending", "accepted", "rejected", "edited", "acknowledged")
REVIEW_DECISIONS = ("approved", "rejected", "changes_requested", "comment")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def utcnow() -> datetime:
    """Naive UTC timestamps keep SQLite and MySQL behaviour identical."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_invite_code() -> str:
    return secrets.token_hex(4).upper()


class Organisation(db.Model):
    __tablename__ = "organisations"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), unique=True, nullable=False)
    invite_code = db.Column(db.String(32), unique=True, nullable=False, default=new_invite_code)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    users = db.relationship("User", backref="organisation", lazy=True)
    guidelines = db.relationship("BrandGuideline", backref="organisation", lazy=True, cascade="all, delete-orphan")
    submissions = db.relationship("ContentSubmission", backref="organisation", lazy=True)


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("organisations.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="creator")
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    @validates("email")
    def _validate_email(self, _key, value):
        value = (value or "").strip().lower()
        if not EMAIL_RE.match(value):
            raise ValueError("Invalid email address.")
        return value

    @validates("role")
    def _validate_role(self, _key, value):
        if value not in ROLES:
            raise ValueError("Invalid role.")
        return value

    @property
    def is_active(self):  # Flask-Login hook
        return bool(self.active)

    @property
    def role_label(self):
        return ROLE_LABELS.get(self.role, self.role)

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def can_review(self):
        return self.role in ("admin", "reviewer")

    @property
    def can_submit(self):
        return self.role in ("admin", "creator")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class BrandGuideline(db.Model):
    """One brand profile. List-type fields are stored as one entry per line."""
    __tablename__ = "brand_guidelines"
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("organisations.id"), nullable=False, index=True)
    title = db.Column(db.String(150), nullable=False)
    company_name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, default="")
    voice_tone = db.Column(db.Text, default="")
    tone_style = db.Column(db.String(20), nullable=False, default="professional")
    approved_terms = db.Column(db.Text, default="")      # "Preferred | variant1, variant2"
    prohibited_words = db.Column(db.Text, default="")    # "word => replacement"
    unsupported_claims = db.Column(db.Text, default="")  # "phrase => replacement"
    brand_colours = db.Column(db.Text, default="")       # hex codes
    slogans = db.Column(db.Text, default="")
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    facts = db.relationship("ApprovedFact", backref="guideline", lazy=True, cascade="all, delete-orphan")
    assets = db.relationship("BrandAsset", backref="guideline", lazy=True, cascade="all, delete-orphan")

    @validates("tone_style")
    def _validate_tone(self, _key, value):
        if value not in TONE_STYLES:
            raise ValueError("Invalid tone style.")
        return value

    @property
    def logo(self):
        return next((a for a in self.assets if a.kind == "logo"), None)

    @property
    def document(self):
        return next((a for a in self.assets if a.kind == "guideline_doc"), None)


class ApprovedFact(db.Model):
    __tablename__ = "approved_facts"
    id = db.Column(db.Integer, primary_key=True)
    guideline_id = db.Column(db.Integer, db.ForeignKey("brand_guidelines.id"), nullable=False, index=True)
    statement = db.Column(db.Text, nullable=False)
    source = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)


class BrandAsset(db.Model):
    __tablename__ = "brand_assets"
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("organisations.id"), nullable=False, index=True)
    guideline_id = db.Column(db.Integer, db.ForeignKey("brand_guidelines.id"), nullable=False, index=True)
    kind = db.Column(db.String(30), nullable=False)  # logo | guideline_doc
    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    mime_type = db.Column(db.String(100))
    size_bytes = db.Column(db.Integer, default=0)
    extracted_text = db.Column(db.Text)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)


class ContentSubmission(db.Model):
    __tablename__ = "content_submissions"
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("organisations.id"), nullable=False, index=True)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    content_type = db.Column(db.String(20), nullable=False)
    content_text = db.Column(db.Text, nullable=False)
    original_text = db.Column(db.Text, nullable=False)
    image_filename = db.Column(db.String(255))
    image_original = db.Column(db.String(255))
    image_alt = db.Column(db.String(500), default="")
    pdf_filename = db.Column(db.String(255))
    pdf_original = db.Column(db.String(255))
    status = db.Column(db.String(30), nullable=False, default="checked", index=True)
    version = db.Column(db.Integer, nullable=False, default=1)
    checked_version = db.Column(db.Integer, nullable=False, default=0)
    score = db.Column(db.Integer)
    review_label = db.Column(db.String(80))
    ai_status = db.Column(db.String(300), default="")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    submitted_at = db.Column(db.DateTime)  # when last sent for review

    author = db.relationship("User", foreign_keys=[author_id])
    issues = db.relationship("ContentIssue", backref="submission", lazy=True, cascade="all, delete-orphan")
    reviews = db.relationship("Review", backref="submission", lazy=True, cascade="all, delete-orphan",
                              order_by="Review.created_at")
    revisions = db.relationship("RevisionHistory", backref="submission", lazy=True, cascade="all, delete-orphan",
                                order_by="RevisionHistory.id")

    @validates("content_type")
    def _validate_type(self, _key, value):
        if value not in CONTENT_TYPES:
            raise ValueError("Invalid content type.")
        return value

    @validates("status")
    def _validate_status(self, _key, value):
        if value not in SUBMISSION_STATUSES:
            raise ValueError("Invalid status.")
        return value

    @property
    def type_label(self):
        return CONTENT_TYPES.get(self.content_type, self.content_type)

    @property
    def status_label(self):
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def is_editable(self):
        return self.status in ("checked", "changes_requested")

    @property
    def is_check_current(self):
        return self.checked_version == self.version

    def current_issues(self):
        items = [i for i in self.issues if i.is_current]
        return sorted(items, key=lambda i: (SEVERITY_ORDER.get(i.severity, 9), i.id))


class ContentIssue(db.Model):
    __tablename__ = "content_issues"
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("content_submissions.id"), nullable=False, index=True)
    check_version = db.Column(db.Integer, nullable=False, default=1)
    is_current = db.Column(db.Boolean, nullable=False, default=True, index=True)
    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(40), nullable=False)
    severity = db.Column(db.String(10), nullable=False)
    original_text = db.Column(db.Text, default="")
    explanation = db.Column(db.Text, default="")
    guideline_violated = db.Column(db.Text, default="")
    suggested_correction = db.Column(db.Text, default="")
    related_source = db.Column(db.Text, default="")
    verification = db.Column(db.String(40), nullable=False, default="none")
    source = db.Column(db.String(10), nullable=False, default="rule")  # rule | ai
    rule_id = db.Column(db.String(60), default="")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    suggestion = db.relationship("CorrectionSuggestion", backref="issue", uselist=False, lazy=True,
                                 cascade="all, delete-orphan")

    @validates("severity")
    def _validate_severity(self, _key, value):
        if value not in SEVERITIES:
            raise ValueError("Invalid severity.")
        return value

    @validates("category")
    def _validate_category(self, _key, value):
        if value not in CATEGORIES:
            raise ValueError("Invalid category.")
        return value

    @validates("verification")
    def _validate_verification(self, _key, value):
        if value not in VERIFICATION:
            raise ValueError("Invalid verification state.")
        return value


class CorrectionSuggestion(db.Model):
    __tablename__ = "correction_suggestions"
    id = db.Column(db.Integer, primary_key=True)
    issue_id = db.Column(db.Integer, db.ForeignKey("content_issues.id"), nullable=False, unique=True)
    original_text = db.Column(db.Text, default="")
    suggested_text = db.Column(db.Text)  # None => no automatic replacement available
    status = db.Column(db.String(20), nullable=False, default="pending")
    final_text = db.Column(db.Text)
    decided_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    decided_at = db.Column(db.DateTime)

    @validates("status")
    def _validate_status(self, _key, value):
        if value not in SUGGESTION_STATUSES:
            raise ValueError("Invalid suggestion status.")
        return value


class Review(db.Model):
    __tablename__ = "reviews"
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("content_submissions.id"), nullable=False, index=True)
    reviewer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    decision = db.Column(db.String(20), nullable=False)
    comment = db.Column(db.Text, default="")
    content_version = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    reviewer = db.relationship("User", foreign_keys=[reviewer_id])

    @validates("decision")
    def _validate_decision(self, _key, value):
        if value not in REVIEW_DECISIONS:
            raise ValueError("Invalid review decision.")
        return value


class RevisionHistory(db.Model):
    """Append-only event log; every row carries a snapshot of the content."""
    __tablename__ = "revision_history"
    id = db.Column(db.Integer, primary_key=True)
    submission_id = db.Column(db.Integer, db.ForeignKey("content_submissions.id"), nullable=False, index=True)
    version = db.Column(db.Integer, nullable=False, default=1)
    event = db.Column(db.String(40), nullable=False)
    details = db.Column(db.Text, default="")
    content_snapshot = db.Column(db.Text, default="")
    status_snapshot = db.Column(db.String(30))
    score = db.Column(db.Integer)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id])


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    org_id = db.Column(db.Integer, db.ForeignKey("organisations.id"), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    action = db.Column(db.String(80), nullable=False)
    entity_type = db.Column(db.String(40))
    entity_id = db.Column(db.Integer)
    details = db.Column(db.Text, default="")
    ip_address = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id])
