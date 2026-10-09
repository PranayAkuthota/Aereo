# Walkthrough and interview preparation

Run the quick start and `python scripts/demo.py` first. Follow one request in the
code before memorizing answers. All examples are synthetic. AI assisted the work;
be ready to explain and modify it yourself rather than claiming unaided authorship.

## Architecture and important functions

| Module / function | Responsibility |
| --- | --- |
| `main.create_app` | Routes, error envelopes, lifespan, per-process admission |
| `UploadLimitMiddleware` | Bounds all POST body frames, including chunked input |
| `uploads.uploaded_file` / `UploadParser` | Exactly one file; owns and closes all multipart spools |
| `main.wait_for_processing` | Shields running worker; retains slot after repeated cancellation |
| `service.process_upload` | Format dispatch, measurement/result budget, counters and persistence |
| `parser.KMLReader` | Safe incremental XML, qualified geometry, attributes, coordinates |
| `parser.archive_components` / `read_component` | ZIP paths, size, codec/CRC/termination validation |
| `parser.preflight_shapefile` / `read_shapefile` | Component identity/counts, ordinal pairing, source type |
| `crs.geographic_geometry` | Valid horizontal CRS and finite WGS84 location |
| `crs.project_for_measurement` | Extent guardrail, UTM/UPS selection, source-to-target transform |
| `measurements.measure` | XY normalization, validity, typed outcome and metric value |
| `Repository` | Short-lived SQLite connections and one atomic upload transaction |
| `schemas` | Finite/non-negative values, units, status/error/count consistency |

POST `/api/files/` returns 201 plus a Location header only after persistence.
GET that location returns metadata. GET its `measurements/` suffix returns an
ordered page, default 100/max 1000. SQLite stores file metadata and feature JSON;
`(file_id, feature_index)` is the indexed primary key with a foreign key. Geometry
is source-CRS XY; it is not necessarily RFC7946 WGS84 GeoJSON.

## Follow an upload

1. Admission happens before multipart parsing; busy capacity returns 503. Every
   multipart handle belongs to the parser, even unfinished parts.
2. HTTP body/file limits apply. The service treats the bounded basename as
   metadata, selects KML or ZIP, and hands format checks to the parser.
3. KML start events enforce depth/feature/element limits; processed nodes are
   removed. Geometry uses qualified namespaces; coordinate tokens are lazy.
4. ZIP members are not extracted. Matching Shapefile components are validated
   before PyShp allocation. Shape and DBF record use the same original ordinal;
   deleted attributes omit that feature without shifting other associations.
5. Normalize to XY and validate finite coordinates/topology. Points only validate
   geographic location. Lines/polygons select a local metric CRS and measure.
6. Result-byte budget and Pydantic invariants are checked. Metadata and every
   feature are inserted in one transaction; failures roll back the whole upload.
7. A cancelled caller cannot kill an already running thread. It may commit one
   complete resource; keep the capacity slot until it finishes. There is no
   persisted PENDING/PROCESSING/FAILED job state in this synchronous contract.

## Twenty likely questions and concise answers

1. **Why FastAPI?** Typed requests/responses, generated OpenAPI, and a small HTTP
   layer. CPU-heavy work goes to a worker thread instead of the event loop.
2. **Why not GeoPandas/GDAL?** This is bounded format ingestion and geometry
   measurement, not dataframe analysis. PyShp and explicit KML avoid a larger
   driver stack; they also make supported format rules reviewable.
3. **Why SQLite?** Durable keyed retrieval and transactions without an external
   service. Multiple API instances or sustained writes would justify PostgreSQL.
4. **What does COMPLETED mean?** All features were considered and the resource
   committed, including individual ERROR/UNSUPPORTED outcomes. Inspect counters.
5. **What happens on cancellation?** Parsing cancellation closes spools. Once a
   worker runs, it finishes atomically and retains its slot. A client may lose its
   returned ID; a retry can duplicate work because idempotency is not implemented.
6. **How do you prevent partial writes?** File and feature inserts share one
   transaction; the repository rolls back on BaseException and closes connections.
   Tests interrupt a later insert and verify both tables remain empty.
7. **Why not measure geographic `.area` directly?** Shapely is planar: degrees
   give square degrees. Transform to a local metric grid before area or length.
8. **How do you choose UTM?** Transform to WGS84, check extent, use centroid
   longitude: `floor((longitude+180)/6)+1`, clamped 1–60; hemisphere selects 326xx/327xx.
   Wholly polar features use UPS; boundary-crossing features are rejected.
