-- DDL for enrichment metrics (Symphony_dev). See docs/enrichment-metrics.md.
-- python scripts/load_enrichment_metrics.py  (also loads the JSONL ledger)
-- Pipeline record_run() runs the same batches at the end of each enrichment.

IF OBJECT_ID(N'dbo.EnrichmentRun', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.EnrichmentRun (
        RunId                   nvarchar(80)   NOT NULL,
        RecordedAt              datetime2(0)   NOT NULL CONSTRAINT DF_EnrichmentRun_RecordedAt DEFAULT (sysutcdatetime()),
        Sites                   int            NOT NULL CONSTRAINT DF_EnrichmentRun_Sites DEFAULT (0),
        AppliedRooftop          int            NOT NULL CONSTRAINT DF_EnrichmentRun_AppliedRooftop DEFAULT (0),
        AppliedTower            int            NOT NULL CONSTRAINT DF_EnrichmentRun_AppliedTower DEFAULT (0),
        AppliedDbSkip           int            NOT NULL CONSTRAINT DF_EnrichmentRun_AppliedDbSkip DEFAULT (0),
        HoldoutEmptyConfirmed   int            NOT NULL CONSTRAINT DF_EnrichmentRun_HoldoutEmptyConfirmed DEFAULT (0),
        HoldoutWeakRooftop      int            NOT NULL CONSTRAINT DF_EnrichmentRun_HoldoutWeakRooftop DEFAULT (0),
        HoldoutWeakTower        int            NOT NULL CONSTRAINT DF_EnrichmentRun_HoldoutWeakTower DEFAULT (0),
        HoldoutEmpty            int            NOT NULL CONSTRAINT DF_EnrichmentRun_HoldoutEmpty DEFAULT (0),
        HoldoutNoNearmap        int            NOT NULL CONSTRAINT DF_EnrichmentRun_HoldoutNoNearmap DEFAULT (0),
        Errors                  int            NOT NULL CONSTRAINT DF_EnrichmentRun_Errors DEFAULT (0),
        NearmapSites            int            NOT NULL CONSTRAINT DF_EnrichmentRun_NearmapSites DEFAULT (0),
        ClaudeSites             int            NOT NULL CONSTRAINT DF_EnrichmentRun_ClaudeSites DEFAULT (0),
        NaipEmptyToNearmap      int            NOT NULL CONSTRAINT DF_EnrichmentRun_NaipEmptyToNearmap DEFAULT (0),
        NaipEmptyToRooftop      int            NOT NULL CONSTRAINT DF_EnrichmentRun_NaipEmptyToRooftop DEFAULT (0),
        NaipEmptyToRooftopApply int            NOT NULL CONSTRAINT DF_EnrichmentRun_NaipEmptyToRooftopApply DEFAULT (0),
        EmptyToRooftopApplyRate decimal(6,3)   NULL,
        SfWrites                int            NOT NULL CONSTRAINT DF_EnrichmentRun_SfWrites DEFAULT (0),
        SfHoldoutsDequeued      int            NOT NULL CONSTRAINT DF_EnrichmentRun_SfHoldoutsDequeued DEFAULT (0),
        SfWriteFailed           int            NOT NULL CONSTRAINT DF_EnrichmentRun_SfWriteFailed DEFAULT (0),
        ApplyEnabled            bit            NULL,
        QueueStates             nvarchar(80)   NULL,
        QueueLimit              int            NULL,
        Notes                   nvarchar(400)  NULL,
        CONSTRAINT PK_EnrichmentRun PRIMARY KEY CLUSTERED (RunId)
    );
END
GO

IF OBJECT_ID(N'dbo.EnrichmentSiteOutcome', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.EnrichmentSiteOutcome (
        RunId                 nvarchar(80)   NOT NULL,
        SalesforceId          nvarchar(18)   NOT NULL,
        Address               nvarchar(300)  NULL,
        SiteState             nvarchar(8)    NULL,
        SiteCity              nvarchar(80)   NULL,
        Carrier               nvarchar(120)  NULL,
        MatchSource           nvarchar(32)   NULL,
        DualModelResolution   nvarchar(48)   NULL,
        ClassifyCoordSource   nvarchar(48)   NULL,
        AssetOffsetM          decimal(8,1)   NULL,
        ScreenSiteType        nvarchar(32)   NULL,
        FinalSiteType         nvarchar(32)   NULL,
        FinalConfidence       decimal(4,3)   NULL,
        NearmapRan            bit            NOT NULL CONSTRAINT DF_EnrichmentSite_NearmapRan DEFAULT (0),
        NearmapTier           nvarchar(32)   NULL,
        ClaudeRan             bit            NOT NULL CONSTRAINT DF_EnrichmentSite_ClaudeRan DEFAULT (0),
        EscalationReason      nvarchar(80)   NULL,
        SecondNearmap         nvarchar(32)   NULL,
        EmptyToNearmap        bit            NOT NULL CONSTRAINT DF_EnrichmentSite_EmptyToNearmap DEFAULT (0),
        EmptyToRooftop        bit            NOT NULL CONSTRAINT DF_EnrichmentSite_EmptyToRooftop DEFAULT (0),
        EmptyToRooftopApply   bit            NOT NULL CONSTRAINT DF_EnrichmentSite_EmptyToRooftopApply DEFAULT (0),
        Bucket                nvarchar(64)   NULL,
        HoldoutReason         nvarchar(128)  NULL,
        UpdateSiteType        nvarchar(32)   NULL,
        Outcome               nvarchar(64)   NOT NULL,
        SfUpdateStatus        nvarchar(32)   NULL,
        Notes                 nvarchar(400)  NULL,
        CONSTRAINT PK_EnrichmentSiteOutcome PRIMARY KEY CLUSTERED (RunId, SalesforceId),
        CONSTRAINT FK_EnrichmentSiteOutcome_Run
            FOREIGN KEY (RunId) REFERENCES dbo.EnrichmentRun (RunId)
    );
    CREATE INDEX IX_EnrichmentSiteOutcome_SalesforceId
        ON dbo.EnrichmentSiteOutcome (SalesforceId, RunId);
    CREATE INDEX IX_EnrichmentSiteOutcome_Outcome
        ON dbo.EnrichmentSiteOutcome (Outcome, RunId);
END
GO

IF COL_LENGTH(N'dbo.EnrichmentRun', N'ApplyEnabled') IS NULL
    ALTER TABLE dbo.EnrichmentRun ADD ApplyEnabled bit NULL;
IF COL_LENGTH(N'dbo.EnrichmentRun', N'QueueStates') IS NULL
    ALTER TABLE dbo.EnrichmentRun ADD QueueStates nvarchar(80) NULL;
IF COL_LENGTH(N'dbo.EnrichmentRun', N'QueueLimit') IS NULL
    ALTER TABLE dbo.EnrichmentRun ADD QueueLimit int NULL;
IF COL_LENGTH(N'dbo.EnrichmentSiteOutcome', N'SiteState') IS NULL
    ALTER TABLE dbo.EnrichmentSiteOutcome ADD SiteState nvarchar(8) NULL;
IF COL_LENGTH(N'dbo.EnrichmentSiteOutcome', N'SiteCity') IS NULL
    ALTER TABLE dbo.EnrichmentSiteOutcome ADD SiteCity nvarchar(80) NULL;
IF COL_LENGTH(N'dbo.EnrichmentSiteOutcome', N'Carrier') IS NULL
    ALTER TABLE dbo.EnrichmentSiteOutcome ADD Carrier nvarchar(120) NULL;
IF COL_LENGTH(N'dbo.EnrichmentSiteOutcome', N'MatchSource') IS NULL
    ALTER TABLE dbo.EnrichmentSiteOutcome ADD MatchSource nvarchar(32) NULL;
IF COL_LENGTH(N'dbo.EnrichmentSiteOutcome', N'DualModelResolution') IS NULL
    ALTER TABLE dbo.EnrichmentSiteOutcome ADD DualModelResolution nvarchar(48) NULL;
IF COL_LENGTH(N'dbo.EnrichmentSiteOutcome', N'ClassifyCoordSource') IS NULL
    ALTER TABLE dbo.EnrichmentSiteOutcome ADD ClassifyCoordSource nvarchar(48) NULL;
IF COL_LENGTH(N'dbo.EnrichmentSiteOutcome', N'AssetOffsetM') IS NULL
    ALTER TABLE dbo.EnrichmentSiteOutcome ADD AssetOffsetM decimal(8,1) NULL;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'IX_EnrichmentSiteOutcome_SiteState'
      AND object_id = OBJECT_ID(N'dbo.EnrichmentSiteOutcome')
)
    CREATE INDEX IX_EnrichmentSiteOutcome_SiteState
        ON dbo.EnrichmentSiteOutcome (SiteState, Outcome);
IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'IX_EnrichmentSiteOutcome_MatchSource'
      AND object_id = OBJECT_ID(N'dbo.EnrichmentSiteOutcome')
)
    CREATE INDEX IX_EnrichmentSiteOutcome_MatchSource
        ON dbo.EnrichmentSiteOutcome (MatchSource, Outcome);
