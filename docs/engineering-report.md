# Engineering report

Validation date: 9 October 2026. This report describes the local implementation
and observed checks. GitHub publication of the hardened implementation was
authorized after review. The intended public repository is
https://github.com/PranayAkuthota/Aereo on `main`; the release commit and remote
verification are reported separately. The hiring-form submission remains manual.

## A. Changes made

The existing FastAPI/SQLite/PyShp/Shapely/pyproj architecture remains. Fixes address
record identity, malformed input, upload lifetime and bounded processing:

- Read Shapefile geometry and DBF attributes by the same original ordinal. Deleted
  DBF rows are omitted; surviving IDs/indices retain their ordinals and can have
  gaps. An all-deleted dataset is an input error. Validate SHP/SHX record identity,
  offsets, lengths and counts before parsing; reject duplicate index mappings.
- Catch specific ZIP decompression exceptions and verify selected components'
  CRC and decoder termination. Truncated DEFLATE with a still-valid decoded CRC
  is rejected too. Valid stored/DEFLATE/BZIP2/LZMA archives are covered.
- Own multipart spools explicitly, including unfinished parts. Close them in
  `finally` on success, input error, size rejection, malformed multipart,
  disconnect and task cancellation. Preserve Swagger's binary file upload schema.
- Return empty pages for offsets beyond total before passing integers to SQLite.
- Validate Point/MultiPoint without selecting a local measurement CRS. Valid
  separated points return NOT_APPLICABLE with null measurement/target CRS.
- Parse XML incrementally with start-event feature/element/depth limits, lazy
  coordinate tokens and removal of processed nodes. Add per-process admission
  before multipart parsing; retain capacity until cancelled worker threads finish.
- Enforce qualified geometry namespaces; flatten nested homogeneous MultiGeometry.
- Normalize output and measurement geometry to XY; discard Z/M. Preserve original
  Shapefile source type independently of the normalized geometry or conversion.
- Validate finite non-negative measurements, units, outcomes and counters. Replace
  weak numerical assertions with independent references and compare full persisted
  metadata/results after restart.
- Align Ruff/mypy/Docker with tested Python 3.12 and pin the base-image digest.

## B. Files changed

Modified relative to the preserved pre-fix snapshot:
`.env.example`, `Dockerfile`, `README.md`, `compose.yaml`, `pyproject.toml`,
`app/config.py`, `app/main.py`, `app/schemas.py`,
`app/geospatial/{parser,crs,measurements}.py`,
`tests/test_{api,measurements,parsers,repository}.py`,
`docs/engineering-report.md`, and `docs/interview-guide.md`.

Added: `app/uploads.py`, `tests/test_regressions.py`, `tests/test_schemas.py`,
and `tests/test_upload_cleanup.py`. Runtime dependency locks did not change. The hardening additions below extend
this file list.
During the pre-publication review the repository was on an unborn branch with
untracked project files, so a
normal `git diff` alone cannot represent these changes. Review used a preserved
pre-fix snapshot and a baseline-relative diff, including new-file inspection.

## C. Tests added and strengthened

There are **76 additional test cases**, bringing the original 60 to **136**.
The final hardening pass added 18 of these and strengthened existing tests.
Important regression coverage includes:

- Deleted first/middle/multiple DBF rows: exact ordinal, geometry, attributes and
  metric length, not just successful status. All-deleted input is rejected.
- Corrupt DEFLATE, truncated compressed members, CRC failure and missing final
  decoder marker; four supported ZIP codecs; oversized CRS data.
- Actual multi-frame ASGI requests with forced disk spooling for eight exits:
  success, validation, file/body limits, incomplete/malformed body, disconnect,
  cancellation. Every allocated spool is asserted closed without GC.
- Capacity remains held while a cancelled request's real worker completes; busy
  requests receive 503 and subsequent requests can proceed after release.
- Ordinary and extreme offsets, namespace errors, nested homogeneous geometry,
  separated/mixed-dimensional MultiPoints, XYZ Shapefile normalization, original
  unsupported source type, XML/coordinate limits and response-model invariants.
- Independent Geod comparisons with declared tolerances and exact UTM dimensions;
  full metadata and measurement-page equality after application restart.

## D. Full suite result

Final `pytest -q`: **136 passed**, zero failures, skips or expected failures.
One upstream Starlette/AnyIO TestClient deprecation warning remains. Tests use
synthetic fixtures and temporary databases; they are not an exhaustive GIS corpus.

## E. Lint, type, format and dependency result

- `ruff check app tests scripts`: passed.
- `ruff format --check app tests scripts`: all 22 files formatted.
- `mypy app scripts`: passed for all 13 application/demo modules.
- `python -m pip check`: no broken requirements.

Python 3.12 is the validated runtime; broader `requires-python >=3.12` metadata
is not evidence that every later Python release has been tested. Exact package
versions and an image digest improve reproducibility but are not artifact-hash
verification or a full supply-chain guarantee. The CI definition is present;
GitHub-hosted execution has not been observed.

## F. Docker validation

