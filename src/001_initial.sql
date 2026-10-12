-- =============================================================================
-- Toast Training Hours Service
-- Migration: 001_initial
-- Database: payroll_training
--
-- Location data is read from toast_order_data.public.locations at runtime.
-- store_guid in that table is the Toast restaurant GUID used as
-- Toast-Restaurant-External-ID. No locations table is needed here.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. API TOKENS
--    Stores the cached Toast API token for this service.
--    One row, keyed on service_name = 'toast_training'.
--    Shared across all location runs within a single daily job execution.
--    The ToastAPI client checks this before re-authenticating.
-- ---------------------------------------------------------------------------
CREATE TABLE api_tokens (
    service_name    TEXT            PRIMARY KEY,
    access_token    TEXT            NOT NULL,
    client_id       TEXT            NOT NULL,
    expires_at      FLOAT           NOT NULL,
    created_at      FLOAT           NOT NULL,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  api_tokens IS
    'Cached Toast API bearer token for the training service. '
    'One row (service_name = toast_training). Refreshed automatically when expired.';


-- ---------------------------------------------------------------------------
-- 2. TRAINING REQUIREMENTS
--    Defines which Toast jobs are training jobs and what completion looks like.
--    Populated on first run by sync_jobs(), then reviewed and configured.
--
--    location_guid references toast_order_data.public.locations.store_guid
--    (cross-database reference — no FK constraint enforced here).
--
--    completion_type values:
--      'hours'             – Server:  reach required_hours total
--      'hours_or_periods'  – Cashier: reach required_hours OR required_periods
--                            full closed Wed–Tue weeks with any training hours
--      'weekly_pace'       – Cook:    average weekly hours decides threshold;
--                            >= pace_threshold_hrs/wk → need weeks_above_pace closed weeks
--                            <  pace_threshold_hrs/wk → need weeks_below_pace closed weeks
--
--    A "closed week" is a Wed–Tue window whose Tuesday is before today.
--    Only closed weeks count toward Cashier periods and Cook weekly totals.
-- ---------------------------------------------------------------------------
CREATE TABLE training_requirements (
    job_guid                UUID            PRIMARY KEY,
    job_name                TEXT            NOT NULL,
    completion_type         TEXT            NOT NULL
                                CHECK (completion_type IN (
                                    'hours',
                                    'hours_or_periods',
                                    'weekly_pace'
                                )),

    -- Used by: hours, hours_or_periods
    required_hours          NUMERIC(6,2),

    -- Used by: hours_or_periods (Cashier)
    -- Number of full closed Wed–Tue weeks required as alternative to hours
    required_periods        INTEGER,

    -- Used by: weekly_pace (Cook)
    pace_threshold_hrs      NUMERIC(6,2),   -- weekly hour threshold (35.0)
    weeks_above_pace        INTEGER,        -- weeks needed if avg >= threshold (3)
    weeks_below_pace        INTEGER,        -- weeks needed if avg <  threshold (5)

    active                  BOOLEAN         NOT NULL DEFAULT TRUE,
    created_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  training_requirements IS
    'Configures which Toast jobs are training jobs and their completion rules. '
    'Populated by sync_jobs() on first run — set completion_type and thresholds after.';

COMMENT ON COLUMN training_requirements.completion_type IS
    'hours | hours_or_periods | weekly_pace';

COMMENT ON COLUMN training_requirements.required_hours IS
    'Server: 24 hrs. Cashier: 70 hrs (hours leg of OR condition).';

COMMENT ON COLUMN training_requirements.required_periods IS
    'Cashier: 2 full closed Wed–Tue weeks (period leg of OR condition).';

COMMENT ON COLUMN training_requirements.pace_threshold_hrs IS
    'Cook: 35 hrs/week divides fast track (3 weeks) from slow track (5 weeks).';

COMMENT ON COLUMN training_requirements.weeks_above_pace IS
    'Cook: closed weeks needed when average weekly hours >= pace_threshold_hrs.';

COMMENT ON COLUMN training_requirements.weeks_below_pace IS
    'Cook: closed weeks needed when average weekly hours < pace_threshold_hrs.';


-- ---------------------------------------------------------------------------
-- 2. TRAINING TIME ENTRIES
--    Local copy of Toast time entries whose job is a configured training job.
--    Toast is the source of truth — rows are UPSERTed by toast_guid daily.
--    Only training-job entries are stored; all other shifts are ignored.
--
--    hours = regularHours + overtimeHours from the Toast API response.
--    Entries with deleted = TRUE are excluded from all hour calculations.
--    Entries with NULL out_date (still clocked in) are never saved.
--
--    location_guid references toast_order_data.public.locations.store_guid.
-- ---------------------------------------------------------------------------
CREATE TABLE training_time_entries (
    toast_guid      UUID            PRIMARY KEY,
    location_guid   UUID            NOT NULL,
    employee_guid   UUID            NOT NULL,
    job_guid        UUID            NOT NULL,
    business_date   DATE            NOT NULL,
    in_date         TIMESTAMPTZ     NOT NULL,
    out_date        TIMESTAMPTZ     NOT NULL,
    hours           NUMERIC(6,2)    NOT NULL,
    modified_date   TIMESTAMPTZ     NOT NULL,
    deleted         BOOLEAN         NOT NULL DEFAULT FALSE,
    upserted_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- Primary driver for daily completion check
CREATE INDEX idx_tte_employee_job
    ON training_time_entries (employee_guid, job_guid)
    WHERE deleted = FALSE;

-- Drives modified-date reconciliation window queries
CREATE INDEX idx_tte_modified
    ON training_time_entries (modified_date);

-- Drives business-date queries scoped to a location
CREATE INDEX idx_tte_location_date
    ON training_time_entries (location_guid, business_date);

COMMENT ON TABLE  training_time_entries IS
    'Filtered local copy of Toast time entries for training jobs only. '
    'UPSERTed daily by toast_guid. Non-training entries are never stored.';

COMMENT ON COLUMN training_time_entries.toast_guid IS
    'Toast time-entry GUID — primary key enabling safe daily UPSERT.';

COMMENT ON COLUMN training_time_entries.hours IS
    'regularHours + overtimeHours from the Toast API response.';

COMMENT ON COLUMN training_time_entries.deleted IS
    'TRUE when Toast returns this entry with deleted=true. Excluded from hour totals.';

COMMENT ON COLUMN training_time_entries.out_date IS
    'NOT NULL — entries with null out_date (still clocked in) are skipped and never saved.';


-- ---------------------------------------------------------------------------
-- 3. TRAINING COMPLETIONS
--    One permanent row per employee + training job when threshold is first met.
--    Global scope — no location_guid in the PK. Hours from all locations pool
--    together toward a single global completion per employee+job.
--
--    notified_at is set only after the Teams message is confirmed sent.
--    Rows where notified_at IS NULL are retried on the next run.
--    A completion row is never deleted or reversed.
-- ---------------------------------------------------------------------------
CREATE TABLE training_completions (
    employee_guid           UUID            NOT NULL,
    job_guid                UUID            NOT NULL,
    completed_at            TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    hours_at_completion     NUMERIC(6,2)    NOT NULL,
    weeks_at_completion     INTEGER,
    location_guid_trigger   UUID,
    notified_at             TIMESTAMPTZ,

    PRIMARY KEY (employee_guid, job_guid)
);

COMMENT ON TABLE  training_completions IS
    'Permanent record of training completions. One row per employee+job, ever. '
    'Never deleted or reversed. Global scope — hours pool across all locations.';

COMMENT ON COLUMN training_completions.notified_at IS
    'NULL until Teams notification confirmed sent. Next run retries any NULL rows.';

COMMENT ON COLUMN training_completions.weeks_at_completion IS
    'Populated for Cashier (hours_or_periods) and Cook (weekly_pace). NULL for Server.';

COMMENT ON COLUMN training_completions.location_guid_trigger IS
    'The location whose daily run first detected this completion.';


-- ---------------------------------------------------------------------------
-- 4. POLL LOG
--    Audit trail of every Toast API poll per location per run.
--    Used to detect gaps, debug failures, and confirm processing windows.
--    Prune rows older than 90 days — handled automatically by main.py.
-- ---------------------------------------------------------------------------
CREATE TABLE poll_log (
    id              SERIAL          PRIMARY KEY,
    run_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    location_guid   UUID            NOT NULL,
    poll_type       TEXT            NOT NULL
                        CHECK (poll_type IN ('business_date', 'modified_range')),
    window_start    TEXT            NOT NULL,
    window_end      TEXT,
    entries_found   INTEGER,
    training_saved  INTEGER,
    success         BOOLEAN         NOT NULL DEFAULT TRUE,
    error           TEXT
);

CREATE INDEX idx_poll_log_location_run
    ON poll_log (location_guid, run_at DESC);

COMMENT ON TABLE  poll_log IS
    'Audit log of every Toast API poll. Rows older than 90 days are pruned by main.py.';

COMMENT ON COLUMN poll_log.poll_type IS
    'business_date = yesterday''s new entries; modified_range = reconciliation window.';

COMMENT ON COLUMN poll_log.window_start IS
    'YYYYMMDD for business_date polls; ISO-8601 datetime for modified_range polls.';

COMMENT ON COLUMN poll_log.window_end IS
    'NULL for business_date polls (single day); ISO-8601 datetime for modified_range.';


-- ---------------------------------------------------------------------------
-- Post-deployment checklist
-- ---------------------------------------------------------------------------
--
-- 1. Run sync_jobs() to populate training_requirements with job_guids from Toast.
--
-- 2. For each location + job row created, run:
--
--    -- Server
--    UPDATE training_requirements
--       SET completion_type  = 'hours',
--           required_hours   = 24
--     WHERE job_name LIKE '%SERVER%';
--
--    -- Cashier
--    UPDATE training_requirements
--       SET completion_type  = 'hours_or_periods',
--           required_hours   = 70,
--           required_periods = 2
--     WHERE job_name LIKE '%CASHIER%';
--
--    -- Cook
--    UPDATE training_requirements
--       SET completion_type      = 'weekly_pace',
--           pace_threshold_hrs   = 35,
--           weeks_above_pace     = 3,
--           weeks_below_pace     = 5
--     WHERE job_name LIKE '%COOK%';
--
-- 3. Verify active = TRUE for all rows you want processed.
--
-- ---------------------------------------------------------------------------
