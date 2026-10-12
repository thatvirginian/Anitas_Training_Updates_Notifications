# -*- coding: utf-8 -*-
"""
notifications.py

Sends Teams notifications when an employee completes training.

Uses the Microsoft Teams Incoming Webhook — a single POST with an
Adaptive Card payload. The webhook URL is read from config.

Retry logic: if a send fails, notified_at is NOT stamped in the DB.
main.py will retry on the next run via get_pending_notifications().
"""

import logging
import requests

from src.config import TEAMS_WEBHOOK_URL
from src import database as db
from src.labor_api import LaborAPI

logger = logging.getLogger("notifications")


# ---------------------------------------------------------------------------
# Card builder
# ---------------------------------------------------------------------------

def _build_card(
    employee_name: str,
    employee_id: str | None,
    job_name: str,
    location_name: str,
    hours: float,
    weeks: int | None,
    reason: str,
    completed_at: str,
) -> dict:
    """
    Build a Teams Adaptive Card payload for a training completion.

    Returns the full webhook POST body.
    """
    name_line = f"{employee_name}  ({employee_id})" if employee_id else employee_name
    facts = [
        {"title": "Employee",   "value": name_line},
        {"title": "Job",        "value": job_name},
        {"title": "Location",   "value": location_name},
        {"title": "Total Hours","value": f"{hours:.1f} hrs"},
    ]
    if weeks is not None:
        facts.append({"title": "Training Weeks", "value": str(weeks)})

    facts.append({"title": "Completed",  "value": completed_at})
    facts.append({"title": "Details",    "value": reason})

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type":    "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type":   "TextBlock",
                            "size":   "Medium",
                            "weight": "Bolder",
                            "text":   "✅ Training Complete",
                            "wrap":   True,
                        },
                        {
                            "type":  "FactSet",
                            "facts": facts,
                        },
                    ],
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# Send one notification
# ---------------------------------------------------------------------------

def send_completion_notification(
    engine,
    completion: dict,
    location_name: str,
    labor_api: LaborAPI,
) -> bool:
    """
    Send a Teams notification for a single training completion.

    Looks up the employee name via the Toast API (falls back to GUID if not found).
    Stamps notified_at in the DB on success.

    Parameters
    ----------
    engine       : Engine
    completion   : dict   Row from get_pending_notifications().
    location_name: str    Human-readable location name for the card.
    labor_api    : LaborAPI

    Returns
    -------
    True if sent successfully, False otherwise.
    """
    employee_guid = completion["employee_guid"]
    job_guid      = completion["job_guid"]
    hours         = float(completion["hours_at_completion"])
    weeks         = completion.get("weeks_at_completion")
    location_guid = completion.get("location_guid_trigger", "")
    completed_at  = str(completion.get("completed_at", ""))

    # Look up job name from DB
    requirements = db.get_training_requirements(engine)
    req = requirements.get(job_guid, {})
    job_name = req.get("job_name", job_guid)

    # Look up employee name + ID from Toast
    employee_name, employee_id = _get_employee_info(labor_api, location_guid or location_name, employee_guid)

    # Build reason string
    reason = _build_reason(req, hours, weeks)

    logger.info(
        "Sending notification  emp=%s (%s)  job=%s  location=%s",
        employee_guid, employee_name, job_name, location_name,
    )

    card = _build_card(
        employee_name=employee_name,
        employee_id=employee_id,
        job_name=job_name,
        location_name=location_name,
        hours=hours,
        weeks=weeks,
        reason=reason,
        completed_at=completed_at[:19].replace("T", " ") if completed_at else "",
    )

    try:
        resp = requests.post(TEAMS_WEBHOOK_URL, json=card, timeout=15)
        resp.raise_for_status()

        # Teams returns "1" on success for simple webhooks
        logger.info("Teams webhook accepted  emp=%s  job=%s", employee_guid, job_guid)
        db.mark_notified(engine, employee_guid, job_guid)
        return True

    except requests.HTTPError as e:
        logger.error(
            "Teams webhook HTTP error  emp=%s  job=%s  status=%s  body=%s",
            employee_guid, job_guid,
            e.response.status_code if e.response else "?",
            e.response.text[:200] if e.response else "",
        )
        return False
    except requests.RequestException as e:
        logger.error("Teams webhook request failed  emp=%s  job=%s: %s",
                     employee_guid, job_guid, e, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Send all pending notifications
# ---------------------------------------------------------------------------

def send_pending_notifications(
    engine,
    labor_api: LaborAPI,
    locations_by_guid: dict[str, str],
) -> tuple[int, int]:
    """
    Send Teams notifications for all completions where notified_at IS NULL.

    Parameters
    ----------
    engine            : Engine
    labor_api         : LaborAPI
    locations_by_guid : dict   { store_guid: location_name }

    Returns
    -------
    (sent, failed) counts
    """
    pending = db.get_pending_notifications(engine)

    if not pending:
        logger.debug("No pending notifications.")
        return 0, 0

    sent = failed = 0
    for completion in pending:
        loc_guid     = completion.get("location_guid_trigger", "")
        location_name = locations_by_guid.get(loc_guid, loc_guid or "Unknown Location")

        success = send_completion_notification(
            engine=engine,
            completion=completion,
            location_name=location_name,
            labor_api=labor_api,
        )
        if success:
            sent += 1
        else:
            failed += 1

    logger.info("Notifications: %d sent, %d failed", sent, failed)
    return sent, failed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_employee_info(
    labor_api: LaborAPI,
    location_guid: str,
    employee_guid: str,
) -> tuple[str, str | None]:
    """
    Look up an employee's display name and externalEmployeeId from Toast.

    Returns
    -------
    (name, external_employee_id)
        name falls back to the GUID string if the API call fails.
        external_employee_id is None if not present on the record.
    """
    try:
        employee = labor_api.get_employee(location_guid, employee_guid)
        if employee:
            first = employee.get("firstName") or ""
            last  = employee.get("lastName") or ""
            name  = f"{first} {last}".strip()
            ext_id = employee.get("externalEmployeeId") or None
            return (name or employee_guid, ext_id)
    except Exception as e:
        logger.warning("Could not fetch employee info for %s: %s", employee_guid, e)

    return employee_guid, None   # fallback


def _build_reason(req: dict, hours: float, weeks: int | None) -> str:
    """Build a short human-readable completion summary."""
    completion_type = req.get("completion_type", "")

    if completion_type == "hours":
        return f"Reached {hours:.1f} hrs (required {req.get('required_hours', '?'):.0f} hrs)"

    elif completion_type == "hours_or_periods":
        required_hours   = req.get("required_hours", 0)
        required_periods = req.get("required_periods", 0)
        if hours >= required_hours:
            return f"Reached {hours:.1f} hrs (required {required_hours:.0f} hrs)"
        return (f"Completed {weeks} full training week(s) "
                f"(required {required_periods}) with {hours:.1f} total hrs")

    elif completion_type == "weekly_pace":
        pace_threshold = req.get("pace_threshold_hrs", 35)
        closed         = weeks or 0
        avg            = hours / closed if closed else 0
        pace_label     = "full-time pace" if avg >= pace_threshold else "part-time pace"
        required       = req.get("weeks_above_pace" if avg >= pace_threshold else "weeks_below_pace", "?")
        return (f"Completed {closed} full training week(s) at {pace_label} "
                f"(avg {avg:.1f} hrs/wk, required {required} weeks)")

    return f"{hours:.1f} hrs"
