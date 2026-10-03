"""High-level import orchestration: parse -> verify -> stage -> publish."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, Optional, Union

from .connectivity import verify_tips
from .errors import LimitExceededError
from .packfile import MAX_PACK_SIZE, DeltaResolver, parse_pack
from .store import ObjectStore, StagedImport

PackSource = Union[str, os.PathLike, bytes, bytearray, memoryview]


def _read_source(source: PackSource) -> bytes:
    if isinstance(source, (str, os.PathLike)):
        size = os.path.getsize(source)
        if size > MAX_PACK_SIZE:
            raise LimitExceededError(
                f"pack file is {size} bytes (limit {MAX_PACK_SIZE})"
            )
        with open(source, "rb") as f:
            return f.read()
    return bytes(source)


class PackImporter:
    """Imports verified PACK v2 files into an :class:`ObjectStore`.

    ``import_pack`` is the one-shot operation; ``stage_pack`` +
    ``store.publish`` / ``store.abort`` expose the two-phase protocol for
    callers that want to inspect or cancel before the commit point.
    """

    def __init__(self, store: ObjectStore):
        self.store = store

    def stage_pack(self, source: PackSource, *, tips=()) -> StagedImport:
        """Fully parse and verify *source*, then quarantine its objects.

        Nothing is published by this method.  Any verification failure
        raises before a single byte is staged; any staging failure cleans
        up the quarantine directory.
        """
        data = _read_source(source)
        parsed = parse_pack(data)
        resolver = DeltaResolver(parsed.entries, base_provider=self.store)
        resolver.resolve_all()  # reconstructs + verifies every object

        verified_tips = verify_tips(
            tips, {e.oid: (e.type_name, e.content) for e in parsed.entries}, self.store
        )
        staged = self.store.begin_import()
        try:
            for entry in parsed.entries:
                assert entry.oid is not None and entry.content is not None
                self.store.stage_object(
                    staged, entry.oid, entry.type_name, entry.content
                )
            staged.record = {
                "id": staged.token,
                "tips": verified_tips,
                "imported_at": datetime.now(timezone.utc).isoformat(),
                "pack_id": parsed.pack_id,
                "pack_size": len(data),
                "object_count": len(parsed.entries),
                "objects": [
                    {"oid": e.oid, "type": e.type_name, "size": len(e.content)}
                    for e in parsed.entries
                ],
            }
        except BaseException:
            self.store.abort(staged)
            raise
        return staged

    def import_pack(self, source: PackSource, *, tips=()) -> Dict:
        """Stage *source* and atomically publish it; returns the import record."""
        staged = self.stage_pack(source, tips=tips)
        try:
            self.store.publish(staged)
        except BaseException:
            # Publish may have moved some objects already (harmless: they are
            # content-addressed and unreferenced without the manifest), but
            # the manifest was not replaced -- clean up the quarantine area.
            self.store.abort(staged)
            raise
        return staged.record
