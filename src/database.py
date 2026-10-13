# -*- coding: utf-8 -*-
"""
database.py

All reads and writes to the payroll_training database.
Nothing outside this module touches SQL directly.

Tables owned here:
    training_time_entries
    training_completions
    poll_log

training_requirements is read-only at runtime (seeded by migration 002).
Locations are read from toast_order_data via get_locations().
"""

import logging
from datetime import datetime, timezone
from sqlalchemy import text

logger = logging.getLogger("database")


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------
# Toast returns timestamps like "2026-10-07T17:15:28.538+0000" — note +0000
# without a colon, which datetime.fromisoformat() rejects on Python < 3.11.
# We normalise to +00:00 before parsing.

def _parse_ts(value: str | None) -> datetime | None:
    """Parse a Toast ISO-8601 timestamp to a timezone-aware datetime."""
    if not value:
        return None
    try:
        # Normalise +0000 / -0500 → +00:00 / -05:00
        if len(value) > 5 and value[-5] in ("+", "-") and ":" not in value[-5:]:
            value = value[:-2] + ":" + value[-2:]
        return datetime.fromisoformat(value)
    except (ValueError, TypeError) as e:
        logger.error("Failed to parse timestamp %r: %s", value, e)
        raise


# ---------------------------------------------------------------------------
# Locations  (read from toast_order_data)
# ---------------------------------------------------------------------------

def get_locations(locations_engine, excluded: set[str] = None) -> list[dict]:
    """
    Return all active locations from toast_order_data.public.locations.

    Parameters
    ----------
    locations_engine : Engine   The toast_order_data engine.
    excluded         : set      store_guid strings to skip (from config).

    Returns
    -------
    list of dicts with keys: location_name, store_guid, timezone
    """
    excluded = excluded or set()
    try:
        with locations_engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT location_name,
                       store_guid::text  AS store_guid,
                       COALESCE(timezone, 'America/New_York') AS timezone
                FROM   public.locations
                ORDER  BY location_name
            """)).fetchall()

        locations = [
            {"location_name": r.location_name,
             "store_guid":    r.store_guid,
             "timezone":      r.timezone}
            for r in rows
            if r.store_guid not in excluded
        ]
        logger.debug("Loaded %d locations (%d excluded)", len(locations), len(rows) - len(locations))
        return locations
    except Exception as e:
        logger.error("Failed to load locations: %s", e, exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Training requirements  (read-only at runtime)
# ---------------------------------------------------------------------------

def get_training_requirements(engine) -> dict[str, dict]:
    """
    Load all active training requirements keyed by job_guid.

    Returns
    -------
    {
        "<job_guid>": {
            "job_name":           str,
            "completion_type":    str,   # hours | hours_or_periods | weekly_pace
            "required_hours":     float | None,
            "required_periods":   int   | None,
            "pace_threshold_hrs": float | None,
            "weeks_above_pace":   int   | None,
            "weeks_below_pace":   int   | None,
        },
        ...
    }
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT job_guid::text,
                       job_name,
                       completion_type,
                       required_hours,
                       required_periods,
                       pace_threshold_hrs,
                       weeks_above_pace,
                       weeks_below_pace
                FROM   training_requirements
                WHERE  active = TRUE
            """)).fetchall()

        requirements = {
            r.job_guid: {
                "job_name":           r.job_name,
                "completion_type":    r.completion_type,
                "required_hours":     float(r.required_hours)     if r.required_hours     is not None else None,
                "required_periods":   int(r.required_periods)     if r.required_periods   is not None else None,
                "pace_threshold_hrs": float(r.pace_threshold_hrs) if r.pace_threshold_hrs is not None else None,
                "weeks_above_pace":   int(r.weeks_above_pace)     if r.weeks_above_pace   is not None else None,
                "weeks_below_pace":   int(r.weeks_below_pace)     if r.weeks_below_pace   is not None else None,
            }
            for r in rows
        }
        logger.debug("Loaded %d training requirements", len(requirements))
        return requirements
    except Exception as e:
        logger.error("Failed to load training requirements: %s", e, exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Time entries
# ---------------------------------------------------------------------------

def upsert_time_entry(engine, entry: dict, location_guid: str) -> bool:
    """
    UPSERT a single Toast time entry into training_time_entries.

    Skips entries with a null outDate (still clocked in).
    Caller is responsible for pre-filtering to training jobs only.

    Returns True if saved, False if skipped (open entry).
    """
    toast_guid = entry.get("guid", "<unknown>")

    out_date = _parse_ts(entry.get("outDate"))
    if out_date is None:
        logger.debug("Skipping open entry %s — no outDate (still clocked in)", toast_guid)
        return False

    try:
        employee_guid = entry["employeeReference"]["guid"]
        job_guid      = entry["jobReference"]["guid"]
        in_date       = _parse_ts(entry["inDate"])
        business_date = datetime.strptime(str(entry["businessDate"]), "%Y%m%d").date()
        regular       = float(entry.get("regularHours")  or 0)
        overtime      = float(entry.get("overtimeHours") or 0)
        hours         = round(regular + overtime, 4)
        modified_date = _parse_ts(entry["modifiedDate"])
        deleted       = bool(entry.get("deleted", False))
    except (KeyError, ValueError, TypeError) as e:
        logger.error("Failed to parse time entry %s: %s | entry=%r", toast_guid, e, entry)
        raise

    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO training_time_entries (
                    toast_guid, location_guid, employee_guid, job_guid,
                    business_date, in_date, out_date, hours,
                    modified_date, deleted, upserted_at
                ) VALUES (
                    :toast_guid, :location_guid, :employee_guid, :job_guid,
                    :business_date, :in_date, :out_date, :hours,
                    :modified_date, :deleted, NOW()
                )
                ON CONFLICT (toast_guid) DO UPDATE SET
                    hours         = EXCLUDED.hours,
                    modified_date = EXCLUDED.modified_date,
                    deleted       = EXCLUDED.deleted,
                    upserted_at   = NOW()
                WHERE training_time_entries.modified_date < EXCLUDED.modified_date
            """), {
                "toast_guid":    toast_guid,
                "location_guid": location_guid,
                "employee_guid": employee_guid,
                "job_guid":      job_guid,
                "business_date": business_date,
                "in_date":       in_date,
                "out_date":      out_date,
                "hours":         hours,
                "modified_date": modified_date,
                "deleted":       deleted,
            })
        logger.debug("Upserted entry %s  emp=%s  hours=%.2f  deleted=%s",
                     toast_guid, employee_guid, hours, deleted)
        return True
    except Exception as e:
        logger.error("DB error upserting entry %s: %s", toast_guid, e, exc_info=True)
        raise


