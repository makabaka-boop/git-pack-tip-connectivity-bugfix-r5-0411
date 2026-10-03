"""Parsing, verification and delta resolution for Git PACK v2 files.

This module performs *all* structural validation of a pack before any byte
is trusted:

* the trailing SHA-1 checksum must match the hash of every preceding byte;
* the header must be ``PACK`` / version 2 with at most ``MAX_OBJECTS``
  objects, and the whole file at most ``MAX_PACK_SIZE`` bytes;
* every object header (type + declared size varint) must be well formed;
* OFS_DELTA offsets must point backwards at the start of a previously
  parsed object;
* each zlib stream is inflated with a hard output cap; the stream must end
  exactly (``eof``), its uncompressed length must equal the declared size,
  and the compressed stream boundary determines where the next object
  starts -- after the last object exactly the 20 checksum bytes may remain;
* delta programs are checked instruction by instruction: copy ranges must
  lie inside the base object, literal inserts must not overrun the program,
  command 0 is rejected, and the produced bytes must match the declared
  target size exactly;
* reconstructed objects must not exceed ``MAX_OBJECT_SIZE`` and delta
  chains must not be deeper than ``MAX_DELTA_DEPTH``;
* missing base objects and dependency cycles reject the whole batch.

Nothing here shells out to git (in particular ``git index-pack`` is never
used); the only compression/format primitives are :mod:`zlib` and
:mod:`hashlib` from the standard library.
"""

from __future__ import annotations

import hashlib
import hmac
import struct
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Tuple

from .errors import (
    ChecksumError,
    DeltaChainCycleError,
    DeltaChainTooDeepError,
    LimitExceededError,
    MissingBaseError,
    ObjectTooLargeError,
    PackFormatError,
)

# --- pack format constants -------------------------------------------------

PACK_SIGNATURE = b"PACK"
PACK_VERSION = 2
HEADER_SIZE = 12  # signature + version + object count
CHECKSUM_SIZE = 20  # trailing SHA-1

OBJ_COMMIT = 1
OBJ_TREE = 2
OBJ_BLOB = 3
OBJ_TAG = 4
OBJ_OFS_DELTA = 6
OBJ_REF_DELTA = 7

TYPE_NAMES = {
    OBJ_COMMIT: "commit",
    OBJ_TREE: "tree",
    OBJ_BLOB: "blob",
    OBJ_TAG: "tag",
}

# --- hard limits ------------------------------------------------------------

MAX_PACK_SIZE = 8 * 1024 * 1024  # 8 MB per pack file
MAX_OBJECTS = 200  # objects per pack
MAX_OBJECT_SIZE = 1 * 1024 * 1024  # 1 MB per reconstructed object
MAX_DELTA_DEPTH = 8  # delta chain layers


def compute_oid(type_name: str, content: bytes) -> str:
    """Return the Git object id (SHA-1) for *content* of type *type_name*."""
    h = hashlib.sha1()
    h.update(f"{type_name} {len(content)}\0".encode("ascii"))
    h.update(content)
    return h.hexdigest()


# --- parsed representation --------------------------------------------------


@dataclass
class PackEntry:
    """One object record inside the pack."""

    offset: int  # absolute offset of the object header
    type: int  # OBJ_* code
    declared_size: int  # size field from the object header
    payload: bytes  # inflated bytes: raw content or delta program
    base_offset: Optional[int] = None  # OFS_DELTA: absolute offset of base
    base_oid: Optional[str] = None  # REF_DELTA: hex object id of base

    # filled in by DeltaResolver:
    content: Optional[bytes] = None  # reconstructed canonical object bytes
    type_name: Optional[str] = None  # "commit" | "tree" | "blob" | "tag"
    oid: Optional[str] = None
    depth: int = 0
    _state: int = field(default=0, repr=False)


@dataclass
class ParsedPack:
    entries: List[PackEntry]
    pack_id: str  # hex of the trailing checksum


# --- low-level readers -------------------------------------------------------


def _read_size_header(data: bytes, pos: int, end: int) -> Tuple[int, int, int]:
    """Read the object type/size varint.  Returns (type, size, new_pos)."""
    if pos >= end:
        raise PackFormatError("object header starts beyond pack body")
    byte = data[pos]
    pos += 1
    obj_type = (byte >> 4) & 0x07
    size = byte & 0x0F
    shift = 4
    while byte & 0x80:
        if pos >= end:
            raise PackFormatError("object size varint runs past pack body")
        if shift > 63:
            raise PackFormatError("object size varint is absurdly long")
        byte = data[pos]
        pos += 1
        size |= (byte & 0x7F) << shift
        shift += 7
    return obj_type, size, pos


