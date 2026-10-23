#!/usr/bin/env python3
"""
OCR-LLM Single-Job Worker

Processes one job from the pending queue, then exits.
Filesystem is the source of truth. No database. No concurrency.

Usage:
    python3 worker.py

Exit codes:
    0 - Job completed successfully, or no work to do
    1 - Job failed (error details in audit/error.txt)
    2 - Another worker is active (job in running/)
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# =============================================================================
# Configuration
# =============================================================================

ROOT = Path("/srv/ocr-llm")
JOBS_DIR = ROOT / "jobs"
PENDING_DIR = JOBS_DIR / "pending"
RUNNING_DIR = JOBS_DIR / "running"
COMPLETED_DIR = JOBS_DIR / "completed"
FAILED_DIR = JOBS_DIR / "failed"

OLLAMA_MODEL = "mistral"


# =============================================================================
# Utility Functions
# =============================================================================

def now_iso() -> str:
    """Return current UTC time in ISO8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write(path: Path, content: str) -> None:
    """
    Write content to file atomically.
    Writes to a temp file first, then renames to avoid partial writes.
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content)
    tmp_path.rename(path)


def append_event(audit_dir: Path, event_type: str, details: dict = None) -> None:
    """
    Append a timestamped event to audit/events.jsonl.

    Events are append-only and provide a complete audit trail
    of everything that happened during job processing.
    """
    event = {
        "timestamp": now_iso(),
        "event": event_type,
    }
    if details:
        event["details"] = details

    events_file = audit_dir / "events.jsonl"
    with open(events_file, "a") as f:
        f.write(json.dumps(event) + "\n")


def update_job_timestamp(job_json_path: Path, field: str) -> None:
    """
    Update a timestamp field in job.json.

    Only started_at and completed_at may be updated.
    All other fields are immutable per the job contract.
    """
    job = json.loads(job_json_path.read_text())
    job["job"][field] = now_iso()
    atomic_write(job_json_path, json.dumps(job, indent=2) + "\n")


def write_error(audit_dir: Path, code: str, stage: str, message: str) -> None:
    """
    Write failure details to audit/error.txt.

    Errors are NEVER written to job.json per the job contract.
    This file is the authoritative record of what went wrong.
    """
    error_content = f"""code: {code}