GO

IF OBJECT_ID(N'dbo.vEnrichmentKpisByState', N'V') IS NOT NULL
    DROP VIEW dbo.vEnrichmentKpisByState;
GO
IF OBJECT_ID(N'dbo.vEnrichmentKpisByMatchSource', N'V') IS NOT NULL
    DROP VIEW dbo.vEnrichmentKpisByMatchSource;
GO
IF OBJECT_ID(N'dbo.vEnrichmentKpis', N'V') IS NOT NULL
    DROP VIEW dbo.vEnrichmentKpis;
GO
IF OBJECT_ID(N'dbo.vEnrichmentSiteLatest', N'V') IS NOT NULL
    DROP VIEW dbo.vEnrichmentSiteLatest;
GO

CREATE VIEW dbo.vEnrichmentSiteLatest
AS
SELECT o.*
FROM dbo.EnrichmentSiteOutcome AS o
INNER JOIN (
    SELECT
        o2.SalesforceId,
        o2.RunId,
        ROW_NUMBER() OVER (
            PARTITION BY o2.SalesforceId
            ORDER BY r.RecordedAt DESC, o2.RunId DESC
        ) AS rn
    FROM dbo.EnrichmentSiteOutcome AS o2
    INNER JOIN dbo.EnrichmentRun AS r ON r.RunId = o2.RunId
) AS latest
    ON latest.SalesforceId = o.SalesforceId
   AND latest.RunId = o.RunId
   AND latest.rn = 1
