# -*- coding: utf-8 -*-
"""
training.py

Completion logic for the Toast Training Hours service.

Three completion rule types (driven by training_requirements.completion_type):

  hours            – Server:  total hours >= required_hours
  hours_or_periods – Cashier: total hours >= required_hours
                               OR closed weeks with any hours >= required_periods
  weekly_pace      – Cook:    average weekly hours decides threshold;
                               >= pace_threshold_hrs/wk → need weeks_above_pace closed weeks
                               <  pace_threshold_hrs/wk → need weeks_below_pace closed weeks

A "closed week" is a Wed–Tue window whose Tuesday is before today.
Only closed weeks count toward period/week thresholds.

Nothing in this module writes SQL — all persistence goes through database.py.
"""

import logging
from datetime import datetime, timezone

from src import database as db

logger = logging.getLogger("training")


# ---------------------------------------------------------------------------
# Completion checks
# ---------------------------------------------------------------------------

def check_completion(
    engine,
    employee_guid: str,
    job_guid: str,
    requirement: dict,
) -> dict | None:
    """
    Evaluate whether an employee has completed a training job.

    Parameters
    ----------
    engine      : SQLAlchemy Engine
    employee_guid : str
    job_guid      : str
    requirement   : dict   One entry from get_training_requirements().

    Returns
    -------
    dict with completion details if threshold is met, None otherwise.
    {
        "employee_guid": str,
        "job_guid":      str,
        "job_name":      str,
        "hours":         float,
        "weeks":         int | None,
        "reason":        str,    # human-readable, used in Teams message
    }
    """
    completion_type = requirement["completion_type"]
    job_name        = requirement["job_name"]

    logger.debug("check_completion  emp=%s  job=%s  type=%s",
                 employee_guid, job_guid, completion_type)

    try:
        if completion_type == "hours":
            return _check_hours(engine, employee_guid, job_guid, requirement)

        elif completion_type == "hours_or_periods":
            return _check_hours_or_periods(engine, employee_guid, job_guid, requirement)

        elif completion_type == "weekly_pace":
            return _check_weekly_pace(engine, employee_guid, job_guid, requirement)

        else:
            logger.error("Unknown completion_type %r for job %s", completion_type, job_guid)
            return None

    except Exception as e:
        logger.error("Error checking completion emp=%s job=%s: %s",
                     employee_guid, job_guid, e, exc_info=True)
        return None


def _check_hours(engine, employee_guid, job_guid, req) -> dict | None:
    """Server: reach required_hours total."""
    required = req["required_hours"]
    total    = db.get_total_hours(engine, employee_guid, job_guid)

    logger.debug("_check_hours  emp=%s  total=%.2f  required=%.2f",
                 employee_guid, total, required)

    if total >= required:
        reason       = f"Reached {total:.1f} hrs (required {required:.0f} hrs)"
        completed_at = db.get_completion_shift_out(engine, employee_guid, job_guid, required)
        logger.info("COMPLETE [hours]  emp=%s  job=%s  %s  completed_at=%s",
                    employee_guid, job_guid, reason, completed_at)
        return {
            "employee_guid": employee_guid,
            "job_guid":      job_guid,
            "job_name":      req["job_name"],
            "hours":         total,
            "weeks":         None,
            "completed_at":  completed_at,
            "reason":        reason,
        }
    return None


def _check_hours_or_periods(engine, employee_guid, job_guid, req) -> dict | None:
    """Cashier: reach required_hours OR required_periods closed weeks."""
    required_hours   = req["required_hours"]
    required_periods = req["required_periods"]

    total      = db.get_total_hours(engine, employee_guid, job_guid)
    weeks_data = db.get_hours_by_week(engine, employee_guid, job_guid)
    closed_weeks_with_hours = [w for w in weeks_data if w["closed"] and w["hours"] > 0]
    closed_count = len(closed_weeks_with_hours)

    logger.debug(
        "_check_hours_or_periods  emp=%s  total=%.2f  required_hrs=%.2f  "
        "closed_weeks=%d  required_periods=%d",
        employee_guid, total, required_hours, closed_count, required_periods,
    )

    # Hours leg
    if total >= required_hours:
        reason       = f"Reached {total:.1f} hrs (required {required_hours:.0f} hrs)"
        completed_at = db.get_completion_shift_out(engine, employee_guid, job_guid, required_hours)
        logger.info("COMPLETE [hours_or_periods/hours]  emp=%s  job=%s  %s  completed_at=%s",
                    employee_guid, job_guid, reason, completed_at)
        return {
            "employee_guid": employee_guid,
            "job_guid":      job_guid,
            "job_name":      req["job_name"],
            "hours":         total,
            "weeks":         closed_count,
            "completed_at":  completed_at,
            "reason":        reason,
        }

    # Periods leg
    if closed_count >= required_periods:
        reason       = (
            f"Completed {closed_count} full training week(s) "
            f"(required {required_periods}) with {total:.1f} total hrs"
        )
        last_week    = closed_weeks_with_hours[-1]
        completed_at = db.get_last_shift_out_in_week(
            engine, employee_guid, job_guid, last_week["week_end"]
        )
        logger.info("COMPLETE [hours_or_periods/periods]  emp=%s  job=%s  %s  completed_at=%s",
                    employee_guid, job_guid, reason, completed_at)
        return {
            "employee_guid": employee_guid,
            "job_guid":      job_guid,
            "job_name":      req["job_name"],
            "hours":         total,
            "weeks":         closed_count,
            "completed_at":  completed_at,
            "reason":        reason,
        }

    return None


