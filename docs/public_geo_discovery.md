# Public GEO discovery and verified import

The SDK can search public GEO metadata, inspect sample annotations and file formats,
explicitly start an import, and read or wait for its actual status. Sync and async
clients expose the same methods under `client.discovery`.

These interfaces are implemented in source. Package publication and live endpoint
acceptance are separate release steps; neither is implied by the examples below.
The canonical server routes are authenticated and retain server-side access and
ownership checks. An API key does not bypass subscription or legal requirements.

## Search and inspect first

```python
from ligandai import LigandAI

with LigandAI() as client:  # reads LIGANDAI_API_KEY
    search = client.discovery.search_geo_response(
        "human skeletal muscle aging", limit=5,
    )
    print(search.model_dump_json(by_alias=True, exclude_unset=True, indent=2))

    for candidate in search.results:
        inspection = client.discovery.inspect_geo(candidate.accession)
        print(inspection.accession, inspection.source.metadata_sha256)
        print(inspection.metadata_coverage.model_dump(by_alias=True))
        print(inspection.parser.model_dump(by_alias=True))
        print(inspection.warnings)
```

`search_geo(query, limit=10)` remains a convenience method returning a list of
`GeoDataset` objects. `search_geo_response` makes the same single request and
preserves the full envelope, including warnings and provenance when the result
list is empty. Additive metadata fields are retained by the models. Search limits
are integers from 1 through 50. Put organism, tissue, disease or technology terms
in the query when those constraints matter; inspect the returned annotations to
verify that they match.

`inspect_geo(accession)` returns `GeoInspection` with schema
`transcriptomic-dataset-inspection/1`. It includes official metadata provenance,
sample characteristics and normalized annotation fields, sample coverage, file
formats, parser warnings, and a metadata SHA-256. `downloaded` is `false`, parser
status is `inspection_required`, and expression units remain `unknown`.

Inspection retrieves metadata; it does not establish that expression data has
been downloaded, parsed, normalized or analyzed. A supported format still needs
file-content validation. For example, a Matrix Market file requires matched
feature and barcode files, and an archive requires inspection of its contents.
Missing donor or cohort annotations stay missing. Age alone does not establish
diagnosed sarcopenia. The metadata digest identifies the inspected metadata, not
the expression file contents.

## Explicit import and status

After reviewing an accession returned by search and inspection, pass that exact
accession to `import_geo`. It accepts no user ID, local file path or arbitrary
download URL.

```python
# chosen_accession is the accession you reviewed above.
with LigandAI() as client:
    submitted = client.discovery.import_geo(chosen_accession)
    print(submitted.job_id, submitted.status)

    receipt = client.discovery.wait_for_import(
        submitted.job_id,
        accession=submitted.accession,
        timeout=300,
        poll_interval=2,
    )
    print(receipt.model_dump_json(by_alias=True, exclude_unset=True, indent=2))
    if receipt.status == "completed":
        print("Server-reported completion; inspect dataset_id and provenance:", receipt.dataset_id)
    else:
        print("Import did not complete:", receipt.status, receipt.error)
```

`force=True` requests re-import of an already imported accession. Submission does
not imply completion. Empty or malformed responses raise validation errors; the
SDK does not invent a queued job, accession, dataset ID or provenance.

When several published files or archive members could contain the matrix, use
`selection={"fileNames": [...], "parsePlan": {...}}` in either the sync or async
method. `fileNames` must match names returned by inspection. A versioned parse
plan uses archive-relative paths from the server's `selectionInspection` failure
receipt and declares the format, matrix/layer, orientation and supplied unit.
The SDK preserves this JSON object; the server validates its allowed fields and
binds selected files to their actual content hashes. It never infers count units,
donors, age groups, or disease status. Unknown units remain `unknown`.

For a genes-by-observations table, declared `feature_metadata_columns` such as
`Length` remain feature annotations and are excluded from observation columns.
Use this only when supported by the inspected source. Do not treat a metadata
inspection, sample-level mean, or parsed unknown-unit matrix as validated raw
counts or donor-level differential expression.

The CLI reads the same bounded JSON selection without uploading the local path:

```bash
ligandai discovery geo import "$ACCESSION" --selection selection.json
```

Selection files must contain a finite JSON object of at most one MiB. Errors in
the local selection file return usage status before opening an API client.

`import_status(job_id, accession=None)` reads one receipt. Supplying the accession
enables the server's owner-scoped restart fallback. The SDK checks that returned
job and accession identities match the request. API authentication, entitlement,
validation and server errors retain their existing typed exceptions.

`wait_for_import` never submits a new import. It returns actual `completed`,
`failed`, `cancelled` or `canceled` receipts. A polling timeout raises
`TimeoutError`; its `last_job` attribute contains the last actual receipt, if one
was observed. Timeouts and poll intervals must be positive finite numbers. The
monotonic deadline caps sleeps and each request timeout; transport retries can
extend an in-flight request beyond that deadline. No new poll starts afterward.
Timing out or interrupting a wait does not cancel the server job.

Async usage uses the same arguments and receipt models:

```python
from ligandai import AsyncLigandAI

async with AsyncLigandAI() as client:
    inspection = await client.discovery.inspect_geo(chosen_accession)
    receipt = await client.discovery.import_status(job_id, accession=chosen_accession)
```

The existing signed H-SWAE capabilities API remains a separate interface for its
reviewed immediate analysis operations. A GEO receipt does not imply H-SWAE
annotation, surfaceome coverage, mechanistic validation or a disease-treatment
effect.

## CLI

The CLI emits JSON by default; `--json` is also accepted. Use the exact reviewed
accession and returned job ID in the shell variables below.

```sh
ligandai discovery geo search "human skeletal muscle aging" --limit 5
ligandai discovery geo inspect "$ACCESSION"
ligandai discovery geo import "$ACCESSION"
ligandai discovery geo status "$JOB_ID" --accession "$ACCESSION"
ligandai discovery geo wait "$JOB_ID" --accession "$ACCESSION" --timeout 300 --poll-interval 2
```

`--base-url` remains a global option before `discovery`. Credentials are read from
the standard SDK environment variables and are not command-line arguments.
Search and inspection do not start imports. `import` emits its actual submission
receipt, so exit 0 with status `queued` means the request was accepted, not that
processing completed.

| Exit | Meaning |
| --- | --- |
| 0 | Command succeeded; `wait` observed `completed` |
| 1 | Operation failed; includes a failed/cancelled terminal receipt from `wait` |
| 2 | Incomplete or invalid arguments; no request was made |
| 3 | Authentication, entitlement or legal access failure |
| 4 | Wait deadline reached; JSON includes the last observed receipt |
| 5 | Connection, edge or unreadable-response failure |
| 6 | Credit/payment failure |
| 7 | Invalid client configuration |
| 130 | Interrupted wait/command; no server cancellation was requested |