Built with **`--no-cache --pull`** using the pinned Python 3.12 slim digest.
This cloud machine required its existing proxy host mapping and trusted CA bundle
passed as the optional BuildKit `pip_ca` secret; TLS verification stayed enabled.
No credential or CA bundle was embedded in the project/image.

A fresh isolated Compose project passed health readiness, ran as UID 10001, and
served Swagger/OpenAPI with the required file-upload schema. Real HTTP checks:

| Check | Observed result |
| --- | --- |
| KML sample upload/retrieval | 201/200; two measured features, one unmeasured point |
| Projected Shapefile sample | 201/200; area exactly 10,000m² |
| Geographic sample values | Area 11,804.99384m²; line 106.57758m |
| Deleted DBF first/multiple rows | Correct surviving ordinals 1 and 4, attributes, coordinates and lengths |
| Offset 10^100 | 200, empty feature page |
| Widely separated MultiPoint | NOT_APPLICABLE; no measurement CRS |
| Four ZIP corruption cases | 400, INVALID_ZIP |
| Malformed KML | Controlled 400 |
| Container removal/recreation | Exact metadata and complete pages preserved for all four uploaded datasets |

Recreation used `compose down` followed by `up`, retaining the named volume, not
just a process restart. Afterwards only this validation project's containers and
volume were removed. No prior application database was deleted.

## G. Assignment compliance

| Requirement | Implementation/evidence | Status |
| --- | --- | --- |
| FastAPI or Django | FastAPI, Pydantic/OpenAPI | Met locally |
| KML or zipped Shapefile | Both formats, parser/API/live-container checks | Met locally |
| Feature ID/index/type/geometry | Original ordinals, source type, normalized XY geometry | Met locally |
| CRS/properties | KML WGS84, required Shapefile PRJ, ExtendedData/DBF | Met locally |
| Polygon area | Polygon/MultiPolygon, holes, metric projected area | Met locally |
| Line length | LineString/MultiLineString, metric projected length | Met locally |
| Point null measurement | Point/MultiPoint, validated without local projection | Met locally |
| Geographic-to-projected measurement | Per-feature local UTM/UPS, fixed XY axis order | Met locally |
| Upload endpoint | POST /api/files/, synchronous 201/Location | Met locally |
| File metadata endpoint | GET /api/files/{id}/, SQLite persistence | Met locally |
| Measurement endpoint | GET /api/files/{id}/measurements/, ordered pagination | Met locally |
| README setup/API/architecture/CRS/decisions | README and walkthrough | Present |
| Learning/future improvements | README, without fabricated unaided authorship | Present |
| Public GitHub submission URL | https://github.com/PranayAkuthota/Aereo | Public repository verified; release commit checked separately |

## H. Follow-up adversarial review and remaining risks

Re-ran the original audit probes and inspected the implementation and diff.
Deleted-record association, extreme offsets, DEFLATE errors, separated MultiPoint,
foreign geometry namespaces, nested homogeneous lines and mixed XY/XYZ points
now behave as specified. The follow-up found and fixed duplicated SHX mapping and
missing DEFLATE termination acceptance, with regressions. A deterministic
100-case byte-mutation probe of SHP/SHX/DBF components yielded 49 controlled 400s
and 51 accepted datasets, with no 5xx; this is limited probing, not exhaustive fuzzing.

The original irrelevant-node XML probe (500,000 elements, about 2MB) was rerun in
an isolated process. It now rejects at 200,000 elements: measured additional peak
RSS was about 3.88MiB versus the earlier 81.43MiB full-tree parser. This is a
specific benchmark, not a bound on every XML input or total process memory.

No new blocking defect was found in the reviewed assignment scope. Remaining
risks are explicit:

- Only a KML subset and one Shapefile dataset are supported. Mixed collections,
  KMZ, some extensions and 3D measurements are unsupported. DBF encoding defaults
  to UTF-8 unless CPG specifies it. Z/M are discarded.
- UTM/UPS results are horizontal grid measurements, not survey-certified ground
  values. The 6° guardrail does not guarantee accuracy. Source CRS area-of-use is
  not independently enforced, optimal datum grids are not guaranteed, and edges
  are not densified. One polar audit probe differed from Geod by about 0.59%.
- Default limits: file 10MiB, body file+64KiB, expanded ZIP 50MiB, 100 entries,
  200:1 ratio, 10,000 features, 100,000 coordinates, 200,000 XML elements, depth 64,
  coordinate token 128 chars, PRJ 64 KiB, CPG 128 bytes, two admitted uploads/process,
  32 MiB total serialized UTF-8 feature results, and 1,000 features/page.
  These are input/concurrency limits, not a complete DoS defense or exact RSS bound.
- A cancelled/disconnected client's worker can still persist results. There is no
  idempotency key or recovery/list endpoint; a retry can create a duplicate.
- Multipart spool ownership and compressed EOF checks use pinned private
  Starlette/CPython APIs. Dependency/runtime upgrades require the regression suite.
