-- DDL for FCC ULS microwave license locations (rooftop backhaul evidence signal).
-- python scripts/load_fcc_uls_microwave.py --dry-run   (parse only)
-- python scripts/load_fcc_uls_microwave.py             (runs these batches, then loads)
-- Source: ULS complete weekly microwave file l_micro.zip (HD.dat + LO.dat),
-- active licenses (license_status 'A') with valid coordinates only.
-- Queried by enrichment/signals/uls.py with a lat/lon bbox like dbo.FCCTowerData.

IF OBJECT_ID(N'dbo.FccUlsMicrowaveLocation', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.FccUlsMicrowaveLocation (
        unique_system_identifier bigint        NOT NULL,
        call_sign                varchar(10)   NOT NULL,
        location_number          int           NOT NULL,
        latitude                 float         NOT NULL,
        longitude                float         NOT NULL,
        ground_elevation_m       float         NULL,
        structure_height_m       float         NULL,
        location_type            varchar(4)    NULL,
        license_status           char(1)       NULL,
        radio_service_code       varchar(4)    NULL,
        structure_type           varchar(16)   NULL,
        loaded_at                datetime2(0)  NOT NULL CONSTRAINT DF_FccUlsMicrowaveLocation_LoadedAt DEFAULT (sysutcdatetime()),
        CONSTRAINT PK_FccUlsMicrowaveLocation PRIMARY KEY CLUSTERED (unique_system_identifier, location_number)
    );
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = N'IX_FccUlsMicrowaveLocation_LatLon'
      AND object_id = OBJECT_ID(N'dbo.FccUlsMicrowaveLocation')
)
    CREATE NONCLUSTERED INDEX IX_FccUlsMicrowaveLocation_LatLon
        ON dbo.FccUlsMicrowaveLocation (latitude, longitude)
        INCLUDE (call_sign, structure_type);
GO

-- Loader staging table: filled with executemany, then swapped into the
-- target inside one transaction (DELETE + INSERT ... SELECT) and dropped.
IF OBJECT_ID(N'dbo.FccUlsMicrowaveLocation_Staging', N'U') IS NOT NULL
    DROP TABLE dbo.FccUlsMicrowaveLocation_Staging;
GO

CREATE TABLE dbo.FccUlsMicrowaveLocation_Staging (
    unique_system_identifier bigint        NOT NULL,
    call_sign                varchar(10)   NOT NULL,
    location_number          int           NOT NULL,
    latitude                 float         NOT NULL,
    longitude                float         NOT NULL,
    ground_elevation_m       float         NULL,
    structure_height_m       float         NULL,
    location_type            varchar(4)    NULL,
    license_status           char(1)       NULL,
    radio_service_code       varchar(4)    NULL,
    structure_type           varchar(16)   NULL
);
GO