def _check_weekly_pace(engine, employee_guid, job_guid, req) -> dict | None:
    """
    Cook: average weekly hours over closed weeks decides the threshold.
    >= pace_threshold_hrs/wk → need weeks_above_pace closed weeks
    <  pace_threshold_hrs/wk → need weeks_below_pace closed weeks
    """
    pace_threshold  = req["pace_threshold_hrs"]
    weeks_above     = req["weeks_above_pace"]
    weeks_below     = req["weeks_below_pace"]

    weeks_data   = db.get_hours_by_week(engine, employee_guid, job_guid)
    closed_weeks = [w for w in weeks_data if w["closed"]]
    closed_count = len(closed_weeks)

    if closed_count == 0:
        logger.debug("_check_weekly_pace  emp=%s  no closed weeks yet", employee_guid)
        return None

    total_hours  = sum(w["hours"] for w in closed_weeks)
    avg_weekly   = total_hours / closed_count
    required_weeks = weeks_above if avg_weekly >= pace_threshold else weeks_below

    logger.debug(
        "_check_weekly_pace  emp=%s  closed_weeks=%d  avg=%.2f/wk  "
        "pace_threshold=%.2f  required_weeks=%d",
        employee_guid, closed_count, avg_weekly, pace_threshold, required_weeks,
    )

    if closed_count >= required_weeks:
        pace_label   = "full-time pace" if avg_weekly >= pace_threshold else "part-time pace"
        reason       = (
            f"Completed {closed_count} full training week(s) at {pace_label} "
            f"(avg {avg_weekly:.1f} hrs/wk, required {required_weeks} weeks)"
        )
        last_week    = closed_weeks[-1]
        completed_at = db.get_last_shift_out_in_week(
            engine, employee_guid, job_guid, last_week["week_end"]
        )
        logger.info("COMPLETE [weekly_pace]  emp=%s  job=%s  %s  completed_at=%s",
                    employee_guid, job_guid, reason, completed_at)
        return {
            "employee_guid": employee_guid,
            "job_guid":      job_guid,
            "job_name":      req["job_name"],
            "hours":         total_hours,
            "weeks":         closed_count,
            "completed_at":  completed_at,
            "reason":        reason,
        }

    return None


# ---------------------------------------------------------------------------
# Process a batch of time entries for one location
# ---------------------------------------------------------------------------

def process_entries(
    engine,
    entries: list[dict],
    location_guid: str,
    requirements: dict[str, dict],
    training_job_guid_set: set[str],
) -> list[dict]:
    """
    Filter, save, and check completion for a list of raw Toast time entries.

    Steps:
      1. Filter to training jobs only.
      2. Upsert each entry (skipping open ones).
      3. Collect unique (employee_guid, job_guid) pairs that were touched.
      4. For each pair, check if training is now complete.
      5. Return list of new completions (already-complete pairs are skipped).

    Parameters
    ----------
    engine               : Engine
    entries              : list   Raw Toast time-entry objects.
    location_guid        : str    Location these entries belong to.
    requirements         : dict   From database.get_training_requirements().
    training_job_guid_set: set    Fast filter — only these job GUIDs are saved.

    Returns
    -------
    list of completion dicts (may be empty).
    """
    # Step 1 — filter to training jobs
    training_entries = [
        e for e in entries
        if e.get("jobReference", {}).get("guid") in training_job_guid_set
    ]

    logger.info(
        "process_entries  location=%s  total=%d  training=%d",
        location_guid, len(entries), len(training_entries),
    )

    if not training_entries:
        return []

    # Step 2 — upsert
    saved, skipped = db.upsert_time_entries(engine, training_entries, location_guid)
    logger.info("Saved %d, skipped %d open entries  location=%s", saved, skipped, location_guid)

    # Step 3 — unique pairs touched this run
    touched = {
        (e["employeeReference"]["guid"], e["jobReference"]["guid"])
        for e in training_entries
        if e.get("outDate") is not None   # only closed entries were saved
    }
    logger.debug("%d unique employee+job pairs to check", len(touched))

    # Step 4 — check completion for each pair
    new_completions = []
    for employee_guid, job_guid in touched:
        req = requirements.get(job_guid)
        if not req:
            logger.warning("No requirement found for job_guid %s — skipping", job_guid)
            continue

        # Skip if already complete
        existing = db.get_completion(engine, employee_guid, job_guid)
        if existing:
            logger.debug("Already complete  emp=%s  job=%s", employee_guid, job_guid)
            continue

        result = check_completion(engine, employee_guid, job_guid, req)
        if result:
            db.save_completion(
                engine,
                employee_guid         = employee_guid,
                job_guid              = job_guid,
                hours_at_completion   = result["hours"],
                location_guid_trigger = location_guid,
                weeks_at_completion   = result.get("weeks"),
                completed_at          = result.get("completed_at"),
            )
            new_completions.append(result)

    if new_completions:
        logger.info("%d new completion(s) detected  location=%s",
                    len(new_completions), location_guid)

    return new_completions
