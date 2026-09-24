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

-- Outcome columns added when UniqueSites switched to "every processed site".
IF COL_LENGTH(N'dbo.EnrichmentRun', N'AppliedOther') IS NULL
    ALTER TABLE dbo.EnrichmentRun ADD AppliedOther int NOT NULL CONSTRAINT DF_EnrichmentRun_AppliedOther DEFAULT (0);
IF COL_LENGTH(N'dbo.EnrichmentRun', N'ApplyFailed') IS NULL
    ALTER TABLE dbo.EnrichmentRun ADD ApplyFailed int NOT NULL CONSTRAINT DF_EnrichmentRun_ApplyFailed DEFAULT (0);
IF COL_LENGTH(N'dbo.EnrichmentRun', N'HoldoutNoImagery') IS NULL
    ALTER TABLE dbo.EnrichmentRun ADD HoldoutNoImagery int NOT NULL CONSTRAINT DF_EnrichmentRun_HoldoutNoImagery DEFAULT (0);
IF COL_LENGTH(N'dbo.EnrichmentRun', N'HoldoutOther') IS NULL
    ALTER TABLE dbo.EnrichmentRun ADD HoldoutOther int NOT NULL CONSTRAINT DF_EnrichmentRun_HoldoutOther DEFAULT (0);
IF COL_LENGTH(N'dbo.EnrichmentRun', N'DbOnlyMiss') IS NULL
    ALTER TABLE dbo.EnrichmentRun ADD DbOnlyMiss int NOT NULL CONSTRAINT DF_EnrichmentRun_DbOnlyMiss DEFAULT (0);
IF COL_LENGTH(N'dbo.EnrichmentRun', N'Skipped') IS NULL
    ALTER TABLE dbo.EnrichmentRun ADD Skipped int NOT NULL CONSTRAINT DF_EnrichmentRun_Skipped DEFAULT (0);
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
IF OBJECT_ID(N'dbo.vEnrichmentKpisByDimension', N'V') IS NOT NULL
    DROP VIEW dbo.vEnrichmentKpisByDimension;
GO
IF OBJECT_ID(N'dbo.vEnrichmentSiteFacts', N'V') IS NOT NULL
    DROP VIEW dbo.vEnrichmentSiteFacts;
GO
IF OBJECT_ID(N'dbo.vEnrichmentSiteLatestWrite', N'V') IS NOT NULL
    DROP VIEW dbo.vEnrichmentSiteLatestWrite;
GO
IF OBJECT_ID(N'dbo.vEnrichmentSiteLatest', N'V') IS NOT NULL
    DROP VIEW dbo.vEnrichmentSiteLatest;
GO

-- Latest observation per Salesforce Id across live runs (applied or not).
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

-- Latest Salesforce-accepted site-type/coords write per Id.
CREATE VIEW dbo.vEnrichmentSiteLatestWrite
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
    WHERE o2.Outcome IN (
            N'applied_rooftop', N'applied_tower', N'applied_db_skip', N'applied_other'
        )
      AND ISNULL(o2.HoldoutReason, N'') <> N'db_only_no_unique_hit'
      AND o2.SfUpdateStatus = N'updated'
) AS latest
    ON latest.SalesforceId = o.SalesforceId
   AND latest.RunId = o.RunId
   AND latest.rn = 1
GO

-- One row per processed Id: latest outcome, write flags, "ever ran" spend.
CREATE VIEW dbo.vEnrichmentSiteFacts
AS
SELECT
    l.SalesforceId,
    l.SiteState,
    l.MatchSource,
    l.Outcome,
    CASE WHEN w.SalesforceId IS NULL THEN 0 ELSE 1 END AS IsWritten,
    -- Rooftop / tower by the Site_Type written (imagery or tower database);
    -- DbSkipWrite is the tower-database share of those.
    CASE WHEN w.FinalSiteType = N'rooftop'
          OR (w.FinalSiteType IS NULL AND w.Outcome = N'applied_rooftop') THEN 1 ELSE 0 END AS RooftopWrite,
    CASE WHEN w.FinalSiteType = N'tower'
          OR (w.FinalSiteType IS NULL AND w.Outcome = N'applied_tower') THEN 1 ELSE 0 END AS TowerWrite,
    CASE WHEN w.Outcome = N'applied_db_skip' THEN 1 ELSE 0 END AS DbSkipWrite,
    CASE WHEN w.EmptyToRooftopApply = 1 THEN 1 ELSE 0 END AS EmptyToRooftopApply,
    e.NearmapEver,
    e.ClaudeEver,
    e.EmptyToNearmapEver