- Ancillary ZIP members are path/size checked but not decompressed/CRC-validated.
- SQLite and a single worker suit local evaluation. No authentication, retention,
  distributed admission, hard worker deadline or high-traffic guarantee is claimed.
- Source/sample/documentation files were inspected for common credential patterns;
  no secrets were found. This is a bounded inspection, not a formal security audit.

## I. Updated assessment

**90/100 for this assignment**, up from the original audit's 65/100 and the pre-hardening 88/100. This is an
engineering judgment, not the employer's score or a selection guarantee. The
critical silent-association defect is fixed; result limits, cancellation/transaction
evidence and evaluator documentation improved further. The remaining points reflect constrained GIS support, accuracy and
operational limits, private API compatibility, and unobserved hosted CI.

## J. Publishing recommendation

The applicant approved publication of the hardened implementation after review.
The release uses `main` in the public repository above. Do not describe the
service as production ready; hiring-form submission remains the applicant's task.

Local setup/startup instructions remain saved in the cloud environment draft.
Saving a draft does not publish it or prove restoration in a fresh task.

## Final hardening pass

Hardening files: modified `.env.example`, `.gitignore`, `.github/workflows/checks.yml`,
`README.md`, `app/config.py`, `app/main.py`, `app/repository.py`, `app/service.py`,
`compose.yaml`, both engineering/interview documents, `tests/test_measurements.py`
and `tests/test_upload_cleanup.py`. Added four `examples/` fixtures,
`scripts/demo.py` and `tests/test_hardening.py`.

The architecture remains unchanged. Source changes add a 32 MiB serialized-feature
result budget, preserve valid extensions when truncating long filename metadata,
and deserialize stored result JSON directly through Pydantic. `.gitignore` now
excludes environment variants, private keys/certificates, databases, temporary
logs and IDE artifacts while retaining the non-secret `.env.example`.

`examples/` adds separate synthetic polygon, line, point and Shapefile inputs;
`scripts/demo.py` exercises upload, metadata and measurement retrieval using only
the standard library. Sample ZIPs are intentional input fixtures. No generated
submission archive belongs in the repository. README now leads with the
architecture diagram, quick start, Swagger and actual API examples. The interview
guide maps the important functions and provides 20 concise questions/answers,
with specific regression-to-defect explanations and honest limitations.

New tests demonstrate exact result-budget boundaries, UTF-8 byte accounting and
aggregate overflow without persistence; cancellation inside an insert transaction
rolls back both tables; an HTTP worker write failure returns a generic error and
no partial file. The existing cancellation test now covers repeated cancellation
and verifies no resource before worker release and exactly one complete resource
afterwards. Other additions exercise page-size boundaries, independent numerical
references for southern/polar/zone-boundary lines, UTM/UPS boundary rejection,
checked-in demo values and long-filename dispatch.

No PENDING/PROCESSING/FAILED job records exist in this synchronous API. Only a
fully committed resource exposes COMPLETED, which may include feature-level errors.
Rejected inputs and interrupted transactions expose no partial resource. A client
disconnect after dispatch may still commit a full result without delivering its
ID. Atomicity does not imply client-delivery or power-loss durability guarantees.

The existing CRS strategy was retained after numerical review. New tolerances
are 0.2% for the selected southern/zone-boundary fixtures and 0.7% for selected
near-pole fixtures, compared to independent ellipsoidal Geod references. These
are test tolerances for those cases, not a universal service accuracy promise.
Source area-of-use enforcement, optimal datum-grid selection, densification and
idempotent recovery remain limitations. No unrelated infrastructure was added.

The final clean Docker build used `--no-cache --pull` after all runtime changes.
The four-example standard-library demo completed against the fresh container.
Live adversarial HTTP checks covered malformed KML, traversal ZIP, corrupt
compression/CRC/termination, missing/invalid CRS, deleted records with exact
association checks, huge offsets, unsupported/invalid geometries, separated
MultiPoint, oversized input and excessive feature count. Eight simultaneous
10,000-point uploads produced two complete 201 resources and six controlled 503
capacity responses. Disconnect/repeated cancellation and interrupted writes were
verified in the ASGI/transaction tests, rather than claiming a live socket
disconnect deterministically cancels Uvicorn's worker.

The CI definition now includes the demo script in lint/type/format checks and
checks dependency consistency. Hosted CI was not observed during local validation.
Reusable cloud startup/demo instructions were updated in the draft; saving a
draft does not publish the environment. Publication was paused for the final
hardening review and then explicitly authorized for this release.

After the final hardening container was removed and recreated, full metadata and
measurement pages matched exactly for all five checked datasets (the four demo
inputs and the deleted-record regression). UID 10001 and health readiness passed.
Only the validation project's containers/volume were cleaned up afterwards.
Final source review covered 41 project files and sample ZIP contents with common
credential-pattern checks, with no findings; ignore rules were exercised.
The hardening diff consists of 13 modified and 6 added files, reviewed against a
preserved pre-hardening snapshot before the initial release commit. No project
deletion or hiring-form submission was performed. Publication verification is
reported separately from these pre-publication validation checks.