def upsert_time_entries(engine, entries: list[dict], location_guid: str) -> tuple[int, int]:
    """
    Bulk UPSERT a list of time entries.

    Returns (saved, skipped).
        saved   = rows inserted or updated
        skipped = open entries (null outDate) ignored
    """
    saved = skipped = 0
    for entry in entries:
        try:
            result = upsert_time_entry(engine, entry, location_guid)
            if result:
                saved += 1
            else:
                skipped += 1
        except Exception as e:
            # Log and continue — one bad entry shouldn't abort the whole batch
            logger.error("Skipping entry %s due to error: %s",
                         entry.get("guid", "<unknown>"), e)
    logger.info("Upserted %d entries, skipped %d open entries  location=%s",
                saved, skipped, location_guid)
    return saved, skipped


# ---------------------------------------------------------------------------
# Hour aggregation  (used by training.py for completion checks)
# ---------------------------------------------------------------------------

def get_total_hours(engine, employee_guid: str, job_guid: str) -> float:
    """Sum of all non-deleted training hours for an employee+job (global)."""
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT COALESCE(SUM(hours), 0) AS total
                FROM   training_time_entries
                WHERE  employee_guid = :emp
                  AND  job_guid      = :job
                  AND  deleted       = FALSE
            """), {"emp": employee_guid, "job": job_guid}).fetchone()
        total = float(row.total)
        logger.debug("get_total_hours  emp=%s  job=%s  total=%.2f", employee_guid, job_guid, total)
        return total
    except Exception as e:
        logger.error("Failed to get total hours emp=%s job=%s: %s",
                     employee_guid, job_guid, e, exc_info=True)
        raise


def get_completion_shift_out(
    engine,
    employee_guid: str,
    job_guid: str,
    required_hours: float,
) -> datetime | None:
    """
    Return the out_date of the shift that first pushed cumulative hours
    over required_hours, ordered by business_date then out_date.

    Used to set completed_at to the real moment the employee finished.
    Returns None if no such shift is found (shouldn't happen if called
    after a threshold check passes).
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                WITH cumulative AS (
                    SELECT out_date,
                           SUM(hours) OVER (
                               ORDER BY business_date, out_date
                               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                           ) AS running_total
                    FROM training_time_entries
                    WHERE employee_guid = :emp
                      AND job_guid      = :job
                      AND deleted       = FALSE
                      AND out_date      IS NOT NULL
                )
                SELECT out_date
                FROM   cumulative
                WHERE  running_total >= :required
                ORDER  BY out_date
                LIMIT  1
            """), {"emp": employee_guid, "job": job_guid, "required": required_hours}).fetchone()
        result = row.out_date if row else None
        logger.debug("get_completion_shift_out  emp=%s  job=%s  out_date=%s",
                     employee_guid, job_guid, result)
        return result
    except Exception as e:
        logger.error("Failed to get completion shift out emp=%s job=%s: %s",
                     employee_guid, job_guid, e, exc_info=True)
        raise


def get_last_shift_out_in_week(
    engine,
    employee_guid: str,
    job_guid: str,
    week_end: object,
) -> datetime | None:
    """
    Return the out_date of the latest shift within the training week
    that ends on week_end (a date object, inclusive).

    Used to set completed_at for period/pace completions.
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT MAX(out_date) AS last_out
                FROM   training_time_entries
                WHERE  employee_guid = :emp
                  AND  job_guid      = :job
                  AND  deleted       = FALSE
                  AND  business_date <= :week_end
                  AND  business_date >= :week_start
                  AND  out_date IS NOT NULL
            """), {
                "emp":        employee_guid,
                "job":        job_guid,
                "week_end":   week_end,
                "week_start": week_end - __import__("datetime").timedelta(days=6),
            }).fetchone()
        result = row.last_out if row else None
        logger.debug("get_last_shift_out_in_week  emp=%s  job=%s  week_end=%s  out=%s",
                     employee_guid, job_guid, week_end, result)
        return result
    except Exception as e:
        logger.error("Failed to get last shift out in week emp=%s job=%s: %s",
                     employee_guid, job_guid, e, exc_info=True)
        raise


def get_hours_by_week(engine, employee_guid: str, job_guid: str) -> list[dict]:
    """
    Return hours grouped by Wed–Tue training week for an employee+job.

    Each row: { week_start: date, week_end: date, hours: float, closed: bool }

    A week is "closed" when its Tuesday (week_end) is before today.
    Only closed weeks are used for Cashier/Cook completion checks.
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                WITH weeks AS (
                    SELECT
                        business_date,
                        hours,
                        -- Roll back to the Wednesday that starts this week
                        business_date - CAST(
                            MOD(EXTRACT(DOW FROM business_date)::int - 3 + 7, 7) AS INT
                        ) AS week_start
                    FROM training_time_entries
                    WHERE employee_guid = :emp
                      AND job_guid      = :job
                      AND deleted       = FALSE
                )
                SELECT
                    week_start,
                    week_start + INTERVAL '6 days' AS week_end,
                    SUM(hours) AS hours
                FROM weeks
                GROUP BY week_start
                ORDER BY week_start
            """), {"emp": employee_guid, "job": job_guid}).fetchall()

        today = datetime.now(timezone.utc).date()
        result = [
            {
                "week_start": r.week_start,
                "week_end":   r.week_end.date() if hasattr(r.week_end, "date") else r.week_end,
                "hours":      float(r.hours),
                "closed":     (r.week_end.date() if hasattr(r.week_end, "date") else r.week_end) < today,
            }
            for r in rows
        ]
        logger.debug("get_hours_by_week  emp=%s  job=%s  weeks=%d", employee_guid, job_guid, len(result))
        return result
    except Exception as e:
        logger.error("Failed to get hours by week emp=%s job=%s: %s",
                     employee_guid, job_guid, e, exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Completions
# ---------------------------------------------------------------------------

def get_completion(engine, employee_guid: str, job_guid: str) -> dict | None:
    """Return the completion row for an employee+job, or None if not yet complete."""
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT employee_guid::text, job_guid::text,
                       completed_at, hours_at_completion,
                       weeks_at_completion, notified_at
                FROM   training_completions
                WHERE  employee_guid = :emp
                  AND  job_guid      = :job
            """), {"emp": employee_guid, "job": job_guid}).fetchone()
        return dict(row._mapping) if row else None
    except Exception as e:
        logger.error("Failed to get completion emp=%s job=%s: %s",
                     employee_guid, job_guid, e, exc_info=True)
        raise