9. **Why `always_xy=True`?** Keep longitude/easting first regardless of authority
   axis order. Otherwise valid-looking coordinates can describe another location.
10. **Why not Web Mercator?** Metre units do not guarantee ground accuracy;
    distortion grows with latitude. A 60° test compares against independent Geod.
11. **What accuracy is guaranteed?** No survey-grade guarantee. Results are UTM/UPS
    grid values; tests verify selected fixtures with tolerances. Six-degree extent
    limits, disabled ballpark operations and finite coordinates are guardrails.
12. **What about datum grids and area-of-use?** Missing usable transformations
    yield feature errors. Best grids are not guaranteed; source CRS area-of-use
    is not separately enforced. PROJ network downloads are not enabled.
13. **How are holes/multipart features measured?** Shapely subtracts holes and sums
    parts after projection. Nested homogeneous KML is flattened; mixed collections
    remain unsupported rather than being measured inconsistently.
14. **What about Z/M?** Output is XY and measurements horizontal; Z/M are discarded.
    Missing Z does not invalidate XY. KML optional altitude must be finite syntax.
15. **What was the deleted-DBF defect?** PyShp filters deleted DBF rows; zipping
    records with unfiltered shapes shifts associations. Reading both by ordinal
    avoids silent geometry/property mismatches; surviving indices can have gaps.
16. **How do you handle malicious uploads?** Body/file/XML/coordinate/feature/result
    limits, bounded admission, safe XML, ZIP path/ratio/CRC/codec checks, no extraction.
    These are local safeguards, not a complete enterprise DoS/security system.
17. **How are API errors represented?** Consistent `error.code/message`, controlled
    4xx for bad input, 503 for capacity, generic 500 for unexpected failures. Logs
    retain diagnostics; responses do not contain internal tracebacks.
18. **How does pagination work?** Indexed ordered LIMIT/OFFSET after checking total.
    Offsets beyond total return[] before SQLite integer binding; limit 1–1000.
    Offset is a page position, not a source ordinal when deleted rows leave gaps.
19. **What performance decisions matter?** Bounded transformer cache, no repeated
    parsing on retrieval, indexed queries, short-lived connections, incremental
    XML clearing and per-process admission. Results remain bounded in memory;
    serialization for budget checks adds work but makes output limits explicit.
20. **How would you extend it?** Start from measured need: densification/geodesic
    mode for broader extents, idempotency/retention for client recovery, or durable
    workers/PostgreSQL for larger workloads. None is implemented here.

## Testing strategy: which defects do the tests catch?

- Exact metric square/hole/line fixtures catch wrong units, missed holes and parts.
  Independent Geod comparisons catch axis, projection and feet/Mercator errors.
- Deleted first/middle/multiple DBF fixtures verify identity, geometry, properties
  and length, catching success responses that silently associate the wrong row.
- Codec/CRC/truncation fixtures catch uncaught decompressors and accepted streams
  without a final end marker. SHX identity tests catch duplicate geometry mapping.
- Multi-frame ASGI disk-spool tests catch resource leaks before FormData exists.
  Repeated cancellation/capacity tests catch premature slot release; transaction
  interruption/failure tests catch partial metadata/results.
- Namespace, MultiGeometry and XY/XYZ fixtures catch foreign-tag interpretation,
  incorrect mixed-type classification and absent-Z rejection.
- Result-byte exact/UTF-8/aggregate boundaries catch character-vs-byte accounting
  and per-feature-only budgets. Page boundaries catch out-of-contract limits.
- Restart compares complete metadata/results; live Docker recreation separately
  checks the named volume, not just a still-running process.

## Exercises and trade-offs

Run the complete checks. Then change a sample endpoint and predict its new length;
upload a self-intersecting polygon and find its feature ERROR; lower feature or
result limits and explain the 413; read the deleted-record test and explain index
gaps. Sketch an idempotency design that survives disconnect without inventing an
implemented endpoint. Explain why a thread is not safely killable and why a
transaction is necessary but does not give clients delivery guarantees.

Supported KML is a subset; KMZ/3D/mixed collections are limited. No densification,
certified accuracy, authentication, retention API or distributed admission exists.
Private Starlette/CPython adapters must be retested on upgrades. These trade-offs
are explicit in README and the engineering report; do not claim more in interview.
