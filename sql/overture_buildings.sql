-- DDL for Overture Maps building footprints near Salesforce sites.
-- python scripts/load_overture_buildings.py load   (runs these batches, then loads)
-- Source: Overture Maps buildings theme (ODbL; attribute derived records),
-- only buildings within ~100 m of a Site__c pin. Queried by
-- enrichment/footprints.py with a centroid lat/lon window, then
-- shape.STContains / STDistance for the pin check.

IF OBJECT_ID(N'dbo.OvertureBuilding', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.OvertureBuilding (
        building_id     varchar(40)   NOT NULL,
        centroid_lat    float         NOT NULL,
        centroid_lon    float         NOT NULL,
        xmin            float         NOT NULL,
        ymin            float         NOT NULL,
        xmax            float         NOT NULL,
        ymax            float         NOT NULL,
        area_m2         float         NULL,
        height_m        float         NULL,
        num_floors      smallint      NULL,
        building_class  varchar(40)   NULL,
        subtype         varchar(40)   NULL,
        shape           geometry      NOT NULL,
        overture_release varchar(16)  NOT NULL,
        loaded_at       datetime2(0)  NOT NULL CONSTRAINT DF_OvertureBuilding_LoadedAt DEFAULT (sysutcdatetime()),
        CONSTRAINT PK_OvertureBuilding PRIMARY KEY CLUSTERED (building_id)
    );
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'IX_OvertureBuilding_LatLon'
      AND object_id = OBJECT_ID(N'dbo.OvertureBuilding')
)
    CREATE NONCLUSTERED INDEX IX_OvertureBuilding_LatLon
        ON dbo.OvertureBuilding (centroid_lat, centroid_lon)
        INCLUDE (xmin, ymin, xmax, ymax, area_m2, height_m, num_floors, building_class);
GO

-- Loader staging table: filled with executemany (outline as WKB), then
-- swapped into the target inside one transaction and dropped.
IF OBJECT_ID(N'dbo.OvertureBuilding_Staging', N'U') IS NOT NULL
    DROP TABLE dbo.OvertureBuilding_Staging;
GO

CREATE TABLE dbo.OvertureBuilding_Staging (
    building_id     varchar(40)     NOT NULL,
    centroid_lat    float           NOT NULL,
    centroid_lon    float           NOT NULL,
    xmin            float           NOT NULL,
    ymin            float           NOT NULL,
    xmax            float           NOT NULL,
    ymax            float           NOT NULL,
    area_m2         float           NULL,
    height_m        float           NULL,
    num_floors      smallint        NULL,
    building_class  varchar(40)     NULL,
    subtype         varchar(40)     NULL,
    shape_wkb       varbinary(max)  NOT NULL,
    overture_release varchar(16)    NOT NULL
);
GO
