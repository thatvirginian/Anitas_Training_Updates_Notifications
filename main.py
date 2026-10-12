# -*- coding: utf-8 -*-
"""
main.py

Entry point for the Toast Training Hours service.

Run order:
  1. Load locations and training requirements from DB
  2. Pull — fetch time entries from all locations (business-date + modified-range)
  3. Filter — keep only training-job entries
  4. Save  — upsert all filtered entries to DB
  5. Calculate — run completion checks across all touched employee+job pairs
  6. Notify — send Teams messages for any pending completions
  7. Prune poll_log
  8. Exit

Pulling all locations before saving ensures that an employee who worked at
two locations on the same day has both shifts in the DB before completion
checks run.
"""

import logging
import sys
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict

from src.database_setup import get_engine, get_locations_engine
from src.toast_api import ToastAPI
from src.labor_api import LaborAPI
from src import database as db
from src import training
from src import notifications
from src.config import (
    TRAINING_JOB_GUID_SET,
    BUSINESS_DATE_LOOKBACK_DAYS,
    POLL_LOG_RETENTION_DAYS,
    EXCLUDED_LOCATION_GUIDS,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("main")

# Default lookback for first modified-range run per location
FIRST_RUN_LOOKBACK_DAYS = 30


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logger.info("═" * 60)
    logger.info("Toast Training Hours — starting run")
    logger.info("═" * 60)

    now = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # 1. Connect
    # ------------------------------------------------------------------
    try:
        engine           = get_engine()
        locations_engine = get_locations_engine()
        api              = ToastAPI(engine=engine)
        labor            = LaborAPI(api)
        logger.info("Database connections established")
    except Exception as e:
        logger.critical("Failed to initialise connections: %s", e, exc_info=True)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 2. Load reference data
    # ------------------------------------------------------------------
    try:
        locations    = db.get_locations(locations_engine, excluded=EXCLUDED_LOCATION_GUIDS)
        requirements = db.get_training_requirements(engine)
        logger.info("Loaded %d locations, %d training requirements",
                    len(locations), len(requirements))
    except Exception as e:
        logger.critical("Failed to load reference data: %s", e, exc_info=True)
        sys.exit(1)

    if not locations:
        logger.warning("No locations found — nothing to do.")
        sys.exit(0)

    if not requirements:
        logger.warning("No active training requirements — nothing to do.")
        sys.exit(0)

    locations_by_guid = {loc["store_guid"]: loc["location_name"] for loc in locations}

    # ------------------------------------------------------------------
    # 3. PULL — fetch all entries from all locations
    # ------------------------------------------------------------------
    logger.info("─" * 60)
    logger.info("PULL — fetching entries from %d locations", len(locations))

    # Collect all training entries across all locations before saving anything.
    # Structure: list of (entry_dict, location_guid)
    all_training_entries: list[tuple[dict, str]] = []

    # Track poll metadata so we can log after saves complete
    poll_records: list[dict] = []

    for loc in locations:
        location_name = loc["location_name"]
        location_guid = loc["store_guid"]

        logger.info("  %s", location_name)

        # ── Business-date pull ─────────────────────────────────────────
        for days_back in range(1, BUSINESS_DATE_LOOKBACK_DAYS + 1):
            business_date = date.today() - timedelta(days=days_back)
            bd_entries    = []
            bd_error      = None

            try:
                bd_entries = labor.get_time_entries_by_business_date(
                    location_guid, business_date
                )
                bd_entries = [e for e in bd_entries if e is not None]
                training_entries = [
                    e for e in bd_entries
                    if (e.get("jobReference") or {}).get("guid") in TRAINING_JOB_GUID_SET
                ]
                for e in training_entries:
                    all_training_entries.append((e, location_guid))

                logger.info("    business_date=%s  total=%d  training=%d",
                            business_date, len(bd_entries), len(training_entries))

            except Exception as e:
                bd_error = str(e)
                logger.error("    business_date pull failed  location=%s  date=%s: %s",
                             location_name, business_date, e, exc_info=True)

            poll_records.append({
                "location_guid":  location_guid,
                "poll_type":      "business_date",
                "window_start":   business_date.strftime("%Y%m%d"),
                "window_end":     None,
                "entries_found":  len(bd_entries),
                "training_found": len([e for e in bd_entries
                                       if e is not None
                                       and (e.get("jobReference") or {}).get("guid")
                                       in TRAINING_JOB_GUID_SET]),
                "success":        bd_error is None,
                "error":          bd_error,
            })

        # ── Modified-range pull ────────────────────────────────────────
        last_checked = db.get_last_modified_check(engine, location_guid)

        if last_checked is None:
            mod_start = now - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)
            logger.info("    modified_range: first run — back %d days", FIRST_RUN_LOOKBACK_DAYS)
        else:
            mod_start = last_checked
            logger.info("    modified_range: since %s", mod_start.isoformat())

        mod_entries = []
        mod_error   = None

        try:
            mod_entries = labor.get_time_entries_by_modified_range(
                location_guid, mod_start, now
            )
            mod_entries = [e for e in mod_entries if e is not None]
            training_mod = [
                e for e in mod_entries
                if (e.get("jobReference") or {}).get("guid") in TRAINING_JOB_GUID_SET
            ]
            for e in training_mod:
                all_training_entries.append((e, location_guid))

            logger.info("    modified_range  total=%d  training=%d",
                        len(mod_entries), len(training_mod))

        except Exception as e:
            mod_error = str(e)
            logger.error("    modified_range pull failed  location=%s: %s",
                         location_name, e, exc_info=True)

        poll_records.append({
            "location_guid":  location_guid,
            "poll_type":      "modified_range",
            "window_start":   mod_start.isoformat(),
            "window_end":     now.isoformat(),
            "entries_found":  len(mod_entries),
            "training_found": len([e for e in mod_entries
                                   if e is not None
                                   and (e.get("jobReference") or {}).get("guid")
                                   in TRAINING_JOB_GUID_SET]),
            "success":        mod_error is None,
            "error":          mod_error,
        })

    logger.info("Pull complete — %d total training entries across all locations",
                len(all_training_entries))

    # ------------------------------------------------------------------
    # 4. SAVE — upsert all training entries to DB
    # ------------------------------------------------------------------
    logger.info("─" * 60)
    logger.info("SAVE — upserting %d training entries", len(all_training_entries))

    saved = skipped = 0
    for entry, location_guid in all_training_entries:
        try:
            result = db.upsert_time_entry(engine, entry, location_guid)
            if result:
                saved += 1
            else:
                skipped += 1
        except Exception as e:
            logger.error("Failed to upsert entry %s: %s",
                         entry.get("guid", "<unknown>"), e)

    logger.info("Save complete — %d saved, %d skipped (open entries)", saved, skipped)

    # Write poll log now that saves are done
    for rec in poll_records:
        db.log_poll(
            engine,
            location_guid  = rec["location_guid"],
            poll_type      = rec["poll_type"],
            window_start   = rec["window_start"],
            window_end     = rec["window_end"],
            entries_found  = rec["entries_found"],
            training_saved = rec["training_found"],
            success        = rec["success"],
            error          = rec["error"],
        )

    # ------------------------------------------------------------------
    # 5. CALCULATE — check completion for all touched employee+job pairs
    # ------------------------------------------------------------------
    logger.info("─" * 60)
    logger.info("CALCULATE — checking completions")

    # Collect unique (employee_guid, job_guid) pairs from closed entries only
    touched: set[tuple[str, str]] = {
        ((e.get("employeeReference") or {}).get("guid"),
         (e.get("jobReference") or {}).get("guid"))
        for e, _ in all_training_entries
        if e.get("outDate") is not None
        and (e.get("employeeReference") or {}).get("guid")
        and (e.get("jobReference") or {}).get("guid")
    }
    logger.info("Checking %d unique employee+job pair(s)", len(touched))

    # ── Batch-check deleted status for all touched employees ──────────────
    # Group unique employee GUIDs by a representative location for the API call
    emp_to_loc: dict[str, str] = {}
    for e, loc in all_training_entries:
        emp_guid = (e.get("employeeReference") or {}).get("guid")
        if emp_guid and emp_guid not in emp_to_loc:
            emp_to_loc[emp_guid] = loc

    # Invert to location → [employee_guids] for batching
    loc_to_emps: dict[str, list[str]] = defaultdict(list)
    for emp_guid, loc_guid in emp_to_loc.items():
        loc_to_emps[loc_guid].append(emp_guid)

    deleted_guids: set[str] = set()
    for loc_guid, emp_guids in loc_to_emps.items():
        try:
            employees = labor.get_employees_by_ids(loc_guid, emp_guids)
            for emp in employees:
                if emp and emp.get("deleted"):
                    deleted_guids.add(str(emp.get("guid", "")))
        except Exception as e:
            logger.error("Failed to check deleted status  location=%s: %s",
                         loc_guid, e, exc_info=True)

    if deleted_guids:
        logger.info("Found %d deleted employee(s) in touched set", len(deleted_guids))

    # ── Per-pair completion check ─────────────────────────────────────────
    new_completions = 0
    for employee_guid, job_guid in touched:
        req = requirements.get(job_guid)
        if not req:
            logger.warning("No requirement for job_guid %s — skipping", job_guid)
            continue

        # Skip if already complete (any status)
        if db.get_completion(engine, employee_guid, job_guid):
            logger.debug("Already complete  emp=%s  job=%s", employee_guid, job_guid)
            continue

        # Find the location_guid for this employee from today's entries
        location_guid_trigger = next(
            (loc for e, loc in all_training_entries
             if (e.get("employeeReference") or {}).get("guid") == employee_guid
             and (e.get("jobReference") or {}).get("guid") == job_guid),
            None,
        )

        # Terminated employees: close out without notifying
        if employee_guid in deleted_guids:
            total_hours = db.get_total_hours(engine, employee_guid, job_guid)
            db.save_completion(
                engine,
                employee_guid         = employee_guid,
                job_guid              = job_guid,
                hours_at_completion   = total_hours,
                location_guid_trigger = location_guid_trigger,
                completion_status     = "terminated",
            )
            logger.info("Closed out terminated employee  emp=%s  job=%s  hours=%.2f",
                        employee_guid, job_guid, total_hours)
            new_completions += 1
            continue

        result = training.check_completion(engine, employee_guid, job_guid, req)
        if result:
            db.save_completion(
                engine,
                employee_guid         = employee_guid,
                job_guid              = job_guid,
                hours_at_completion   = result["hours"],
                location_guid_trigger = location_guid_trigger,
                weeks_at_completion   = result.get("weeks"),
                completion_status     = "completed",
            )
            new_completions += 1

    logger.info("Calculate complete — %d new completion(s)", new_completions)

    # ------------------------------------------------------------------
    # 6. NOTIFY — send Teams messages for all pending completions
    # ------------------------------------------------------------------
    logger.info("─" * 60)
    logger.info("NOTIFY — sending Teams notifications")
    try:
        sent, failed = notifications.send_pending_notifications(
            engine, labor, locations_by_guid
        )
        logger.info("Notifications: %d sent, %d failed", sent, failed)
    except Exception as e:
        logger.error("Notification step failed: %s", e, exc_info=True)

    # ------------------------------------------------------------------
    # 7. PRUNE poll log
    # ------------------------------------------------------------------
    try:
        pruned = db.prune_poll_log(engine, POLL_LOG_RETENTION_DAYS)
        if pruned:
            logger.info("Pruned %d old poll_log rows", pruned)
    except Exception as e:
        logger.error("Failed to prune poll_log: %s", e, exc_info=True)

    # ------------------------------------------------------------------
    # Done
    # ------------------------------------------------------------------
    logger.info("═" * 60)
    logger.info("Run complete — %d new completion(s) this run", new_completions)
    logger.info("═" * 60)


if __name__ == "__main__":
    main()
