"""Application configuration. Secrets come from environment variables only."""
import os
import secrets
from datetime import timedelta

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite:///brandbridge.db")
    # Render/Heroku style URLs use the legacy "postgres://" scheme.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


class Config:
    APP_ENV = os.environ.get("APP_ENV", "development")
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

    # SQLite by default; set DATABASE_URL=mysql+pymysql://user:pw@host/db to migrate.
    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Uploads are stored OUTSIDE the static folder and only served via
    # authenticated, organisation-scoped routes.
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", os.path.join(BASE_DIR, "uploads"))
    MAX_CONTENT_LENGTH = 12 * 1024 * 1024  # hard request limit (12 MB)
    MAX_IMAGE_BYTES = 5 * 1024 * 1024
    MAX_PDF_BYTES = 10 * 1024 * 1024
    MAX_PDF_PAGES = 60
    MAX_CONTENT_CHARS = 50000

    # Session / cookie hardening
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = APP_ENV == "production"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    REMEMBER_COOKIE_HTTPONLY = True

    WTF_CSRF_TIME_LIMIT = None  # token valid for the whole session

    # Optional LLM integration (basic checking never depends on it)
    AI_PROVIDER = os.environ.get("AI_PROVIDER", "none").lower()  # anthropic | openai | none
    AI_API_KEY = os.environ.get("AI_API_KEY", "")
    AI_MODEL = os.environ.get("AI_MODEL", "")
    AI_BASE_URL = os.environ.get("AI_BASE_URL", "")
    AI_TIMEOUT = int(os.environ.get("AI_TIMEOUT", "25"))


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite://"
    AI_PROVIDER = "none"
    AI_API_KEY = ""


def validate_production(cfg) -> None:
    if cfg.get("APP_ENV") == "production" and not os.environ.get("SECRET_KEY"):
        raise RuntimeError("SECRET_KEY environment variable must be set in production.")
