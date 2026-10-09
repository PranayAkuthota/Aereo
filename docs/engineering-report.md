# Engineering Design

This service ingests bounded KML or zipped Shapefile datasets, calculates
horizontal metric measurements, and persists per-feature results. The HTTP
contract and examples are in the [README](../README.md); this document explains
implementation choices and their limits.

## Architecture and Data Flow

`main.py` owns routes, startup, error envelopes and upload admission. `uploads.py`
owns multipart parsing and spool cleanup. `service.py` coordinates parsing,
measurement, result-size accounting, metadata counters and persistence.

1. Admit the upload before parsing multipart content; reject busy capacity with 503.
2. Apply body/file limits and dispatch by supported filename extension.
3. Parse source features and CRS, rejecting structural corruption.
4. Validate geometry and produce a typed measurement or explicit feature outcome.
5. Enforce the aggregate serialized-result budget and model invariants.
6. Insert metadata and all results in one transaction, then return 201/Location.
7. Retrieve persisted metadata or an ordered page without reparsing the upload.

Blocking parsing, measurement and writes run in a worker thread. Raw uploads are
not retained. A structural input failure rejects the dataset; a feature-level
geometry error can coexist with measured features in a completed resource.

## Format Readers

KML uses defusedxml incremental parsing, qualified KML 2.2 geometry tags and
bounded coordinate tokens. Start events enforce depth, element and feature limits;
processed nodes are removed to avoid retaining the entire XML tree. Properties
include name, description, ExtendedData and SimpleData. Homogeneous nested
MultiGeometry becomes a corresponding multipart geometry; mixed collections and
unsupported extensions receive explicit outcomes. Unsupported XML encoding
failures are caught at the reader boundary, without masking feature-processing
errors.

ZIP members stay in memory and are never extracted. One matching SHP/SHX/DBF/PRJ
dataset is required; CPG is optional. Member names, paths, types, counts, declared
expansion and compression ratios are checked. Selected components undergo CRC
and compressed end-marker validation. Invalid filename encodings are classified
as malformed archives.

Shapefile preflight validates component headers, SHX mappings, SHP record identity,
lengths and coordinate counts before PyShp allocation. Multipart starts must begin
at zero, be non-negative and strictly increasing, and lie below the coordinate
count. These constraints partition all coordinates into non-empty ranges, with
the last range ending at the coordinate count; malformed indices are rejected,
not repaired. Geometry and DBF attributes are read by the same original ordinal.
Deleted rows are omitted without shifting associations, and surviving indices
may have gaps.

## CRS Strategy and Measurement

KML has EPSG:4326 source coordinates. Shapefile PRJ WKT is required and parsed by
pyproj. Supported sources are horizontal geographic or projected CRSs.
`geographic_geometry` checks finite XY and a valid WGS84 location.

For measurable lines and polygons, `project_for_measurement` checks geographic
extent, selects a centroid-based UTM zone/hemisphere or wholly polar UPS, and
transforms the original geometry directly into that metric grid. Transformations
use `always_xy=True`, a bounded cache and `allow_ballpark=False`; PROJ network
downloads are not enabled. Unavailable usable transformations yield feature errors.

`measure` normalizes geometry to XY, validates finite coordinates and topology,
and uses Shapely for projected area or length. Polygon holes are subtracted;
multipart values are summed. Points validate location but have null measurements
and no measurement CRS. Invalid polygons are not silently repaired. Source
geometry and source type remain distinguishable in the response.

The 6° extent guardrail and UTM/UPS boundary rejection constrain local measurement;
they do not guarantee survey accuracy. Results are grid values, with projection
distortion. Source area-of-use, optimal datum-grid availability, Norway/Svalbard
UTM exceptions and edge densification are not implemented.

## Persistence and Request Lifetime

SQLite keeps deployment self-contained. `files` stores metadata JSON; `features`
stores typed result JSON with `(file_id, feature_index)` as its primary key and a
foreign key to the file. Parameterized SQL, short-lived connections, WAL and a
10-second busy timeout support modest local concurrency.