FROM dbo.vEnrichmentSiteLatest AS l
LEFT JOIN dbo.vEnrichmentSiteLatestWrite AS w
    ON w.SalesforceId = l.SalesforceId
INNER JOIN (
    SELECT
        SalesforceId,
        MAX(CAST(NearmapRan AS int)) AS NearmapEver,
        MAX(CAST(ClaudeRan AS int)) AS ClaudeEver,
        MAX(CAST(EmptyToNearmap AS int)) AS EmptyToNearmapEver
    FROM dbo.EnrichmentSiteOutcome
    GROUP BY SalesforceId
) AS e
    ON e.SalesforceId = l.SalesforceId
GO

-- KPI columns defined once; Dimension = all | state | match_source.
-- UniqueSites = every processed Id; WrittenSites = Salesforce-accepted writes.
CREATE VIEW dbo.vEnrichmentKpisByDimension
AS
SELECT
    CASE
        WHEN GROUPING(SiteState) = 0 THEN N'state'
        WHEN GROUPING(MatchSource) = 0 THEN N'match_source'
        ELSE N'all'
    END AS Dimension,
    SiteState,
    MatchSource,
    COUNT(*) AS UniqueSites,
    SUM(IsWritten) AS WrittenSites,
    SUM(RooftopWrite) AS RooftopSfWrites,
    SUM(TowerWrite) AS TowerSfWrites,
    SUM(DbSkipWrite) AS AppliedDbSkip,
    SUM(CASE WHEN Outcome = N'apply_failed' THEN 1 ELSE 0 END) AS ApplyFailed,
    SUM(CASE WHEN Outcome = N'holdout_empty_confirmed' THEN 1 ELSE 0 END) AS HoldoutEmptyConfirmed,
    SUM(CASE WHEN Outcome = N'holdout_weak_rooftop' THEN 1 ELSE 0 END) AS HoldoutWeakRooftop,
    SUM(CASE WHEN Outcome = N'holdout_weak_tower' THEN 1 ELSE 0 END) AS HoldoutWeakTower,
    SUM(CASE WHEN Outcome = N'holdout_empty' THEN 1 ELSE 0 END) AS HoldoutEmpty,
    SUM(CASE WHEN Outcome = N'holdout_no_nearmap' THEN 1 ELSE 0 END) AS HoldoutNoNearmap,
    SUM(CASE WHEN Outcome = N'holdout_no_imagery' THEN 1 ELSE 0 END) AS HoldoutNoImagery,
    SUM(CASE WHEN Outcome = N'holdout_other' THEN 1 ELSE 0 END) AS HoldoutOther,
    SUM(CASE WHEN Outcome = N'db_only_miss' THEN 1 ELSE 0 END) AS DbOnlyMiss,
    SUM(CASE WHEN Outcome = N'skipped' THEN 1 ELSE 0 END) AS Skipped,
    SUM(CASE WHEN Outcome = N'error' THEN 1 ELSE 0 END) AS Errors,
    SUM(NearmapEver) AS NearmapSites,
    SUM(ClaudeEver) AS ClaudeSites,
    SUM(EmptyToNearmapEver) AS NaipEmptyToNearmap,
    SUM(EmptyToRooftopApply) AS NaipEmptyToRooftopApply,
    CAST(SUM(EmptyToRooftopApply) * 1.0 / NULLIF(SUM(EmptyToNearmapEver), 0) AS decimal(6,3))
        AS EmptyToRooftopApplyRate,
    CAST(SUM(RooftopWrite) * 1.0 / NULLIF(COUNT(*), 0) AS decimal(6,3)) AS RooftopWriteRate,
    CAST(SUM(TowerWrite) * 1.0 / NULLIF(COUNT(*), 0) AS decimal(6,3)) AS TowerWriteRate,
    CAST(SUM(IsWritten) * 1.0 / NULLIF(COUNT(*), 0) AS decimal(6,3)) AS TotalWriteRate
FROM dbo.vEnrichmentSiteFacts
GROUP BY GROUPING SETS ((), (SiteState), (MatchSource))
GO

CREATE VIEW dbo.vEnrichmentKpis
AS
SELECT * FROM dbo.vEnrichmentKpisByDimension WHERE Dimension = N'all'
GO

CREATE VIEW dbo.vEnrichmentKpisByState
AS
SELECT * FROM dbo.vEnrichmentKpisByDimension WHERE Dimension = N'state'
GO

CREATE VIEW dbo.vEnrichmentKpisByMatchSource
AS
SELECT * FROM dbo.vEnrichmentKpisByDimension WHERE Dimension = N'match_source'
GO
