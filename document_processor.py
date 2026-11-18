"""Secure upload validation and PDF text extraction."""
from __future__ import annotations

import os
import uuid

import fitz  # PyMuPDF
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

IMAGE_EXT = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP"}
PDF_EXT = {"pdf"}


class UploadError(ValueError):
    """Raised with a user-friendly message when an upload is rejected."""


def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def save_upload(file_storage, kind: str, cfg) -> dict:
    """Validate and store an upload. kind: 'image' | 'pdf'.

    Checks extension, size, magic bytes / real decodability. Files are stored under random UUID
    names outside the static folder. Returns metadata dict.
    """
    if file_storage is None or not file_storage.filename:
        raise UploadError("No file selected.")
    original = secure_filename(file_storage.filename) or "upload"
    ext = _ext(original)
    if kind == "image":
        if ext not in IMAGE_EXT:
            raise UploadError("Unsupported image format. Allowed: PNG, JPG, JPEG, WEBP.")
        limit = cfg["MAX_IMAGE_BYTES"]
    elif kind == "pdf":
        if ext not in PDF_EXT:
            raise UploadError("Unsupported document format. Only PDF files are allowed.")
        limit = cfg["MAX_PDF_BYTES"]
    else:
        raise UploadError("Unknown upload type.")

    data = file_storage.read(limit + 1)
    if len(data) > limit:
        raise UploadError(f"File too large. Maximum size is {limit // (1024 * 1024)} MB.")
    if not data:
        raise UploadError("The uploaded file is empty.")

    if kind == "pdf":
        if not data.startswith(b"%PDF-"):
            raise UploadError("This file is not a valid PDF.")
        mime = "application/pdf"
    else:
        import io
        try:
            with Image.open(io.BytesIO(data)) as im:
                im.verify()
            with Image.open(io.BytesIO(data)) as im:
                fmt = im.format
                if im.width * im.height > 40_000_000:
                    raise UploadError("Image dimensions are too large.")
        except (UnidentifiedImageError, OSError, SyntaxError):
            raise UploadError("This file is not a valid image.")
        if fmt != IMAGE_EXT[ext] and not (fmt == "JPEG" and ext in ("jpg", "jpeg")):
            raise UploadError("The image content does not match its file extension.")
        mime = Image.MIME.get(fmt, "application/octet-stream")

    os.makedirs(cfg["UPLOAD_FOLDER"], exist_ok=True)
    stored = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(cfg["UPLOAD_FOLDER"], stored)
    with open(path, "wb") as fh:
        fh.write(data)
    return {"original": original, "stored": stored, "path": path, "mime": mime, "size": len(data)}


def extract_pdf_text(path: str, max_pages: int = 60, max_chars: int = 50000) -> str:
    try:
        with fitz.open(path) as doc:
            if doc.needs_pass:
                raise UploadError("Password-protected PDFs are not supported.")
            parts = []
            for i, page in enumerate(doc):
                if i >= max_pages:
                    break
                parts.append(page.get_text("text"))
                if sum(len(p) for p in parts) > max_chars:
                    break
    except UploadError:
        raise
    except Exception as exc:  # fitz raises several error types for corrupt files
        raise UploadError("The PDF could not be read. It may be corrupted.") from exc
    text = "\n".join(parts).strip()[:max_chars]
    if not text:
        raise UploadError("No selectable text was found in this PDF (it may be a scanned image).")
    return text


def delete_file(upload_folder: str, stored_name: str | None) -> None:
    if not stored_name:
        return
    path = os.path.join(upload_folder, os.path.basename(stored_name))
    try:
        os.remove(path)
    except OSError:
        pass
