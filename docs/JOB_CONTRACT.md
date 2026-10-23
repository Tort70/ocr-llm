# OCR-LLM Job Contract v1.0

Filesystem-based job contract for OCR → LLM processing pipeline.

## Directory Layout

```
/srv/ocr-llm/
├── jobs/
│   └── job-YYYYMMDD-NNN/          # One directory per job
│       ├── job.json               # Job definition and metadata
│       ├── status.txt             # Single-word status (quick reads)
│       ├── input/                 # Original input files
│       ├── work/                  # Intermediate processing files
│       ├── output/                # Final results
│       └── audit/                 # Audit trail logs
├── schema/
│   └── job.schema.json            # Authoritative JSON schema
└── docs/
    └── JOB_CONTRACT.md            # This file
```

## File Ownership and Write Rules

| File/Directory | Written By | Mutability |
|----------------|-----------|------------|
| `job.json` | Submitter only | **IMMUTABLE** (timestamps excepted) |
| `status.txt` | Worker | Mutable - **AUTHORITATIVE execution state** |
| `input/` | Submitter | **IMMUTABLE after creation** |
| `work/` | Worker | Ephemeral (may be cleared) |
| `output/` | Worker | Written once at completion |
| `audit/` | Worker + System | **APPEND-ONLY** |

### Immutability Rules

**job.json is IMMUTABLE after creation, except:**
- `job.started_at` - set once when processing begins
- `job.completed_at` - set once when processing ends

**All other fields in job.json must NEVER be modified:**
- `job.id`, `job.created_at`
- `creator.*`, `input.*`, `task.*`, `constraints.*`
- `status` (informational snapshot only)

**Files in `input/` directory must NEVER be modified.**

### Status Authority

> **CRITICAL:** `status.txt` is the authoritative execution state.
> `job.json.status` is informational only and must NOT be used for control flow.

Workers and orchestrators must:
- Read `status.txt` to determine job state
- Never rely on `job.json.status` for decisions
- Update `status.txt` atomically when state changes

## Status Values

Contents of `status.txt` (single line, no trailing content):

| Status | Meaning |
|--------|---------|
| `pending` | Job submitted, awaiting processing |
| `processing` | Worker has claimed job, actively processing |
| `completed` | Processing finished successfully |
| `failed` | Processing failed (see `audit/error.txt`) |
| `cancelled` | Job cancelled before completion |

**Status transitions:**
```
pending → processing → completed
                    └→ failed
pending → cancelled
processing → cancelled
```

## Auditability

The contract supports audit requirements through:

1. **Immutable input record**: `input.checksum` allows verification that processed content matches original submission

2. **Creator attribution**: `creator.*` captures who submitted the job and their role at submission time

3. **Timestamp chain**: `created_at` → `started_at` → `completed_at` provides processing timeline

4. **Append-only audit log**: `audit/` directory stores:
   - `error.txt` - failure details (if job failed)
   - `events.jsonl` - timestamped processing events
   - `llm-request.json` - exact prompt sent to LLM
   - `llm-response.json` - raw LLM response

5. **Exam mode flag**: When `constraints.exam_mode: true`:
   - Network context disabled
   - Full prompt/response logging required
   - No result caching permitted

## Exam Integrity

For secure exam processing (`exam_mode: true`):

- Input files must pass checksum verification before processing
- LLM requests must not include external context
- All prompts and responses logged to audit directory
- PII redaction applied if `pii_redaction: true`
- Time limits enforced; job fails if exceeded
- Output written atomically (no partial results)

## Filesystem Conventions

**Job ID format:** `job-YYYYMMDD-NNN`
- `YYYYMMDD`: Date of creation
- `NNN`: Zero-padded sequence number (001-999)
- Example: `job-20260208-001`

**Quick status check (no JSON parsing):**
```bash
cat /srv/ocr-llm/jobs/job-20260208-001/status.txt
# Returns: pending
```

**Find all pending jobs:**
```bash
grep -l "^pending$" /srv/ocr-llm/jobs/*/status.txt
```

**Checksum verification:**
```bash
cd /srv/ocr-llm/jobs/job-20260208-001/input
sha256sum -c <<< "3a7bd3e2... midterm-essay-student-1042.pdf"
```

## Schema Versioning

- Current version: `1.0`
- `contract_version` field is required and must be checked by processors
- Future versions must maintain backward compatibility for v1.0 readers
- Unknown top-level keys must be ignored (forward compatibility)
- `_extensions` namespace reserved for experimental features

## Error Handling

When a job fails, error details are written to `audit/error.txt`:

```
# audit/error.txt format (one error per job)
code: OCR_TIMEOUT
stage: ocr
timestamp: 2026-02-08T14:35:42Z
message: OCR processing exceeded 120 second limit
```

**Errors are NEVER written to job.json.** The `job.json` file remains immutable after creation (timestamps excepted).

Error codes by stage:
- `validation`: `INVALID_CHECKSUM`, `UNSUPPORTED_FORMAT`, `FILE_TOO_LARGE`
- `ocr`: `OCR_FAILED`, `OCR_TIMEOUT`, `NO_TEXT_DETECTED`
- `llm`: `LLM_API_ERROR`, `LLM_TIMEOUT`, `CONTENT_FILTERED`
- `output`: `WRITE_FAILED`, `DISK_FULL`