def _read_ofs_delta_offset(data: bytes, pos: int, end: int) -> Tuple[int, int]:
    """Read the OFS_DELTA negative offset varint.  Returns (offset, new_pos)."""
    if pos >= end:
        raise PackFormatError("ofs-delta offset missing")
    byte = data[pos]
    pos += 1
    offset = byte & 0x7F
    while byte & 0x80:
        if pos >= end:
            raise PackFormatError("ofs-delta offset runs past pack body")
        byte = data[pos]
        pos += 1
        offset = ((offset + 1) << 7) | (byte & 0x7F)
        if offset > MAX_PACK_SIZE:
            raise PackFormatError("ofs-delta offset exceeds pack size")
    return offset, pos


def _inflate(data: bytes, pos: int, end: int, declared_size: int) -> Tuple[bytes, int]:
    """Inflate one zlib stream starting at *pos*.

    Returns ``(payload, consumed)`` where *consumed* is the exact number of
    compressed bytes belonging to the stream -- this is what pins down the
    boundary to the next object.  Raises if the stream is invalid,
    truncated, or produces anything but exactly *declared_size* bytes.
    """
    decomp = zlib.decompressobj()
    try:
        out = decomp.decompress(data[pos:end], declared_size + 1)
    except zlib.error as exc:
        raise PackFormatError(f"invalid zlib stream at offset {pos}: {exc}") from exc
    if decomp.unconsumed_tail or len(out) > declared_size:
        # The output cap was hit: the stream wants to produce more bytes
        # than the object header declared.
        raise PackFormatError(
            f"zlib stream at offset {pos} exceeds declared size {declared_size}"
        )
    if not decomp.eof:
        raise PackFormatError(f"truncated zlib stream at offset {pos}")
    if decomp.flush():
        raise PackFormatError(f"zlib stream at offset {pos} has trailing output")
    if len(out) != declared_size:
        raise PackFormatError(
            f"declared size {declared_size} != inflated size {len(out)} "
            f"at offset {pos}"
        )
    consumed = (end - pos) - len(decomp.unused_data)
    return out, consumed


def _parse_entry(
    data: bytes, pos: int, end: int, previous_starts: set
) -> Tuple[PackEntry, int]:
    start = pos
    obj_type, size, pos = _read_size_header(data, pos, end)
    if obj_type not in TYPE_NAMES and obj_type not in (OBJ_OFS_DELTA, OBJ_REF_DELTA):
        raise PackFormatError(f"reserved object type {obj_type} at offset {start}")
    if size > MAX_OBJECT_SIZE:
        # Applies both to raw object content and to delta programs (a delta
        # program for a <=1 MiB target never needs to be larger than this).
        raise ObjectTooLargeError(
            f"object at offset {start} declares {size} bytes "
            f"(limit {MAX_OBJECT_SIZE})"
        )

    base_offset = None
    base_oid = None
    if obj_type == OBJ_OFS_DELTA:
        rel, pos = _read_ofs_delta_offset(data, pos, end)
        if rel == 0:
            raise PackFormatError(f"zero ofs-delta offset at {start}")
        base_offset = start - rel
        if base_offset not in previous_starts:
            raise PackFormatError(
                f"ofs-delta at offset {start} points at {base_offset}, "
                "which is not the start of a preceding object"
            )
    elif obj_type == OBJ_REF_DELTA:
        if pos + 20 > end:
            raise PackFormatError(f"ref-delta base id truncated at offset {start}")
        base_oid = data[pos : pos + 20].hex()
        pos += 20

    payload, consumed = _inflate(data, pos, end, size)
    pos += consumed
    entry = PackEntry(
        offset=start,
        type=obj_type,
        declared_size=size,
        payload=payload,
        base_offset=base_offset,
        base_oid=base_oid,
    )
    return entry, pos


