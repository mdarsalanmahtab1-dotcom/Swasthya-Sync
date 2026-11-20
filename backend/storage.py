"""
SwasthyaSync — patient document storage.

GCS_BUCKET set  -> private Google Cloud Storage bucket (credentials from the Cloud Run
                   service account via ADC). Paths are stored as "gs://bucket/object".
GCS_BUCKET unset -> local folder backend/uploads/ (dev only), stored as "local:<filename>".
                   This folder is NOT publicly mounted.

Files are never exposed by public or signed URLs; staff fetch them through the
authenticated GET /api/documents/{doc_id} endpoint, and the PDF generator reads them here.
"""

from __future__ import annotations

import logging
import os
import re
import urllib.request

logger = logging.getLogger(__name__)

GCS_BUCKET = (os.environ.get("GCS_BUCKET") or "").strip()
LOCAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")

_gcs_client = None


def _bucket():
    global _gcs_client
    if _gcs_client is None:
        from google.cloud import storage as gcs  # google-cloud-storage
        _gcs_client = gcs.Client()
    return _gcs_client.bucket(GCS_BUCKET)


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name or "file"))[:120]


def upload_bytes(path: str, data: bytes, content_type: str) -> str:
    """Store bytes; return the stored path (gs://... or local:...)."""
    if GCS_BUCKET:
        blob = _bucket().blob(path)
        blob.upload_from_string(data, content_type=content_type)
        return f"gs://{GCS_BUCKET}/{path}"
    os.makedirs(LOCAL_DIR, exist_ok=True)
    fname = _safe_name(path)
    with open(os.path.join(LOCAL_DIR, fname), "wb") as f:
        f.write(data)
    return f"local:{fname}"


def download_bytes(stored: str) -> bytes | None:
    """Read a stored file. Also understands legacy values (/uploads/x, local paths, old signed URLs)."""
    stored = (stored or "").strip()
    if not stored:
        return None
    try:
        if stored.startswith("gs://"):
            bucket_name, _, obj = stored[5:].partition("/")
            if GCS_BUCKET and bucket_name == GCS_BUCKET:
                return _bucket().blob(obj).download_as_bytes()
            from google.cloud import storage as gcs
            return gcs.Client().bucket(bucket_name).blob(obj).download_as_bytes()
        if stored.startswith("local:") or stored.startswith("/uploads/"):
            p = os.path.join(LOCAL_DIR, _safe_name(stored.split(":", 1)[-1]))
            return open(p, "rb").read() if os.path.exists(p) else None
        if stored.startswith("http://") or stored.startswith("https://"):
            # Legacy Supabase signed URLs from before the migration
            req = urllib.request.Request(stored, headers={"User-Agent": "SwasthyaSync/1.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                return resp.read()
        if os.path.exists(stored):
            return open(stored, "rb").read()
    except Exception as e:
        logger.error(f"download_bytes failed ({type(e).__name__})")
    return None


def delete(stored: str) -> None:
    try:
        if stored.startswith("gs://"):
            _, _, obj = stored[5:].partition("/")
            _bucket().blob(obj).delete()
        elif stored.startswith("local:"):
            p = os.path.join(LOCAL_DIR, _safe_name(stored[6:]))
            if os.path.exists(p):
                os.remove(p)
    except Exception as e:
        logger.warning(f"delete failed ({type(e).__name__})")


def guess_content_type(stored: str) -> str:
    s = stored.lower()
    if s.endswith(".png"):
        return "image/png"
    if s.endswith(".webp"):
        return "image/webp"
    if s.endswith(".pdf"):
        return "application/pdf"
    if s.endswith(".heic") or s.endswith(".heif"):
        return "image/heic"
    return "image/jpeg"