stage: {stage}
timestamp: {now_iso()}
message: {message}
"""
    (audit_dir / "error.txt").write_text(error_content)


# =============================================================================
# Pipeline Stages (Stubs)
# =============================================================================

def run_ocr(job_dir: Path, job_config: dict) -> str:
    """
    OCR Stage: Extract text from input files.

    Reads from: input/
    Writes to: work/ocr.txt

    Uses Tesseract OCR with pdftoppm for PDF rendering.
    Respects language and DPI settings from job_config.task.ocr.
    """
    input_dir = job_dir / "input"
    work_dir = job_dir / "work"

    # Find input file (first non-hidden file in input/)
    input_files = [f for f in input_dir.iterdir() if not f.name.startswith(".")]

    if not input_files:
        raise ValueError("No input files found")

    input_file = input_files[0]
    ocr_config = job_config["task"]["ocr"]

    language = ocr_config.get("language", "eng")
    dpi = ocr_config.get("dpi", 300)

    # Create temp directory for page images
    pages_dir = work_dir / "pages"
    pages_dir.mkdir(exist_ok=True)

    # Step 1: Convert PDF to PNG images using pdftoppm
    # Output files will be named: page-1.png, page-2.png, etc.
    pdf_result = subprocess.run(
        [
            "pdftoppm",
            "-png",
            "-r", str(dpi),
            str(input_file),
            str(pages_dir / "page"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    if pdf_result.returncode != 0:
        raise RuntimeError(f"pdftoppm failed: {pdf_result.stderr}")

    # Step 2: Run Tesseract on each page image
    page_images = sorted(pages_dir.glob("page-*.png"))

    if not page_images:
        raise RuntimeError("No page images generated from PDF")

    ocr_pages = []
    for i, page_image in enumerate(page_images, start=1):
        # Tesseract outputs to stdout with "-" as output base
        tess_result = subprocess.run(
            [
                "tesseract",
                str(page_image),
                "stdout",
                "-l", language,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if tess_result.returncode != 0:
            raise RuntimeError(f"Tesseract failed on page {i}: {tess_result.stderr}")

        page_text = tess_result.stdout
        ocr_pages.append(f"--- PAGE {i} ---\n{page_text}")

    # Step 3: Combine all pages
    ocr_text = "\n".join(ocr_pages)

    # Write raw OCR output
    ocr_output = work_dir / "ocr.txt"
    ocr_output.write_text(ocr_text)

    return ocr_text


def run_normalize(job_dir: Path, ocr_text: str) -> str:
    """
    Normalization Stage: Clean and chunk OCR output.

    Reads from: work/ocr.txt (or passed ocr_text)
    Writes to: work/normalized.txt

    Performs minimal cleanup to improve LLM comprehension:
    - Collapse excessive whitespace
    - Remove common OCR artifacts
    - Preserve paragraph structure
    """
    import re

    work_dir = job_dir / "work"

    normalized = ocr_text

    # Collapse multiple blank lines to single blank line (preserve paragraphs)
    normalized = re.sub(r'\n{3,}', '\n\n', normalized)

    # Collapse multiple spaces to single space
    normalized = re.sub(r'[ \t]{2,}', ' ', normalized)

    # Remove trailing whitespace on each line
    normalized = re.sub(r'[ \t]+$', '', normalized, flags=re.MULTILINE)

    # Remove leading whitespace on each line (OCR sometimes indents randomly)
    normalized = re.sub(r'^[ \t]+', '', normalized, flags=re.MULTILINE)

    # Strip leading/trailing whitespace from entire document
    normalized = normalized.strip()

    normalized_output = work_dir / "normalized.txt"
    normalized_output.write_text(normalized)

    return normalized


def run_llm(job_dir: Path, job_config: dict, text: str) -> dict:
    """
    LLM Stage: Process text through Ollama/Mistral.

    Reads: normalized text
    Writes:
        - audit/llm-request.json (exact prompt sent)
        - audit/llm-response.json (raw response)
        - output/result.json (structured result)
        - output/result.txt (human-readable result)

    Uses local Ollama inference only. No external API calls.
    """
    audit_dir = job_dir / "audit"
    output_dir = job_dir / "output"

    llm_config = job_config["task"]["llm"]

    # --- Load prompt template from processing/prompts/ ---
    # The template name comes from job.json.task.prompt_template.
    # Each template is a plain-text file with an {ocr_text} placeholder
    # that gets replaced with the normalized OCR output.

    template_name = job_config["task"].get("prompt_template")

    if not template_name:
        raise ValueError(
            "job.json.task.prompt_template is missing or null — "
            "every job must specify a prompt template"
        )

    template_path = ROOT / "processing" / "prompts" / f"{template_name}.txt"

    if not template_path.is_file():
        raise FileNotFoundError(
            f"Prompt template not found: {template_path}"
        )

    template_text = template_path.read_text()

    if "{ocr_text}" not in template_text:
        raise ValueError(
            f"Template '{template_name}' is missing the required "
            "{{ocr_text}} placeholder"
        )

    prompt = template_text.replace("{ocr_text}", text)

    # Prepare request payload (written to audit BEFORE the LLM call)
    request_payload = {
        "prompt_template": template_name,
        "model": llm_config.get("model", OLLAMA_MODEL),
        "prompt": prompt,
        "options": {
            "temperature": llm_config.get("temperature", 0.0),
            "num_predict": llm_config.get("max_tokens", 4096),
        },
        "stream": False,
    }

    # Write request to audit log BEFORE calling LLM
    # This ensures we have a record even if the call fails
    (audit_dir / "llm-request.json").write_text(
        json.dumps(request_payload, indent=2) + "\n"
    )

    # Call Ollama via CLI
    # Using subprocess for simplicity and no external dependencies
    try:
        result = subprocess.run(
            [
                "ollama", "run",
                llm_config.get("model", OLLAMA_MODEL),
                "--nowordwrap",
            ],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )

        if result.returncode != 0:
            raise RuntimeError(f"Ollama failed: {result.stderr}")

        response_text = result.stdout.strip()

    except subprocess.TimeoutExpired:
        raise RuntimeError("LLM inference timed out after 300 seconds")
    except FileNotFoundError:
        raise RuntimeError("Ollama not found. Is it installed?")

    # Build response object
    response_payload = {
        "model": llm_config.get("model", OLLAMA_MODEL),
        "response": response_text,
        "completed_at": now_iso(),
    }

    # Write response to audit log
    (audit_dir / "llm-response.json").write_text(
        json.dumps(response_payload, indent=2) + "\n"
    )

    # Write final outputs
    output_result = {
        "job_id": job_config["job"]["id"],
        "model": llm_config.get("model", OLLAMA_MODEL),
        "response": response_text,
        "processed_at": now_iso(),
    }

    (output_dir / "result.json").write_text(
        json.dumps(output_result, indent=2) + "\n"
    )
    (output_dir / "result.txt").write_text(response_text + "\n")

    return response_payload


# =============================================================================
# Job Processing
# =============================================================================

def process_job(job_dir: Path) -> None:
    """
    Execute the full pipeline for a single job.

    Stages:
        1. OCR - extract text from input
        2. Normalize - clean and chunk text
        3. LLM - process through Ollama/Mistral

    All intermediate files go to work/.
    Final outputs go to output/.
    Audit trail goes to audit/.
    """
    job_json_path = job_dir / "job.json"
    audit_dir = job_dir / "audit"

    job_config = json.loads(job_json_path.read_text())
    job_id = job_config["job"]["id"]

    # --- OCR Stage ---
    append_event(audit_dir, "stage_started", {"stage": "ocr"})
    try:
        ocr_text = run_ocr(job_dir, job_config)
    except Exception as e:
        raise StageError("ocr", "OCR_FAILED", str(e))
    append_event(audit_dir, "stage_completed", {"stage": "ocr"})

    # --- Normalization Stage ---
    append_event(audit_dir, "stage_started", {"stage": "normalize"})
    try:
        normalized_text = run_normalize(job_dir, ocr_text)
    except Exception as e:
        raise StageError("normalize", "NORMALIZE_FAILED", str(e))
    append_event(audit_dir, "stage_completed", {"stage": "normalize"})

    # --- LLM Stage ---
    append_event(audit_dir, "stage_started", {"stage": "llm"})
    try:
        run_llm(job_dir, job_config, normalized_text)
    except subprocess.TimeoutExpired:
        raise StageError("llm", "LLM_TIMEOUT", "Inference exceeded time limit")
    except Exception as e:
        raise StageError("llm", "LLM_API_ERROR", str(e))
    append_event(audit_dir, "stage_completed", {"stage": "llm"})


class StageError(Exception):
    """Error during a pipeline stage. Carries stage name and error code."""
    def __init__(self, stage: str, code: str, message: str):
        self.stage = stage
        self.code = code
        self.message = message
        super().__init__(message)


# =============================================================================
# Main Worker Logic
# =============================================================================

def get_oldest_pending_job() -> Path | None:
    """
    Find the oldest job in pending/.

    Jobs are sorted by directory name (job-YYYYMMDD-NNN format),
    which ensures chronological ordering.
    """
    if not PENDING_DIR.exists():
        return None

    pending_jobs = sorted(PENDING_DIR.iterdir())

    # Filter to only job directories (skip hidden files, etc.)
    pending_jobs = [
        j for j in pending_jobs
        if j.is_dir() and j.name.startswith("job-")
    ]

    return pending_jobs[0] if pending_jobs else None


def main() -> int:
    """
    Worker entry point.

    Returns:
        0 - Success or no work
        1 - Job failed
        2 - Another worker active
    """

    # -------------------------------------------------------------------------
    # Step 1: Check if another worker is active
    # -------------------------------------------------------------------------
    # If any job exists in running/, another worker owns it.
    # We exit immediately to avoid concurrent processing.

    if RUNNING_DIR.exists():
        running_jobs = [
            j for j in RUNNING_DIR.iterdir()
            if j.is_dir() and j.name.startswith("job-")
        ]
        if running_jobs:
            print(f"Another worker is active: {running_jobs[0].name}", file=sys.stderr)
            return 2

    # -------------------------------------------------------------------------
    # Step 2: Find work to do
    # -------------------------------------------------------------------------

    job_dir = get_oldest_pending_job()

    if job_dir is None:
        print("No pending jobs. Exiting.")
        return 0

    job_id = job_dir.name
    print(f"Processing job: {job_id}")

    # -------------------------------------------------------------------------
    # Step 3: Claim the job
    # -------------------------------------------------------------------------
    # Move from pending/ to running/ atomically.
    # This prevents other workers from picking up the same job.

    RUNNING_DIR.mkdir(parents=True, exist_ok=True)
    running_job_dir = RUNNING_DIR / job_id

    try:
        # Atomic move (same filesystem)
        job_dir.rename(running_job_dir)
    except OSError as e:
        print(f"Failed to claim job: {e}", file=sys.stderr)
        return 1

    job_dir = running_job_dir  # Update reference

    # -------------------------------------------------------------------------
    # Step 4: Record successful claim
    # -------------------------------------------------------------------------
    # Log worker_claimed immediately after atomic move succeeds.
    # This provides an audit record of which process owns the job.

    audit_dir = job_dir / "audit"
    claim_event = {
        "event": "worker_claimed",
        "timestamp": now_iso(),
        "pid": os.getpid(),
    }
    with open(audit_dir / "events.jsonl", "a") as f:
        f.write(json.dumps(claim_event) + "\n")

    # -------------------------------------------------------------------------
    # Step 5: Update status to processing
    # -------------------------------------------------------------------------
    # status.txt is the AUTHORITATIVE execution state.
    # job.json.status is informational only (we don't update it).

    status_file = job_dir / "status.txt"
    job_json_path = job_dir / "job.json"

    atomic_write(status_file, "processing\n")

    # -------------------------------------------------------------------------
    # Step 6: Record started_at timestamp
    # -------------------------------------------------------------------------
    # IMPORTANT: started_at is written ONLY AFTER:
    #   1. Job directory moved from pending/ to running/ (Step 3)
    #   2. status.txt updated to "processing" (Step 5)
    # This guarantees no job appears "started" while still in pending/.

    update_job_timestamp(job_json_path, "started_at")
    append_event(audit_dir, "job_started", {"location": str(job_dir)})

    # -------------------------------------------------------------------------
    # Step 7: Execute pipeline
    # -------------------------------------------------------------------------

    try:
        process_job(job_dir)

    except StageError as e:
        # ---------------------------------------------------------------------
        # Pipeline stage failed - handle gracefully
        # ---------------------------------------------------------------------
        print(f"Job failed at stage '{e.stage}': {e.message}", file=sys.stderr)

        # Write error to audit/error.txt (NEVER to job.json)
        write_error(audit_dir, e.code, e.stage, e.message)
        append_event(audit_dir, "job_failed", {
            "code": e.code,
            "stage": e.stage,
            "message": e.message,
        })

        # Update status.txt (authoritative state)
        atomic_write(status_file, "failed\n")

        # Move to failed/
        FAILED_DIR.mkdir(parents=True, exist_ok=True)
        failed_job_dir = FAILED_DIR / job_id
        job_dir.rename(failed_job_dir)

        print(f"Job moved to: {failed_job_dir}")
        return 1

    except Exception as e:
        # ---------------------------------------------------------------------
        # Unexpected error - still handle gracefully
        # ---------------------------------------------------------------------
        print(f"Unexpected error: {e}", file=sys.stderr)

        write_error(audit_dir, "INTERNAL_ERROR", "unknown", str(e))
        append_event(audit_dir, "job_failed", {
            "code": "INTERNAL_ERROR",
            "stage": "unknown",
            "message": str(e),
        })

        atomic_write(status_file, "failed\n")

        FAILED_DIR.mkdir(parents=True, exist_ok=True)
        failed_job_dir = FAILED_DIR / job_id
        job_dir.rename(failed_job_dir)

        return 1

    # -------------------------------------------------------------------------
    # Step 8: Mark complete and move to completed/
    # -------------------------------------------------------------------------

    append_event(audit_dir, "job_completed")
    update_job_timestamp(job_json_path, "completed_at")
    atomic_write(status_file, "completed\n")

    COMPLETED_DIR.mkdir(parents=True, exist_ok=True)
    completed_job_dir = COMPLETED_DIR / job_id
    job_dir.rename(completed_job_dir)

    print(f"Job completed: {completed_job_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