def parse_pack(data: bytes) -> ParsedPack:
    """Parse and structurally verify a PACK v2 byte string.

    Delta *resolution* is a separate step (:class:`DeltaResolver`); this
    function validates the envelope: limits, checksum, headers, zlib stream
    boundaries and declared sizes.
    """
    if len(data) > MAX_PACK_SIZE:
        raise LimitExceededError(f"pack is {len(data)} bytes (limit {MAX_PACK_SIZE})")
    if len(data) < HEADER_SIZE + CHECKSUM_SIZE:
        raise PackFormatError("pack too small for header plus checksum")
    if data[:4] != PACK_SIGNATURE:
        raise PackFormatError("bad pack signature")
    version, count = struct.unpack(">II", data[4:HEADER_SIZE])
    if version != PACK_VERSION:
        raise PackFormatError(f"unsupported pack version {version}")
    if count > MAX_OBJECTS:
        raise LimitExceededError(f"pack declares {count} objects (limit {MAX_OBJECTS})")

    body_end = len(data) - CHECKSUM_SIZE
    expected = hashlib.sha1(data[:body_end]).digest()
    actual = data[body_end:]
    if not hmac.compare_digest(expected, actual):
        raise ChecksumError("pack checksum mismatch")

    entries: List[PackEntry] = []
    starts = set()
    pos = HEADER_SIZE
    for _ in range(count):
        entry, pos = _parse_entry(data, pos, body_end, starts)
        starts.add(entry.offset)
        entries.append(entry)
    if pos != body_end:
        raise PackFormatError(
            f"object stream desynchronized: {body_end - pos} unexpected "
            "bytes before pack checksum"
        )
    return ParsedPack(entries=entries, pack_id=actual.hex())


# --- delta programs -----------------------------------------------------------


