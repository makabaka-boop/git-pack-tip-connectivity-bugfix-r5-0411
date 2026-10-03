"""High-level import orchestration: parse -> verify -> stage -> publish."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, Optional, Union

from .connectivity import verify_tips
from .errors import LimitExceededError
from .packfile import MAX_PACK_SIZE, DeltaResolver, parse_pack
from .store import ObjectStore, PublishedStoreView, StagedImport

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

        Nothing is published by this method.  Parsing, delta reconstruction,
        id checks and tip reachability all complete before a single byte is
        staged; any failure aborts the import without touching the object
        store or the manifest.  The pre-existing archive is viewed through a
        snapshot of the manifest, so loose objects orphaned by an earlier
        interrupted publish can never be mistaken for committed content.
        """
        data = _read_source(source)
        parsed = parse_pack(data)

        # Snapshot committed content: only manifest-listed objects may serve
        # as thin-pack bases or satisfy tip references.
        published = PublishedStoreView(self.store, self.store.published_oids())

        resolver = DeltaResolver(parsed.entries, base_provider=published)
        resolver.resolve_all()  # reconstructs + verifies every object

        incoming = {}
        for entry in parsed.entries:
            assert entry.oid is not None and entry.content is not None
            incoming[entry.oid] = (entry.type_name, entry.content)
        verified_tips = verify_tips(tips, incoming, published)

        staged = self.store.begin_import()
        try:
            seen = set()
            staged_objects = []
            for entry in parsed.entries:
                assert entry.oid is not None and entry.content is not None
                if entry.oid in seen:
                    continue  # same object appearing twice in one pack
                seen.add(entry.oid)
                self.store.stage_object(
                    staged, entry.oid, entry.type_name, entry.content
                )
                staged_objects.append(
                    {
                        "oid": entry.oid,
                        "type": entry.type_name,
                        "size": len(entry.content),
                    }
                )
            staged.record = {
                "id": staged.token,
                "tips": verified_tips,
                "imported_at": datetime.now(timezone.utc).isoformat(),
                "pack_id": parsed.pack_id,
                "pack_size": len(data),
                "object_count": len(staged_objects),
                "objects": staged_objects,
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
            # publish() rolls objects moved before the commit point back into
            # the quarantine directory; drop that directory on the way out.
            self.store.abort(staged)
            raise
        return staged.record
