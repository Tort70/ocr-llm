"""
Upload handler with OCR-LLM pilot routing.

When the pilot flag is enabled on a request, this handler creates a job in
the OCR-LLM filesystem queue (/srv/ocr-llm/jobs/pending/) instead of
following the legacy upload path.

The web layer ONLY creates the job and stops. The worker (managed by systemd)
independently picks up pending jobs. No OCR, LLM, or processing logic runs here.
"""

import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, request, jsonify, g
from werkzeug.utils import secure_filename

logger = logging.getLogger(__name__)

upload_bp = Blueprint("upload", __name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OCR_LLM_ROOT = Path("/srv/ocr-llm")
JOBS_DIR = OCR_LLM_ROOT / "jobs"
PENDING_DIR = JOBS_DIR / "pending"
LEGACY_UPLOAD_DIR = OCR_LLM_ROOT / "input" / "uploads"

ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "tiff", "webp"}
ALLOWED_ROLES = {"student", "instructor", "admin", "system"}

# Maximum upload size (10 MB). Enforced at application level as a safeguard.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Current UTC time in ISO 8601 format matching worker.py convention."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_extension(filename: str) -> str:
    """Return lowercase extension without dot, or empty string."""
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _sha256(file_bytes: bytes) -> str:
    """Compute SHA-256 checksum in the format required by the job contract."""
    digest = hashlib.sha256(file_bytes).hexdigest()
    return f"sha256:{digest}"


def _next_job_id() -> str:
    """
    Generate the next unique job ID for today: job-YYYYMMDD-NNN.

    Scans ALL job directories (pending, running, completed, failed) for
    today's date prefix to find the highest existing sequence number,
    then increments by one.
    """
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    prefix = f"job-{today}-"

    max_seq = 0
    for subdir in ("pending", "running", "completed", "failed"):
        parent = JOBS_DIR / subdir
        if not parent.exists():
            continue
        for entry in parent.iterdir():
            if entry.is_dir() and entry.name.startswith(prefix):
                try:
                    seq = int(entry.name[len(prefix):])
                    max_seq = max(max_seq, seq)
                except ValueError:
                    continue

    next_seq = max_seq + 1
    if next_seq > 999:
        raise RuntimeError("Daily job limit (999) reached")

    return f"{prefix}{next_seq:03d}"


def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically via temp file + rename (same as worker.py)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    tmp.rename(path)


# ---------------------------------------------------------------------------
# Upload handler (POST) — processes the submitted form
# ---------------------------------------------------------------------------

