"""Command line interface: ``python -m pack_import --store DIR <command>``."""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional

from .errors import PackImportError
from .importer import PackImporter
from .store import ObjectStore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pack_import",
        description="Verifying importer for Git PACK v2 files "
        "(no git index-pack involved).",
    )
    parser.add_argument(
        "--store", required=True, help="object store directory (created if missing)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="verify, stage and publish a pack")
    p_import.add_argument(
        "--tip",
        action="append",
        default=[],
        metavar="OBJECT-ID",
        help="commit (or annotated tag peeling to a commit) whose full "
        "reachable object graph must be delivered by this pack; may be "
        "given multiple times",
    )
    p_import.add_argument("packfile", help="path to the .pack file")
    p_import.add_argument(
        "--no-publish",
        action="store_true",
        help="stage only; leave the import in the quarantine area",
    )

    sub.add_parser("manifest", help="print the published import manifest")
    sub.add_parser(
        "cleanup", help="remove interrupted-import leftovers from the staging area"
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    store = ObjectStore(args.store)
    try:
        if args.command == "import":
            importer = PackImporter(store)
            if args.no_publish:
                staged = importer.stage_pack(args.packfile, tips=args.tip)
                json.dump(staged.record, sys.stdout, indent=2, sort_keys=True)
                print()
                print(
                    f"staged under {staged.directory} (not published)",
                    file=sys.stderr,
                )
            else:
                record = importer.import_pack(args.packfile, tips=args.tip)
                json.dump(record, sys.stdout, indent=2, sort_keys=True)
                print()
        elif args.command == "manifest":
            json.dump(store.read_manifest(), sys.stdout, indent=2, sort_keys=True)
            print()
        elif args.command == "cleanup":
            store.cleanup_staging()
            print("staging area cleaned", file=sys.stderr)
        return 0
    except PackImportError as exc:
        print(f"pack_import: error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"pack_import: I/O error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