def save_completion(
    engine,
    employee_guid: str,
    job_guid: str,
    hours_at_completion: float,
    location_guid_trigger: str,
    weeks_at_completion: int | None = None,
    completion_status: str = "completed",
    completed_at: datetime | None = None,
) -> None:
    """
    Insert a training completion row.
    No-op if one already exists (PK constraint prevents duplicates).

    Parameters
    ----------
    completion_status : str
        'completed'  — employee met the training threshold (default)
        'terminated' — employee was deleted in Toast before finishing
    completed_at : datetime | None
        The out_date of the shift that triggered completion.
        Falls back to NOW() if not provided (e.g. terminated employees).
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO training_completions (
                    employee_guid, job_guid, completed_at,
                    hours_at_completion, weeks_at_completion,
                    location_guid_trigger, notified_at,
                    completion_status
                ) VALUES (
                    :emp, :job, COALESCE(:completed_at, NOW()),
                    :hours, :weeks,
                    :loc, NULL,
                    :status
                )
                ON CONFLICT (employee_guid, job_guid) DO NOTHING
            """), {
                "emp":          employee_guid,
                "job":          job_guid,
                "completed_at": completed_at,
                "hours":        hours_at_completion,
                "weeks":        weeks_at_completion,
                "loc":          location_guid_trigger,
                "status":       completion_status,
            })
        logger.info("Saved completion  emp=%s  job=%s  hours=%.2f  weeks=%s  status=%s  completed_at=%s",
                    employee_guid, job_guid, hours_at_completion,
                    weeks_at_completion, completion_status, completed_at)
    except Exception as e:
        logger.error("Failed to save completion emp=%s job=%s: %s",
                     employee_guid, job_guid, e, exc_info=True)
        raise


