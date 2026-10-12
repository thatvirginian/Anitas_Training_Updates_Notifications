# -*- coding: utf-8 -*-
"""
config.py

Training-specific configuration for the Toast Training Hours service.
DB credentials and Toast API credentials are handled by database_setup.py
and toast_api.py respectively — nothing is duplicated here.
"""

import os
from dotenv import load_dotenv

load_dotenv(override=True)


# =============================================================================
# Notifications
# =============================================================================

TEAMS_WEBHOOK_URL: str = os.environ["TEAMS_WEBHOOK_URL"]


# =============================================================================
# Training job GUIDs
# Confirmed 2026-10-08 — shared across all 11 locations.
# These are the only jobs whose time entries are saved locally.
# =============================================================================

TRAINING_JOB_GUIDS: dict[str, str] = {
    "6a467863-1082-46a0-8bc1-e18a96f0746d": "Server in Training",
    "1f8502da-888c-4523-96bd-4cecd6e371fa": "Cashier in Training",
    "439493a2-9ee6-4852-9486-fa93ea3652c1": "Cook in Training",
}

# Set for O(1) membership checks during time-entry filtering
TRAINING_JOB_GUID_SET: set[str] = set(TRAINING_JOB_GUIDS.keys())


# =============================================================================
# Week boundary
# Training weeks run Wednesday–Tuesday.
# Python weekday(): Monday=0 … Sunday=6  →  Wednesday=2
# =============================================================================

WEEK_START_WEEKDAY: int = 2


# =============================================================================
# Polling windows
# =============================================================================

# Business dates to poll each run (default 1 = yesterday only).
# Raise temporarily after a gap or outage.
BUSINESS_DATE_LOOKBACK_DAYS: int = int(os.getenv("BUSINESS_DATE_LOOKBACK_DAYS", "1"))

# Days to look back in the modified-date reconciliation window.
# Catches corrections to older entries. Toast API max is 30.
MODIFIED_LOOKBACK_DAYS: int = int(os.getenv("MODIFIED_LOOKBACK_DAYS", "8"))


# =============================================================================
# Poll log retention
# =============================================================================

POLL_LOG_RETENTION_DAYS: int = int(os.getenv("POLL_LOG_RETENTION_DAYS", "90"))


# =============================================================================
# Locations to exclude from training tracking
# Add store_guid strings (comma-separated) to EXCLUDED_LOCATION_GUIDS in .env
# to skip a location without deactivating it in toast_order_data.
# Currently empty — all 11 locations are processed.
# =============================================================================

EXCLUDED_LOCATION_GUIDS: set[str] = {
    g.strip()
    for g in os.getenv("EXCLUDED_LOCATION_GUIDS", "").split(",")
    if g.strip()
}