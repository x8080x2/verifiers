"""Neon (PostgreSQL) helpers — single source of truth for jobs + files."""

import os
import secrets
import time
import logging
import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)


def get_conn():
    """Get a new database connection. Caller must close it."""
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(url, cursor_factory=RealDictCursor)


def init_schema():
    """Run schema.sql to add missing columns."""
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    if not os.path.exists(schema_path):
        logger.warning("schema.sql not found at %s", schema_path)
        return
    with open(schema_path, "r") as f:
        sql = f.read()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    logger.info("Database schema initialized")


# ── File helpers ──

def store_file(filename: str, content_bytes: bytes, user_id: int = 1) -> str:
    """Save uploaded file content to uploaded_files table. Returns token."""
    token = secrets.token_urlsafe(24)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO uploaded_files (token, user_id, filename, content) VALUES (%s, %s, %s, %s) RETURNING token",
                (token, user_id, filename, content_bytes.decode("utf-8", errors="ignore")),
            )
        conn.commit()
    logger.info("Stored file token=%s filename=%s size=%d", token, filename, len(content_bytes))
    return token


def get_file(token: str) -> dict | None:
    """Retrieve file metadata + content by token."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM uploaded_files WHERE token = %s", (token,))
            row = cur.fetchone()
            return dict(row) if row else None


def delete_file(token: str):
    """Remove a file row by token."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM uploaded_files WHERE token = %s", (token,))


# ── Job helpers ──

def create_job(list_id: str, submitted_total: int, filtered_total: int,
               invalid_total: int, token: str, user_id: int = 1) -> dict:
    """Insert a new job row. Returns the created job dict."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO validation_jobs
                   (list_id, user_id, submitted_total, filtered_total, invalid_total,
                    token, status, valid_count, filtered_count, invalid_count, processed)
                   VALUES (%s, %s, %s, %s, %s, %s, 'processing', 0, 0, 0, 0)
                   RETURNING *""",
                (list_id, user_id, submitted_total, filtered_total, invalid_total, token),
            )
            row = cur.fetchone()
        conn.commit()
    logger.info("Created job list_id=%s total=%d", list_id, submitted_total)
    return dict(row)


def get_job(list_id: str) -> dict | None:
    """Get job by DeBounce list_id."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM validation_jobs WHERE list_id = %s", (list_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def update_job_progress(list_id: str, processed: int,
                        valid: int = None, invalid: int = None,
                        filtered: int = None):
    """Update running counts during processing."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            parts = ["processed = %s", "updated_at = now()"]
            params = [processed]
            if valid is not None:
                parts.append("valid_count = %s")
                params.append(valid)
            if invalid is not None:
                parts.append("invalid_count = %s")
                params.append(invalid)
            if filtered is not None:
                parts.append("filtered_count = %s")
                params.append(filtered)
            params.append(list_id)
            cur.execute(
                f"UPDATE validation_jobs SET {', '.join(parts)} WHERE list_id = %s",
                params,
            )
        conn.commit()


def complete_job(list_id: str, download_link: str = ""):
    """Mark job as completed."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE validation_jobs
                   SET status = 'completed', download_link = %s, updated_at = now()
                   WHERE list_id = %s""",
                (download_link, list_id),
            )
        conn.commit()


def fail_job(list_id: str):
    """Mark job as failed."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE validation_jobs SET status = 'error', updated_at = now() WHERE list_id = %s",
                (list_id,),
            )


def cleanup_old_jobs(max_age_s: int = 86400):
    """Delete jobs and orphaned files older than max_age_s."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """DELETE FROM validation_jobs
                   WHERE created_at < now() - make_interval(secs => %s)""",
                (max_age_s,),
            )
            deleted = cur.rowcount
        conn.commit()
    if deleted:
        logger.info("Cleaned up %d old jobs", deleted)


def count_active_jobs_for_user(user_id: int) -> int:
    """Count jobs for a user that are still processing."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM validation_jobs WHERE user_id = %s AND status = 'processing'",
                (user_id,),
            )
            row = cur.fetchone()
            return row["cnt"] if row else 0


def count_all_active_jobs() -> int:
    """Count all jobs currently processing (across all users)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM validation_jobs WHERE status IN ('processing', 'queued')")
            row = cur.fetchone()
            return row["cnt"] if row else 0


def get_oldest_queued_job() -> dict | None:
    """Get the oldest queued job to process next."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM validation_jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
            )
            row = cur.fetchone()
            return dict(row) if row else None


def queue_job(list_id: str, submitted_total: int, filtered_total: int,
              invalid_total: int, token: str, user_id: int = 1) -> dict:
    """Insert a job as 'queued' (will be processed when DeBounce is free)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO validation_jobs
                   (list_id, user_id, submitted_total, filtered_total, invalid_total,
                    token, status, valid_count, filtered_count, invalid_count, processed)
                   VALUES (%s, %s, %s, %s, %s, %s, 'queued', 0, 0, 0, 0)
                   RETURNING *""",
                (list_id, user_id, submitted_total, filtered_total, invalid_total, token),
            )
            row = cur.fetchone()
        conn.commit()
    logger.info("Queued job list_id=%s total=%d", list_id, submitted_total)
    return dict(row)


# ── Balance / Top-up helpers ──

def get_balance(user_id: int) -> float:
    """Get user's current credit balance."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT credits FROM balances WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return float(row["credits"]) if row else 0.0


def add_credits(user_id: int, amount: float):
    """Add credits to a user's balance."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO balances (user_id, credits, updated_at)
                   VALUES (%s, %s, now())
                   ON CONFLICT (user_id)
                   DO UPDATE SET credits = balances.credits + %s, updated_at = now()""",
                (user_id, amount, amount),
            )
        conn.commit()


def create_payment_request(user_id: int, uid_str: str, amount_usd: float, tx_hash: str = "") -> dict:
    """Create a pending payment request for admin approval."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO payment_requests (user_id, uid_str, amount_usd, tx_hash, status)
                   VALUES (%s, %s, %s, %s, 'pending') RETURNING *""",
                (user_id, uid_str, amount_usd, tx_hash),
            )
            row = cur.fetchone()
        conn.commit()
    return dict(row)


def get_payment_request(req_id: int) -> dict | None:
    """Get a payment request by ID."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM payment_requests WHERE id = %s", (req_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def approve_payment(req_id: int, admin_note: str = ""):
    """Approve a payment request and add credits."""
    req = get_payment_request(req_id)
    if not req or req["status"] != "pending":
        return False
    add_credits(req["user_id"], float(req["amount_usd"]))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE payment_requests SET status = 'approved', admin_note = %s, updated_at = now() WHERE id = %s",
                (admin_note, req_id),
            )
        conn.commit()
    logger.info("Approved payment req_id=%d user_id=%d amount=%s", req_id, req["user_id"], req["amount_usd"])
    return True


def deny_payment(req_id: int, admin_note: str = ""):
    """Deny a payment request without adding credits."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE payment_requests SET status = 'denied', admin_note = %s, updated_at = now() WHERE id = %s",
                (admin_note, req_id),
            )
        conn.commit()
    logger.info("Denied payment req_id=%d", req_id)
