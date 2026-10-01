# AgriCentral · satellite worker

Fills three tables with per-cell satellite values. The .NET API reads those tables with
plain SQL. **The two services never call each other** — the database is the whole interface.

```
   ┌──────────────────────── AgriCentral Postgres ────────────────────────┐
   │                                                                      │
   │   "MapandCoordinates"  ──read──┐           satellite.cell_values     │
   │   "Coordinate"         ──read──┤                 ▲         │         │
   │                                │                 │write    │read     │
   └────────────────────────────────┼─────────────────┼─────────┼─────────┘
                                    │                 │         │
                            ┌───────▼─────────────────┴──┐  ┌───▼────────┐
                            │  this worker (Docker)      │  │  .NET API  │
                            │  every 6h + a 30s queue    │  │            │
                            └───────────┬────────────────┘  └────────────┘
                                        │
                                        ▼
                         Microsoft Planetary Computer
                         (Sentinel-2 · Landsat · Copernicus)
```

## What it produces

One row per 20 m cell inside each mapped farm boundary:

| Column | Source | Resolution | What it is |
|---|---|---|---|
| `Ndvi` | Sentinel-2 | 20 m | leaf cover. **Mixed canopy** — coffee *and* shade trees |
| `Ndmi` | Sentinel-2 | 20 m | canopy moisture index. Not soil water |
| `NdviChange` | Sentinel-2 | 20 m | vs an earlier clear scene. Negative = decline |
| `SurfaceTempC` | Landsat | 100 m | land surface at overpass. Not air, not leaf |
| `ElevationM` | Copernicus | 30 m | **surface** model — includes tree canopy |

The 20 m Sentinel grid defines the rows; the coarser datasets are sampled onto it, so about
25 rows share one Landsat temperature. Nothing is interpolated to look finer than the
instrument that measured it.

**`NULL` means "not measured"** — cloud, no overpass, or outside the valid mask. It never
means zero. Queries that average these columns should ignore NULLs, not coalesce them.

## Setup

```sh
python -m venv .venv
.venv\Scripts\activate          # or: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # then set DATABASE_URL
```

**The tables are created by Entity Framework in the .NET project**, not here — this service
never issues DDL. In `agricentral-api`:

```sh
dotnet ef migrations add AddSatelliteTables
dotnet ef database update
```

Two connections, because AgriCentral keeps these in two databases on the same host:

| Variable | Database | Used for |
|---|---|---|
| `DATABASE_URL` | `farmfuture` (DefaultConnection) | the three `Satellite*` tables — read/write |
| `GEOMETRY_DATABASE_URL` | `farmfuture_backoffice` (BackofficeConnection) | `"MapandCoordinates"` — **read only** |

The tables go in the DefaultConnection database because `ApplicationDbContext` is the only
context with EF migration history. `BackofficeDbContext` is database-first with no
migrations, so putting them there would mean baselining it first — avoidable risk on a
production database. Nothing is joined across the two: the worker reads geometry, does its
own maths, and writes values.

## Running

```sh
python run.py                        # the worker: both loops. This is what the container does
python run.py --once                 # one sweep of every farm, then exit
python run.py --farm <uuid>          # one farm, then exit — prints cells, time, peak memory
python run.py --farm <uuid> --force  # ignore the stored scene id and re-fetch
```

### Do this before trusting it in production

```sh
python run.py --farm <your-largest-farm-uuid>
```

Peak memory and runtime scale with farm **area**, not farm count — one large estate costs
more than a hundred smallholdings. This code has only ever been exercised on a 7.7 ha farm.
Measure your biggest three before sizing the container or enabling the sweep.

## The two loops

| Loop | Interval | Does |
|---|---|---|
| queue | 30 s | farms the .NET API asked for — a new farm or a changed boundary |
| sweep | 6 h | every mapped farm in turn |

The sweep checks the catalog first and **skips the download when the newest scene is the
one already stored** (`farm_status.scene_id`). Sentinel-2 revisits in about five days, so
most sweeps download nothing and cost a few seconds.

## Tests

```sh
python -m pytest
```

Ten offline tests covering the grid sampling and merge — no network, no database.

## Deployment notes

**Exactly one replica.** Two would run two sweeps on the same schedule, download the same
imagery twice and race on the same rows. This process is the single writer of the
`satellite` schema and the design depends on that.

**No port, no health check.** Nothing connects to this service, so there is no endpoint a
probe could call — and a probe that went unanswered while numpy was busy would restart the
container mid-work. Monitor `satellite.farm_status.last_run_at` instead: if it stops
moving, the worker is stuck. Pair with `restart: unless-stopped`.

**Outbound HTTPS to `planetarycomputer.microsoft.com` is required.** On a locked-down
network that is a firewall rule, and it is the first thing to check when every farm reports
`failed`.

## What the .NET side needs

Two things, both plain SQL:

```csharp
// read: is this farm ready?
var status = await _db.SatelliteFarmStatus
    .FirstOrDefaultAsync(s => s.FarmId == farmId);

// read: its values
var cells = await _db.SatelliteCellValues
    .Where(c => c.FarmId == farmId && (zone == null || c.ZoneId == zone))
    .ToListAsync();

// write: ask for a farm to be processed now (on farm create / boundary change)
_db.SatelliteJobQueue.Add(new SatelliteJobQueue {
    FarmId = farmId, Reason = "boundary_changed" });
await _db.SaveChangesAsync();
```

A farm whose `SatelliteFarmStatus` row is missing or whose `Status` is not `"ready"` has no usable data yet — return
a *preparing* state to the app rather than an error. That is a normal condition for a newly
mapped farm, not a fault.

## Honest limits

- `ndvi` cannot distinguish dense coffee from dense shade trees overhead, and is **not** a
  measure of shade cover. Treat a grower's measured shade reading as a separate input.
- `elevation_m` includes tree canopy. Under shade trees it is treetops, 10–15 m above where
  water actually flows. Drainage work needs a ground survey.
- `ndvi_change` is a reason to inspect, never a cause. Pruning and seasonal change confound
  it, and a decline identifies no pest or disease.
- A single source: all four datasets come from Microsoft Planetary Computer, anonymously.
  An outage there stops refreshes — stored values keep serving, dated.

## Phase 2, if wanted

This deliberately produces **numbers only**. The interpretation layer from the Coffee Estate
Twin — nine pest/disease exposure maps, scout-stop selection, the sun model, the fertilizer
planner — is not here. It can be added later by copying `app/engines/` from that project;
these three tables, this container and the .NET endpoints all stay as they are.