GO

CREATE VIEW dbo.vEnrichmentKpis
AS
SELECT
    COUNT(*) AS UniqueSites,
    SUM(CASE WHEN Outcome = N'applied_rooftop' THEN 1 ELSE 0 END) AS RooftopSfWrites,
    SUM(CASE WHEN Outcome = N'applied_tower' THEN 1 ELSE 0 END) AS TowerSfWrites,
    SUM(CASE WHEN Outcome = N'applied_db_skip' THEN 1 ELSE 0 END) AS AppliedDbSkip,
    SUM(CASE WHEN Outcome = N'holdout_empty_confirmed' THEN 1 ELSE 0 END) AS HoldoutEmptyConfirmed,
    SUM(CASE WHEN Outcome = N'holdout_weak_rooftop' THEN 1 ELSE 0 END) AS HoldoutWeakRooftop,
    SUM(CASE WHEN Outcome = N'holdout_weak_tower' THEN 1 ELSE 0 END) AS HoldoutWeakTower,
    SUM(CASE WHEN Outcome = N'holdout_empty' THEN 1 ELSE 0 END) AS HoldoutEmpty,
    SUM(CASE WHEN Outcome = N'holdout_no_nearmap' THEN 1 ELSE 0 END) AS HoldoutNoNearmap,
    SUM(CASE WHEN Outcome = N'error' THEN 1 ELSE 0 END) AS Errors,
    SUM(CASE WHEN NearmapRan = 1 THEN 1 ELSE 0 END) AS NearmapSites,
    SUM(CASE WHEN ClaudeRan = 1 THEN 1 ELSE 0 END) AS ClaudeSites,
    SUM(CASE WHEN EmptyToNearmap = 1 THEN 1 ELSE 0 END) AS NaipEmptyToNearmap,
    SUM(CASE WHEN EmptyToRooftopApply = 1 THEN 1 ELSE 0 END) AS NaipEmptyToRooftopApply,
    CAST(
        SUM(CASE WHEN EmptyToRooftopApply = 1 THEN 1 ELSE 0 END) * 1.0
        / NULLIF(SUM(CASE WHEN EmptyToNearmap = 1 THEN 1 ELSE 0 END), 0)
        AS decimal(6,3)
    ) AS EmptyToRooftopApplyRate,
    CAST(
        SUM(CASE WHEN Outcome = N'applied_rooftop' THEN 1 ELSE 0 END) * 1.0
        / NULLIF(COUNT(*), 0)
        AS decimal(6,3)
    ) AS RooftopWriteRate
FROM dbo.vEnrichmentSiteLatest
GO