class _Cursor:
    """Bounds-checked reader over a delta program."""

    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes):
        self.buf = buf
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            raise PackFormatError("delta program is truncated")
        chunk = self.buf[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def byte(self) -> int:
        return self.take(1)[0]

    def varint(self) -> int:
        value = 0
        shift = 0
        while True:
            byte = self.byte()
            value |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return value
            shift += 7
            if shift > 63:
                raise PackFormatError("delta varint is absurdly long")

    def done(self) -> bool:
        return self.pos == len(self.buf)


def apply_delta_program(
    base: bytes, program: bytes, max_size: int = MAX_OBJECT_SIZE
) -> bytes:
    """Apply a Git delta program to *base* and return the reconstructed bytes.

    Every copy range is validated against the base length, literal inserts
    against the program length, and the output against the declared target
    size (and *max_size*).
    """
    cur = _Cursor(program)
    src_size = cur.varint()
    if src_size != len(base):
        raise PackFormatError(
            f"delta source size {src_size} != base object size {len(base)}"
        )
    tgt_size = cur.varint()
    if tgt_size > max_size:
        raise ObjectTooLargeError(
            f"delta target size {tgt_size} exceeds limit {max_size}"
        )

    out = bytearray()
    while not cur.done():
        cmd = cur.byte()
        if cmd & 0x80:
            # copy from base
            cp_off = 0
            cp_size = 0
            if cmd & 0x01:
                cp_off |= cur.byte()
            if cmd & 0x02:
                cp_off |= cur.byte() << 8
            if cmd & 0x04:
                cp_off |= cur.byte() << 16
            if cmd & 0x08:
                cp_off |= cur.byte() << 24
            if cmd & 0x10:
                cp_size |= cur.byte()
            if cmd & 0x20:
                cp_size |= cur.byte() << 8
            if cmd & 0x40:
                cp_size |= cur.byte() << 16
            if cp_size == 0:
                cp_size = 0x10000
            if cp_off > len(base) or cp_size > len(base) - cp_off:
                raise PackFormatError(
                    f"delta copy range [{cp_off}, {cp_off + cp_size}) exceeds "
                    f"base size {len(base)}"
                )
            out += base[cp_off : cp_off + cp_size]
        elif cmd:
            # literal insert of cmd bytes
            out += cur.take(cmd)
        else:
            raise PackFormatError("delta command 0 is reserved")
        if len(out) > tgt_size:
            raise PackFormatError(
                f"delta produced more than the declared target size {tgt_size}"
            )
    if len(out) != tgt_size:
        raise PackFormatError(
            f"delta produced {len(out)} bytes, target size says {tgt_size}"
        )
    return bytes(out)


# --- delta resolution ---------------------------------------------------------


class BaseProvider(Protocol):
    """Source of base objects that live outside the pack (thin packs)."""

    def has(self, oid: str) -> bool: ...

    def read(self, oid: str) -> Tuple[str, bytes]:
        """Return ``(type_name, content)`` for *oid*; raise if unreadable."""


class _Deferred(Exception):
    """Internal: a REF_DELTA base is not known yet; retry in a later pass."""

    def __init__(self, oid: str):
        super().__init__(oid)
        self.oid = oid


_UNRESOLVED, _VISITING, _RESOLVED = 0, 1, 2


class DeltaResolver:
    """Reconstruct canonical object bytes for every entry of a parsed pack.

    Resolution is a fixpoint over the entries: plain objects and OFS_DELTA
    chains resolve immediately (OFS bases always precede the delta in the
    pack), REF_DELTA entries wait until their base object id is known --
    either reconstructed from the pack itself or supplied by
    *base_provider* (the destination object store, for thin packs).

    The resolver enforces the depth limit, the per-object size limit, and
    detects dependency cycles.  Any failure aborts the whole batch.
    """

    def __init__(
        self,
        entries: List[PackEntry],
        base_provider: Optional[BaseProvider] = None,
        max_depth: int = MAX_DELTA_DEPTH,
        max_size: int = MAX_OBJECT_SIZE,
    ):
        self.entries = entries
        self.base_provider = base_provider
        self.max_depth = max_depth
        self.max_size = max_size
        self._by_offset: Dict[int, PackEntry] = {e.offset: e for e in entries}
        self._by_oid: Dict[str, Tuple[bytes, str, int]] = {}
        self._external: Dict[str, Tuple[str, bytes]] = {}

    def resolve_all(self) -> None:
        pending = list(self.entries)
        while pending:
            deferred: List[Tuple[PackEntry, str]] = []
            progressed = False
            for entry in pending:
                try:
                    self._resolve(entry)
                    progressed = True
                except _Deferred as d:
                    deferred.append((entry, d.oid))
            if not deferred:
                return
            if not progressed:
                missing = sorted({oid for _, oid in deferred})
                where = ", ".join(
                    f"offset {e.offset} wants {oid}" for e, oid in deferred[:5]
                )
                raise MissingBaseError(
                    f"delta base object(s) not in pack or object store: "
                    f"{', '.join(missing)} ({where})"
                )
            pending = [e for e, _ in deferred]

    def _resolve(self, entry: PackEntry) -> None:
        if entry._state == _RESOLVED:
            return
        if entry._state == _VISITING:
            raise DeltaChainCycleError(
                f"delta dependency cycle involving object at offset {entry.offset}"
            )
        entry._state = _VISITING
        try:
            if entry.type in TYPE_NAMES:
                entry.type_name = TYPE_NAMES[entry.type]
                entry.content = entry.payload
                entry.depth = 0
            else:
                base_content, base_type, base_depth = self._resolve_base(entry)
                entry.depth = base_depth + 1
                if entry.depth > self.max_depth:
                    raise DeltaChainTooDeepError(
                        f"delta chain deeper than {self.max_depth} at offset "
                        f"{entry.offset}"
                    )
                entry.type_name = base_type
                entry.content = apply_delta_program(
                    base_content, entry.payload, self.max_size
                )
            if len(entry.content) > self.max_size:
                raise ObjectTooLargeError(
                    f"reconstructed object at offset {entry.offset} is "
                    f"{len(entry.content)} bytes (limit {self.max_size})"
                )
            entry.oid = compute_oid(entry.type_name, entry.content)
            entry._state = _RESOLVED
            self._by_oid[entry.oid] = (entry.content, entry.type_name, entry.depth)
        finally:
            if entry._state == _VISITING:
                # Exception path (deferral or failure): allow a later retry.
                entry._state = _UNRESOLVED

    def _resolve_base(self, entry: PackEntry) -> Tuple[bytes, str, int]:
        if entry.type == OBJ_OFS_DELTA:
            base = self._by_offset.get(entry.base_offset)
            if base is None:  # parse_pack already guarantees this
                raise PackFormatError(
                    f"ofs-delta at offset {entry.offset} has unknown base "
                    f"offset {entry.base_offset}"
                )
            self._resolve(base)
            assert base.content is not None and base.type_name is not None
            return base.content, base.type_name, base.depth

        # OBJ_REF_DELTA
        assert entry.base_oid is not None
        hit = self._by_oid.get(entry.base_oid)
        if hit is not None:
            return hit
        external = self._lookup_external(entry.base_oid)
        if external is not None:
            type_name, content = external
            return content, type_name, 0
        raise _Deferred(entry.base_oid)

    def _lookup_external(self, oid: str) -> Optional[Tuple[str, bytes]]:
        if self.base_provider is None:
            return None
        if oid not in self._external:
            if not self.base_provider.has(oid):
                return None
            self._external[oid] = self.base_provider.read(oid)
        return self._external[oid]