File and feature inserts share one transaction. Exceptions roll back the write
and connections close in `finally`. Pydantic checks finite/non-negative values,
paired units, outcome consistency and metadata counters before storage; stored
JSON is validated again when read. Pagination checks total before binding offsets,
so an arbitrarily large offset safely returns an empty page.

Only committed resources expose `COMPLETED`; there are no persisted pending or
failed jobs. A cancelled caller cannot stop an already running thread. Its upload
slot stays held until the worker finishes, and the worker may commit a complete
resource after the client disconnects. There is no idempotency/recovery endpoint,
so a retry can duplicate work. Transaction atomicity does not guarantee delivery
to the client or tested power-loss durability.

## Input Safety and Resource Controls

Defaults are a 10 MiB file, 50 MiB declared ZIP expansion, 10,000 features,
100,000 coordinates, 200,000 XML elements, 32 MiB serialized feature results and
two admitted uploads per process. Fixed format bounds and environment variables
are listed in the README. Results are checked incrementally before persistence.

Archive checks reject traversal, absolute paths, symlinks, encryption and duplicate
names. XML blocks DTDs/entities and does not fetch referenced resources. Multipart
handles are explicitly closed, including unfinished parts after rejection,
disconnect or cancellation. Controlled input errors use the common error envelope;
unexpected failures return a generic response while diagnostics stay in logs.

These controls are not a complete denial-of-service or security audit. Parsing
and results remain in memory; DBF field expansion has no separate count limit.
Ancillary archive members are path/size checked but not decompressed/CRC-checked.
The spool-ownership adapter depends on Starlette 0.52.1 private internals; compressed
end-marker checks depend on CPython decoder internals. Both require regression
validation on upgrades.

## Testing Strategy

The suite contains **151 tests**, using synthetic inputs and temporary databases:

| Coverage | What it establishes |
| --- | --- |
| API and formats | Uploads, metadata, geometry/properties, outcomes and pagination |
| Numerical fixtures | Known metric dimensions, holes, multipart sums and independent Geod comparisons |
| CRS cases | Geographic/projected sources, axis order, hemispheres, polar regions and extent boundaries |
| Malformed inputs | ZIP paths/codecs/CRC, XML complexity/encoding and Shapefile structure |
| Fifteen parser regressions | Invalid part starts and filename/XML encodings return controlled errors without writes; valid inputs retain geometry and measurements |
| Request lifetime | Disk-spool cleanup, repeated cancellation and capacity ownership |
| Transactions and restart | Interrupted writes leave both tables empty; full metadata/results survive restart |

Ruff, formatting, mypy and dependency consistency are separate checks. CI runs
these checks, pytest and a Docker build. Live container validation covers health,
Swagger/OpenAPI, deterministic KML/Shapefile flows, malformed requests and full
result persistence after recreation with the volume retained. Selected numerical
fixtures use tolerances; these are evidence for those cases, not universal
accuracy promises. `pip check` establishes dependency consistency, not absence
of package vulnerabilities.

## Runtime and Dependencies

Python 3.12 and exact runtime/development dependency locks reproduce the tested
environment. FastAPI/Pydantic, PyShp, Shapely, pyproj and defusedxml provide the
required format and measurement operations without GDAL on the host.

Docker uses a digest-pinned Python base and a non-root user. Compose binds to
loopback, uses one worker, a named database volume, 512 MiB memory and one CPU.
Private-CA networks can supply a trusted bundle during installation:

```bash
docker build --secret id=pip_ca,src=/path/to/ca-bundle.pem -t aereo-measurements .
```

The BuildKit secret is mounted only for installation and is not copied into the
image. Ordinary local builds do not need it. TLS verification remains enabled.

## Tradeoffs and Future Work

The format subset and synchronous contract keep the assignment understandable
and self-contained. SQLite suits local evaluation; distributed admission,
authentication, retention, deduplication and durable background processing are
not implemented. KML styling/KMZ, mixed collections, Shapefile MultiPatch and 3D
measurement are outside the supported measurement envelope.

Future work should follow actual usage: densification or geodesic measurement
for broader extents, idempotent client recovery and retention policies, or an
external database and durable workers for sustained larger workloads. None is
required to run the current API.