CREATE VIEW dbo.vEnrichmentKpisByState
AS
SELECT
    SiteState,
    COUNT(*) AS UniqueSites,
    SUM(CASE WHEN Outcome = N'applied_rooftop' THEN 1 ELSE 0 END) AS RooftopSfWrites,
    SUM(CASE WHEN Outcome = N'applied_tower' THEN 1 ELSE 0 END) AS TowerSfWrites,
    SUM(CASE WHEN Outcome = N'applied_db_skip' THEN 1 ELSE 0 END) AS AppliedDbSkip,
    SUM(CASE WHEN Outcome = N'holdout_empty_confirmed' THEN 1 ELSE 0 END) AS HoldoutEmptyConfirmed,
    SUM(CASE WHEN Outcome = N'holdout_weak_rooftop' THEN 1 ELSE 0 END) AS HoldoutWeakRooftop,
    SUM(CASE WHEN Outcome = N'holdout_weak_tower' THEN 1 ELSE 0 END) AS HoldoutWeakTower,
    SUM(CASE WHEN Outcome = N'holdout_empty' THEN 1 ELSE 0 END) AS HoldoutEmpty,
    SUM(CASE WHEN Outcome = N'holdout_no_nearmap' THEN 1 ELSE 0 END) AS HoldoutNoNearmap,
    SUM(CASE WHEN Outcome = N'error' THEN 1 ELSE 0 END) AS Errors,
    SUM(CASE WHEN NearmapRan = 1 THEN 1 ELSE 0 END) AS NearmapSites,
    SUM(CASE WHEN ClaudeRan = 1 THEN 1 ELSE 0 END) AS ClaudeSites,
    SUM(CASE WHEN EmptyToNearmap = 1 THEN 1 ELSE 0 END) AS NaipEmptyToNearmap,
    SUM(CASE WHEN EmptyToRooftopApply = 1 THEN 1 ELSE 0 END) AS NaipEmptyToRooftopApply,
    CAST(
        SUM(CASE WHEN EmptyToRooftopApply = 1 THEN 1 ELSE 0 END) * 1.0
        / NULLIF(SUM(CASE WHEN EmptyToNearmap = 1 THEN 1 ELSE 0 END), 0)
        AS decimal(6,3)
    ) AS EmptyToRooftopApplyRate,
    CAST(
        SUM(CASE WHEN Outcome = N'applied_rooftop' THEN 1 ELSE 0 END) * 1.0
        / NULLIF(COUNT(*), 0)
        AS decimal(6,3)
    ) AS RooftopWriteRate
FROM dbo.vEnrichmentSiteLatest
GROUP BY SiteState
GO

CREATE VIEW dbo.vEnrichmentKpisByMatchSource
AS
SELECT
    MatchSource,
    COUNT(*) AS UniqueSites,
    SUM(CASE WHEN Outcome = N'applied_rooftop' THEN 1 ELSE 0 END) AS RooftopSfWrites,
    SUM(CASE WHEN Outcome = N'applied_tower' THEN 1 ELSE 0 END) AS TowerSfWrites,
    SUM(CASE WHEN Outcome = N'applied_db_skip' THEN 1 ELSE 0 END) AS AppliedDbSkip,
    SUM(CASE WHEN Outcome = N'holdout_empty_confirmed' THEN 1 ELSE 0 END) AS HoldoutEmptyConfirmed,
    SUM(CASE WHEN Outcome = N'holdout_weak_rooftop' THEN 1 ELSE 0 END) AS HoldoutWeakRooftop,
    SUM(CASE WHEN Outcome = N'holdout_weak_tower' THEN 1 ELSE 0 END) AS HoldoutWeakTower,
    SUM(CASE WHEN Outcome = N'holdout_empty' THEN 1 ELSE 0 END) AS HoldoutEmpty,
    SUM(CASE WHEN Outcome = N'holdout_no_nearmap' THEN 1 ELSE 0 END) AS HoldoutNoNearmap,
    SUM(CASE WHEN Outcome = N'error' THEN 1 ELSE 0 END) AS Errors,
    SUM(CASE WHEN NearmapRan = 1 THEN 1 ELSE 0 END) AS NearmapSites,
    SUM(CASE WHEN ClaudeRan = 1 THEN 1 ELSE 0 END) AS ClaudeSites,
    SUM(CASE WHEN EmptyToNearmap = 1 THEN 1 ELSE 0 END) AS NaipEmptyToNearmap,
    SUM(CASE WHEN EmptyToRooftopApply = 1 THEN 1 ELSE 0 END) AS NaipEmptyToRooftopApply,
    CAST(
        SUM(CASE WHEN EmptyToRooftopApply = 1 THEN 1 ELSE 0 END) * 1.0
        / NULLIF(SUM(CASE WHEN EmptyToNearmap = 1 THEN 1 ELSE 0 END), 0)
        AS decimal(6,3)
    ) AS EmptyToRooftopApplyRate,
    CAST(
        SUM(CASE WHEN Outcome = N'applied_rooftop' THEN 1 ELSE 0 END) * 1.0
        / NULLIF(COUNT(*), 0)
        AS decimal(6,3)
    ) AS RooftopWriteRate
FROM dbo.vEnrichmentSiteLatest
GROUP BY MatchSource
GO
