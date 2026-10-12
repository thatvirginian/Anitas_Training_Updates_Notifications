# -*- coding: utf-8 -*-
"""
labor_api.py

Toast Labor API client for the Training Hours service.
Handles all /labor/v1/* calls: time entries and employees.

Auth is delegated to the injected ToastAPI instance — this module never
touches credentials or tokens directly.

Usage:
    from src.toast_api import ToastAPI
    from src.labor_api import LaborAPI

    api   = ToastAPI(engine=engine)
    labor = LaborAPI(api)

    entries   = labor.get_time_entries(location_guid, business_date="20261008")
    employees = labor.get_employees(location_guid)
"""

import logging
import requests
from datetime import date, datetime, timezone
from typing import Optional

logger = logging.getLogger("LaborAPI")


class LaborAPI:
    """
    Wraps Toast /labor/v1 endpoints.

    Parameters
    ----------
    toast_api : ToastAPI
        The shared auth client.  get_headers() is called per request so
        token refresh is handled transparently.
    """

    def __init__(self, toast_api):
        self._api = toast_api
        self.base_url = toast_api.base_url

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self, location_guid: str) -> dict:
        """Auth headers plus the required restaurant-scoping header."""
        return {
            **self._api.get_headers(),
            "Toast-Restaurant-External-ID": str(location_guid),
        }

    def _get(self, path: str, location_guid: str, params: dict = None) -> list:
        """
        GET a Toast labor endpoint and return the JSON list.

        Raises requests.HTTPError on a non-2xx response.
        """
        url = f"{self.base_url}{path}"
        resp = requests.get(
            url,
            headers=self._headers(location_guid),
            params=params or {},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Time Entries
    # ------------------------------------------------------------------

    def get_time_entries_by_business_date(
        self,
        location_guid: str,
        business_date: date,
    ) -> list[dict]:
        """
        Pull all time entries for a single business date.

        business_date mode never returns deleted/archived entries, so this
        is clean for the daily "new entries" sweep.

        Parameters
        ----------
        location_guid : str
        business_date : date
            The calendar date to query.  Formatted as YYYYMMDD for the API.

        Returns
        -------
        list[dict]
            Raw Toast time-entry objects.
        """
        bd_str = business_date.strftime("%Y%m%d")
        logger.debug(
            "get_time_entries_by_business_date  location=%s  date=%s",
            location_guid, bd_str,
        )
        return self._get(
            "/labor/v1/timeEntries",
            location_guid,
            params={"businessDate": bd_str},
        )

    def get_time_entries_by_modified_range(
        self,
        location_guid: str,
        start: datetime,
        end: datetime,
    ) -> list[dict]:
        """
        Pull time entries modified within [start, end).

        This mode ALWAYS includes deleted/archived entries (Toast behaviour).
        Use for the daily reconciliation window to catch edits and deletions.

        Parameters
        ----------
        location_guid : str
        start, end : datetime
            Timezone-aware datetimes.  Formatted as ISO-8601 for the API.

        Returns
        -------
        list[dict]
            Raw Toast time-entry objects, including deleted ones.
        """
        logger.debug(
            "get_time_entries_by_modified_range  location=%s  %s → %s",
            location_guid, start.isoformat(), end.isoformat(),
        )
        # Toast expects ISO-8601 with Z suffix, not +00:00, and no microseconds
        def _fmt(dt: datetime) -> str:
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        return self._get(
            "/labor/v1/timeEntries",
            location_guid,
            params={
                "modifiedStartDate": _fmt(start),
                "modifiedEndDate":   _fmt(end),
            },
        )

    # ------------------------------------------------------------------
    # Employees
    # ------------------------------------------------------------------

    def get_employee(self, location_guid: str, employee_guid: str) -> dict | None:
        """
        Fetch a single employee by GUID.

        Use this when you already have an employeeReference.guid from a time
        entry and just need the name for a notification.  Cheaper than pulling
        all employees at the location.

        Returns None if the employee is not found (404), so callers can fall
        back to displaying the GUID without crashing.

        Parameters
        ----------
        location_guid  : str   Toast restaurant GUID (for the header)
        employee_guid  : str   Toast employee GUID (path parameter)

        Returns
        -------
        dict | None
            Raw Toast employee object, or None on 404.
            Key fields: guid, firstName, chosenName, lastName, email.
        """
        logger.debug("get_employee  location=%s  employee=%s", location_guid, employee_guid)
        try:
            return self._get(f"/labor/v1/employees/{employee_guid}", location_guid)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.warning("Employee %s not found at location %s", employee_guid, location_guid)
                return None
            raise

    def get_employees(self, location_guid: str) -> list[dict]:
        """
        Fetch all employees for a location.

        Use for a bulk name-cache seed/refresh.  For one-off lookups during
        notification sending, prefer get_employee() instead.

        Parameters
        ----------
        location_guid : str

        Returns
        -------
        list[dict]
            Raw Toast employee objects.
            Key fields: guid, firstName, chosenName, lastName, email, deleted.
        """
        logger.debug("get_employees  location=%s", location_guid)
        return self._get("/labor/v1/employees", location_guid)

    def get_employees_by_ids(
        self,
        location_guid: str,
        employee_guids: list[str],
    ) -> list[dict]:
        """
        Fetch a batch of employees by GUID in a single request.

        Toast accepts up to 100 employeeIds per call.  This method chunks
        automatically for larger lists and merges the results.

        Use this during the CALCULATE step to check deleted status for all
        employees with open training records in one pass.

        Parameters
        ----------
        location_guid   : str
        employee_guids  : list[str]   Up to any number; chunked internally.

        Returns
        -------
        list[dict]
            Raw Toast employee objects (may include deleted employees).
            Key fields: guid, firstName, lastName, deleted, deletedDate.
        """
        CHUNK_SIZE = 100
        results: list[dict] = []

        for i in range(0, len(employee_guids), CHUNK_SIZE):
            chunk = employee_guids[i : i + CHUNK_SIZE]
            logger.debug(
                "get_employees_by_ids  location=%s  ids=%d  chunk=%d-%d",
                location_guid, len(employee_guids), i, i + len(chunk),
            )
            # requests encodes repeated keys as ?employeeIds=a&employeeIds=b
            params = [("employeeIds", guid) for guid in chunk]
            url  = f"{self.base_url}/labor/v1/employees"
            resp = requests.get(
                url,
                headers=self._headers(location_guid),
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json() or []
            results.extend(e for e in batch if e is not None)

        return results
