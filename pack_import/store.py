"""Quarantined object store with atomic manifest publication.

Layout of a store directory::

    <store>/
        objects/            published loose objects (git object-dir layout)
            <2 hex>/<38 hex>
        staging/
            <token>/        quarantine area of one in-flight import
                objects/<2 hex>/<38 hex>
        manifest.json       published import manifest (the commit point)
        manifest.json.tmp   scratch file used while publishing
        manifest.lock       advisory lock serializing publish

Protocol
--------
1.  ``begin_import`` creates a private staging directory.
2.  Every verified object is written there in loose-object format
    (zlib of ``"<type> <size>\\0<content>"``) and fsynced.
3.  ``publish`` first moves the staged objects into ``objects/`` with
    atomic renames (content-addressed, so renames are idempotent), then
    appends the import record to the manifest and atomically replaces
    ``manifest.json`` via write-temp + fsync + ``os.replace``.  The
    manifest rename is the single commit point: before it the old
    manifest is still authoritative, after it the import is visible.
4.  A crash before the commit point leaves the old manifest readable and
    the staging directory behind; ``cleanup_staging`` (or ``abort``)
    removes it.  A crash after the objects were moved but before the
    manifest was replaced leaves valid, unreferenced loose objects --
    harmless, exactly like interrupted git object writes.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import shutil
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .errors import MissingBaseError, PackFormatError
from .packfile import compute_oid

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_file_sync(path: Path, data: bytes, mode: int = 0o444) -> None:
    """Write *data* to a fresh *path*, fsync it, and make it read-only."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(path, mode)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise


def loose_object_bytes(type_name: str, content: bytes) -> bytes:
    """Serialize an object the way git stores loose objects."""
    header = f"{type_name} {len(content)}\0".encode("ascii")
    return zlib.compress(header + content)


@dataclass
class StagedImport:
    """Handle for one quarantined, not yet published import."""

    store: "ObjectStore"
    token: str
    directory: Path
    objects: List[Dict] = field(default_factory=list)
    record: Optional[Dict] = None


class ObjectStore:
    """Loose-object store with staging and atomic manifest publication.

    Also implements the ``BaseProvider`` protocol (``has``/``read``) so the
    delta resolver can pull bases of thin packs from already published
    objects.
    """

    def __init__(self, root: os.PathLike | str):
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.staging_dir = self.root / "staging"
        self.manifest_path = self.root / MANIFEST_NAME
        self._manifest_tmp = self.root / (MANIFEST_NAME + ".tmp")
        self._lock_path = self.root / "manifest.lock"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    # -- published state -----------------------------------------------------

    def read_manifest(self) -> Dict:
        """Return the published manifest (empty structure if none exists).

        Only the committed ``manifest.json`` is consulted; a leftover
        ``.tmp`` from an interrupted publish is ignored.
        """
        try:
            with open(self.manifest_path, "rb") as f:
                return json.load(f)
        except FileNotFoundError:
            return {"version": MANIFEST_VERSION, "imports": []}

    def loose_path(self, oid: str) -> Path:
        return self.objects_dir / oid[:2] / oid[2:]

    # -- BaseProvider interface ----------------------------------------------

    def has(self, oid: str) -> bool:
        return self.loose_path(oid).is_file()

    def read(self, oid: str) -> Tuple[str, bytes]:
        path = self.loose_path(oid)
        if not path.is_file():
            raise MissingBaseError(f"object {oid} not in store")
        try:
            raw = zlib.decompress(path.read_bytes())
        except zlib.error as exc:
            raise PackFormatError(f"corrupt loose object {oid}: {exc}") from exc
        nul = raw.find(b"\0")
        if nul < 0:
            raise PackFormatError(f"corrupt loose object {oid}: no header")
        try:
            type_name, size_s = raw[:nul].decode("ascii").split(" ")
            size = int(size_s)
        except ValueError as exc:
            raise PackFormatError(f"corrupt loose object {oid}: bad header") from exc
        content = raw[nul + 1 :]
        if len(content) != size:
            raise PackFormatError(f"corrupt loose object {oid}: size mismatch")
        if compute_oid(type_name, content) != oid:
            raise PackFormatError(f"corrupt loose object {oid}: id mismatch")
        return type_name, content

    # -- staging --------------------------------------------------------------

    def begin_import(self) -> StagedImport:
        token = f"{int(time.time())}-{os.getpid()}-{secrets.token_hex(4)}"
        directory = self.staging_dir / token
        (directory / "objects").mkdir(parents=True)
        return StagedImport(store=self, token=token, directory=directory)

    def stage_object(
        self, staged: StagedImport, oid: str, type_name: str, content: bytes
    ) -> None:
        rel = Path(oid[:2]) / oid[2:]
        target = staged.directory / "objects" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_file_sync(target, loose_object_bytes(type_name, content))
        staged.objects.append(
            {"oid": oid, "type": type_name, "size": len(content), "path": str(rel)}
        )

    def abort(self, staged: StagedImport) -> None:
        """Cancel a staged import: drop the quarantine directory."""
        shutil.rmtree(staged.directory, ignore_errors=True)

    def cleanup_staging(self) -> None:
        """Remove all interrupted-import leftovers (staging dirs, tmp file)."""
        if self.staging_dir.is_dir():
            for child in self.staging_dir.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        self._manifest_tmp.unlink(missing_ok=True)

    # -- publication ------------------------------------------------------------

    def publish(self, staged: StagedImport) -> None:
        """Atomically publish a fully staged import.

        Objects are moved into place first; the manifest replacement is the
        atomic commit point.  On any failure the previously published
        manifest remains valid.
        """
        if staged.record is None:
            raise ValueError("staged import has no manifest record")
        with open(self._lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._move_objects(staged)
            self._publish_manifest(staged.record)
        shutil.rmtree(staged.directory, ignore_errors=True)

    def _move_objects(self, staged: StagedImport) -> None:
        synced = set()
        for obj in staged.objects:
            src = staged.directory / "objects" / obj["path"]
            dst = self.objects_dir / obj["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                # Content-addressed store: an existing object must be
                # byte-identical, otherwise something is deeply wrong.
                if dst.read_bytes() != src.read_bytes():
                    raise PackFormatError(
                        f"object {obj['oid']} already exists with different content"
                    )
                src.unlink()
            else:
                os.replace(src, dst)  # atomic, same filesystem
            if dst.parent not in synced:
                _fsync_dir(dst.parent)
                synced.add(dst.parent)
        _fsync_dir(self.objects_dir)

    def _publish_manifest(self, record: Dict) -> None:
        manifest = self.read_manifest()
        manifest.setdefault("imports", []).append(record)
        data = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        with open(self._manifest_tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(self._manifest_tmp, self.manifest_path)  # commit point
        _fsync_dir(self.root)