def get_pending_notifications(engine) -> list[dict]:
    """
    Return completion rows where the Teams notification has not been sent yet.
    Retried on every run until notified_at is stamped.
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT employee_guid::text, job_guid::text,
                       completed_at, hours_at_completion,
                       weeks_at_completion,
                       location_guid_trigger::text,
                       completion_status
                FROM   training_completions
                WHERE  notified_at IS NULL
                  AND  completion_status = 'completed'
                ORDER  BY completed_at
            """)).fetchall()
        pending = [dict(r._mapping) for r in rows]
        if pending:
            logger.info("Found %d pending notification(s)", len(pending))
        return pending
    except Exception as e:
        logger.error("Failed to get pending notifications: %s", e, exc_info=True)
        raise


def mark_notified(engine, employee_guid: str, job_guid: str) -> None:
    """Stamp notified_at after the Teams message is confirmed sent."""
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE training_completions
                   SET notified_at = NOW()
                 WHERE employee_guid = :emp
                   AND job_guid      = :job
            """), {"emp": employee_guid, "job": job_guid})
        logger.info("Marked notified  emp=%s  job=%s", employee_guid, job_guid)
    except Exception as e:
        logger.error("Failed to mark notified emp=%s job=%s: %s",
                     employee_guid, job_guid, e, exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Poll log
# ---------------------------------------------------------------------------

def get_last_modified_check(engine, location_guid: str) -> datetime | None:
    """
    Return the timestamp of the last successful modified-range poll for a
    location, or None if it has never run.

    main.py uses this as modifiedStartDate for the next run.
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(text("""
                SELECT MAX(run_at) AS last_run
                FROM   poll_log
                WHERE  location_guid = :loc
                  AND  poll_type     = 'modified_range'
                  AND  success       = TRUE
            """), {"loc": location_guid}).fetchone()
        last_run = row.last_run if row and row.last_run else None
        logger.debug("get_last_modified_check  location=%s  last_run=%s", location_guid, last_run)
        return last_run
    except Exception as e:
        logger.error("Failed to get last modified check for location %s: %s",
                     location_guid, e, exc_info=True)
        raise


def log_poll(
    engine,
    location_guid: str,
    poll_type: str,
    window_start: str,
    window_end: str | None,
    entries_found: int,
    training_saved: int,
    success: bool = True,
    error: str | None = None,
) -> None:
    """Write one row to poll_log."""
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO poll_log (
                    run_at, location_guid, poll_type,
                    window_start, window_end,
                    entries_found, training_saved,
                    success, error
                ) VALUES (
                    NOW(), :loc, :poll_type,
                    :window_start, :window_end,
                    :entries_found, :training_saved,
                    :success, :error
                )
            """), {
                "loc":            location_guid,
                "poll_type":      poll_type,
                "window_start":   window_start,
                "window_end":     window_end,
                "entries_found":  entries_found,
                "training_saved": training_saved,
                "success":        success,
                "error":          error,
            })
        logger.debug("Logged poll  location=%s  type=%s  found=%d  saved=%d  success=%s",
                     location_guid, poll_type, entries_found, training_saved, success)
    except Exception as e:
        # Don't let a logging failure crash the run
        logger.error("Failed to write poll_log for location %s: %s", location_guid, e, exc_info=True)


def prune_poll_log(engine, retention_days: int) -> int:
    """Delete poll_log rows older than retention_days. Returns rows deleted."""
    try:
        with engine.begin() as conn:
            result = conn.execute(text("""
                DELETE FROM poll_log
                WHERE run_at < NOW() - (:days || ' days')::INTERVAL
            """), {"days": retention_days})
        deleted = result.rowcount
        logger.info("Pruned %d poll_log rows older than %d days", deleted, retention_days)
        return deleted
    except Exception as e:
        logger.error("Failed to prune poll_log: %s", e, exc_info=True)
        raise
