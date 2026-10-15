# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Explicit GEO metadata inspection and import commands; JSON preserves provenance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ligandai._geo import GeoImportTimeout


def add_discovery_parser(subparsers: Any) -> None:
    discovery = subparsers.add_parser("discovery", help="Discover and inspect public datasets.")
    groups = discovery.add_subparsers(dest="discovery_command")
    geo = groups.add_parser("geo", help="Public GEO search, inspection and explicit imports.")
    commands = geo.add_subparsers(dest="geo_command")
    search = commands.add_parser("search", help="Search metadata; never downloads expression data.")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    inspect = commands.add_parser("inspect", help="Inspect sample annotations and parser warnings.")
    inspect.add_argument("accession")
    start = commands.add_parser("import", help="Start an import; a queued receipt is not completion.")
    start.add_argument("accession")
    start.add_argument("--force", action="store_true", help="Ask the server to re-import this accession.")
    start.add_argument("--selection", metavar="JSON_FILE", help="Explicit published file names and parse plan; no expression file upload.")
    status = commands.add_parser("status", help="Read the actual import receipt.")
    wait = commands.add_parser("wait", help="Poll an existing import; never creates a new job.")
    for command in (status, wait):
        command.add_argument("job_id")
        command.add_argument("--accession", help="GSE accession for owner-scoped restart fallback.")
    wait.add_argument("--timeout", type=float, default=300, metavar="SECONDS")
    wait.add_argument("--poll-interval", type=float, default=2, metavar="SECONDS")
    for command in (search, inspect, start, status, wait):
        command.add_argument("--json", action="store_true", help="Emit JSON (the default).")


def run_discovery(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    # Reuse the standard auth/config/error mapping; import lazily to avoid a cycle.
    from ligandai.cli import (
        EXIT_FAILED,
        EXIT_INCOMPLETE,
        EXIT_OK,
        EXIT_USAGE,
        _fail,
        _incomplete,
        _make_client,
        _print_err,
    )

    if args.discovery_command != "geo":
        return _incomplete(parser, "discovery")
    if not args.geo_command:
        return _incomplete(parser, "discovery geo")

    # Validate locally before opening a client or making any HTTP request.
    from ligandai._geo import (
        geo_accession,
        geo_import_selection,
        geo_job_id,
        geo_poll_parameters,
        geo_search_params,
    )

    selection = None
    try:
        if args.geo_command == "search":
            geo_search_params(args.query, args.limit)
        elif args.geo_command in {"inspect", "import"}:
            geo_accession(args.accession)
            if args.geo_command == "import" and args.selection:
                with Path(args.selection).open("rb") as source:
                    content = source.read(1024 * 1024 + 1)
                if len(content) > 1024 * 1024:
                    raise ValueError("selection exceeds the one MiB limit")
                selection = geo_import_selection(json.loads(content))
                if selection is None:
                    raise ValueError("selection file must contain a JSON object")
        else:
            geo_job_id(args.job_id)
            if args.accession is not None:
                geo_accession(args.accession)
            if args.geo_command == "wait":
                geo_poll_parameters(args.timeout, args.poll_interval)
    except (ValueError, OSError) as exc:
        _print_err(str(exc))
        return EXIT_USAGE

    try:
        with _make_client(args) as client:
            discovery = client.discovery
            command = args.geo_command
            if command == "search":
                result = discovery.search_geo_response(args.query, limit=args.limit)
            elif command == "inspect":
                result = discovery.inspect_geo(args.accession)
            elif command == "import":
                result = discovery.import_geo(args.accession, force=args.force, selection=selection)
            elif command == "status":
                result = discovery.import_status(args.job_id, accession=args.accession)
            else:
                result = discovery.wait_for_import(
                    args.job_id, accession=args.accession,
                    timeout=args.timeout, poll_interval=args.poll_interval,
                )
            print(result.model_dump_json(by_alias=True, exclude_unset=True, indent=2))
            if command == "wait" and result.status != "completed":
                return EXIT_FAILED
            return EXIT_OK
    except GeoImportTimeout as exc:
        print(json.dumps({
            "completed": False,
            "reason": "polling_timeout",
            "jobId": exc.job_id,
            "lastJob": (
                exc.last_job.model_dump(mode="json", by_alias=True, exclude_unset=True)
                if exc.last_job else None
            ),
        }, indent=2))
        _print_err(str(exc))
        return EXIT_INCOMPLETE
    except KeyboardInterrupt:
        _print_err("Interrupted; the import was not cancelled. Use status or wait to resume.")
        return 130
    except Exception as exc:
        return _fail(f"GEO {args.geo_command}", exc)
