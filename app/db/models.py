"""The three tables this worker writes, as SQLAlchemy sees them.

These are created and migrated by Entity Framework in the AgriCentral API
(data/SatelliteCellValue.cs and friends, configured in ApplicationDbContext). This module
does NOT create them and must never be asked to - there is deliberately no create_all call
anywhere in this service. It only describes them so the worker can read and write rows.

Names are PascalCase because that is how EF Core creates them, matching the other 154
tables in the database. Every name here is a contract with the C# side: change one there
without changing it here and the worker fails at runtime, not at build time.
"""
from __future__ import annotations

from sqlalchemy import (BigInteger, Column, Date, DateTime, Float, Index, Integer,
                        LargeBinary, MetaData,
                        String, Table, Text, func)

# No schema: ApplicationDbContext does not use one, so these sit in the default schema
# alongside every other table.
metadata = MetaData()

# One row per 20 m analysis cell. The Sentinel-2 grid defines the cells and the coarser
# datasets are sampled onto it, so about 25 rows share one Landsat temperature.
# NULL means "not measured" - never zero.
cell_values = Table(
    "SatelliteCellValues", metadata,
    Column("FarmId", String(64), primary_key=True),
    Column("CellRow", Integer, primary_key=True),
    Column("CellCol", Integer, primary_key=True),
    Column("ZoneId", String(64), nullable=True),
    Column("Lat", Float, nullable=False),
    Column("Lon", Float, nullable=False),
    Column("Ndvi", Float, nullable=True),            # leaf cover, mixed canopy
    Column("Ndmi", Float, nullable=True),            # canopy moisture index
    Column("NdviChange", Float, nullable=True),      # vs baseline scene; negative = decline
    Column("SurfaceTempC", Float, nullable=True),    # land surface, not air or leaf
    Column("ElevationM", Float, nullable=True),      # surface model: includes canopy
    Column("ObservedOn", Date, nullable=True),
    Column("BaselineOn", Date, nullable=True),
    Column("ThermalOn", Date, nullable=True),
    Column("UpdatedAt", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("IX_SatelliteCellValues_FarmId_ZoneId", "FarmId", "ZoneId"),
)

# Whether a farm has usable data and what it was built from. SceneId is load-bearing: the
# worker compares it against the catalog to decide whether a download is needed at all.
farm_status = Table(
    "SatelliteFarmStatus", metadata,
    Column("FarmId", String(64), primary_key=True),
    Column("Status", String(16), nullable=False),    # ready | preparing | failed
    Column("SceneId", String(200), nullable=True),
    Column("ObservedOn", Date, nullable=True),
    Column("CellCount", Integer, nullable=False, default=0),
    Column("LastRunAt", DateTime(timezone=True), nullable=True),
    Column("LastSuccessAt", DateTime(timezone=True), nullable=True),
    Column("DurationMs", BigInteger, nullable=True),
    Column("Message", Text, nullable=True),
)

# Written by the .NET API, read by this worker - the only traffic that is not
# worker-to-database.
job_queue = Table(
    "SatelliteJobQueue", metadata,
    Column("Id", BigInteger, primary_key=True, autoincrement=True),
    Column("FarmId", String(64), nullable=False),
    Column("Reason", String(32), nullable=False),    # new_farm | boundary_changed | manual
    Column("CreatedAt", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("TakenAt", DateTime(timezone=True), nullable=True),
    Column("DoneAt", DateTime(timezone=True), nullable=True),
    Column("Error", Text, nullable=True),
)


# A whole earth-observation dataset for one farm, gzipped. The WSB page computes its layers
# in the browser, so it needs each dataset in full - the per-cell table is not enough.
# Stored compressed and served to the browser still compressed, so neither this worker nor
# the API ever decompresses it.
datasets = Table(
    "SatelliteDatasets", metadata,
    Column("FarmId", String(64), primary_key=True),
    Column("Dataset", String(32), primary_key=True),
    Column("Payload", LargeBinary, nullable=True),
    Column("PayloadBytes", Integer, nullable=False, default=0),      # uncompressed
    Column("CompressedBytes", Integer, nullable=False, default=0),
    Column("FootprintHash", String(24), nullable=True),              # boundary edit = new hash
    Column("SourceRef", String(200), nullable=True),                 # scene id or season window
    Column("RetrievedAt", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("ExpiresAt", DateTime(timezone=True), nullable=True),
    Column("Status", String(16), nullable=False, default="available"),
    Column("Message", Text, nullable=True),
)

# The grower's journal. Written by the .NET API, read here so engines can take recorded
# evidence into account.
field_records = Table(
    "SatelliteFieldRecords", metadata,
    Column("Id", String(36), primary_key=True),
    Column("FarmId", String(64), nullable=False),
    Column("Kind", String(16), nullable=False),
    Column("ZoneId", String(64), nullable=False),
    Column("Topic", String(24), nullable=True),
    Column("Payload", Text, nullable=False),
    Column("ObservedOn", Date, nullable=True),
    Column("NextCheck", Date, nullable=True),
    Column("CreatedAt", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("RecordedBy", String(64), nullable=True),
)