@upload_bp.route("/upload", methods=["POST"])
def handle_upload():
    """
    Accept a document upload.

    Routing decision (OCR-LLM pilot):
      If the request includes `ocr_llm_pilot=true` (form field or JSON),
      the file is routed to the OCR-LLM job queue. Otherwise, the legacy
      upload path is used.

    This is the ONLY integration point between the web layer and the
    OCR-LLM system. Everything after job creation is handled by the
    independent worker process.
    """

    # --- Authentication context -------------------------------------------
    # In production, these come from session middleware / auth decorators.
    # Shown here as request-level values for the pilot; replace with your
    # actual auth layer.
    user_id = request.form.get("user_id", getattr(g, "user_id", "unknown"))
    user_role = request.form.get("user_role", getattr(g, "user_role", "instructor"))
    session_id = request.form.get("session_id", getattr(g, "session_id", None))

    if user_role not in ALLOWED_ROLES:
        return jsonify({"error": f"Invalid role: {user_role}"}), 400

    # --- File validation --------------------------------------------------
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    uploaded = request.files["file"]
    if uploaded.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    ext = _file_extension(uploaded.filename)
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400

    file_bytes = uploaded.read()
    if len(file_bytes) == 0:
        return jsonify({"error": "Empty file"}), 400
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        return jsonify({"error": "File exceeds maximum size"}), 400

    safe_filename = secure_filename(uploaded.filename)

    # =====================================================================
    # ROUTING DECISION — OCR-LLM pilot flag
    #
    # This is the single condition that determines whether the upload
    # enters the OCR-LLM job queue or follows the legacy path.
    # In production, this could be a feature flag, admin toggle, or
    # instructor-only checkbox ("Process with OCR-LLM (pilot)").
    # =====================================================================
    use_ocr_llm = request.form.get("ocr_llm_pilot", "").lower() == "true"

    if not use_ocr_llm:
        # =================================================================
        # LEGACY PATH — existing upload behavior, unchanged
        # =================================================================
        LEGACY_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        dest = LEGACY_UPLOAD_DIR / safe_filename
        dest.write_bytes(file_bytes)

        logger.info("Legacy upload: %s (%d bytes)", safe_filename, len(file_bytes))
        return jsonify({
            "status": "uploaded",
            "filename": safe_filename,
        }), 200

    # =====================================================================
    # OCR-LLM PILOT PATH
    #
    # Steps:
    #   1. Generate unique job ID
    #   2. Create job directory tree in jobs/pending/
    #   3. Write input file
    #   4. Write job.json (contract v1.0)
    #   5. Write status.txt
    #   6. Log and return — processing STOPS here
    #
    # The worker (systemd timer, polls every minute) will independently
    # pick up this job. The web layer never invokes OCR or LLM logic.
    # =====================================================================

    try:
        # --- Step 1: Generate job ID --------------------------------------
        job_id = _next_job_id()

        # --- Step 2: Create job directory tree ----------------------------
        job_dir = PENDING_DIR / job_id
        (job_dir / "input").mkdir(parents=True)
        (job_dir / "work").mkdir()
        (job_dir / "output").mkdir()
        (job_dir / "audit").mkdir()

        # --- Step 3: Write uploaded file to input/ -----------------------
        input_path = job_dir / "input" / safe_filename
        input_path.write_bytes(file_bytes)

        # --- Step 4: Write job.json ---------------------------------------
        checksum = _sha256(file_bytes)

        job_manifest = {
            "contract_version": "1.0",
            "job": {
                "id": job_id,
                "created_at": _now_iso(),
                "started_at": None,
                "completed_at": None,
            },
            "creator": {
                "system": "exam-portal",
                "user_id": user_id,
                "role": user_role,
                "session_id": session_id,
            },
            "input": {
                "filename": safe_filename,
                "type": ext,
                "checksum": checksum,
                "size_bytes": len(file_bytes),
                "page_count": None,  # determined by worker during OCR
            },
            "task": {
                "ocr": {
                    "engine": "tesseract",
                    "language": "eng",
                    "dpi": 300,
                },
                "llm": {
                    "provider": "ollama",
                    "model": "mistral",
                    "temperature": 0.0,
                    "max_tokens": 4096,
                },
                "prompt_template": "exam-draft-v1",
                "prompt_variables": {},
            },
            "constraints": {
                "exam_mode": True,
                "allow_network_context": False,
                "time_limit_seconds": 300,
                "content_filter": True,
                "pii_redaction": False,
                "rubric_id": None,
            },
            "status": "pending",
            "_extensions": {},
        }

        _atomic_write(
            job_dir / "job.json",
            json.dumps(job_manifest, indent=2) + "\n",
        )

        # --- Step 5: Write status.txt ------------------------------------
        # status.txt is the AUTHORITATIVE execution state per JOB_CONTRACT.
        _atomic_write(job_dir / "status.txt", "pending\n")

        # --- Step 6: Log and return --------------------------------------
        # Server-side log records the routing decision and job ID for ops.
        logger.info(
            "OCR-LLM pilot: routed upload to job queue — "
            "job_id=%s file=%s size=%d user=%s",
            job_id, safe_filename, len(file_bytes), user_id,
        )

        # ================================================================
        # PROCESSING STOPS HERE.
        #
        # The web layer has created a valid pending job. The systemd-managed
        # worker will independently discover it in jobs/pending/, move it
        # to running/, execute the OCR→Normalize→LLM pipeline, and move it
        # to completed/ or failed/.
        #
        # We do NOT:
        #   - invoke worker.py
        #   - run OCR or LLM
        #   - wait for results
        #   - poll for completion
        #   - read output files
        #
        # This is intentional: the web layer's only job is to create a
        # valid filesystem job and return control to the user.
        # ================================================================

        return jsonify({
            "status": "accepted",
            "message": "Document uploaded. Processing has begun.",
            "job_id": job_id,
        }), 202  # 202 Accepted — request received, processing deferred

    except Exception:
        logger.exception("Failed to create OCR-LLM job for upload: %s", safe_filename)

        # Clean up partial job directory if it was created
        if "job_dir" in locals() and job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)

        return jsonify({"error": "Failed to queue document for processing"}), 500
