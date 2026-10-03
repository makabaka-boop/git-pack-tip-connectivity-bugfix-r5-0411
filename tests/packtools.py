"""Test helpers: git wrappers and a from-scratch PACK v2 builder.

The builder lets tests construct packs with arbitrary (including
malicious) object records while still producing a well-formed envelope
(header + checksum), so the parser can be attacked with precisely
controlled inputs.
"""

from __future__ import annotations

import hashlib
import os
import random
import string
import struct
import subprocess
import zlib
from pathlib import Path

from pack_import.packfile import (
    OBJ_BLOB,
    OBJ_OFS_DELTA,
    OBJ_REF_DELTA,
    compute_oid,
)

# --- git wrappers --------------------------------------------------------------


def git(repo, *args, input=None, env=None):
    """Run git in *repo*; returns the CompletedProcess. Fails the test on error."""
    if isinstance(input, str):
        input = input.encode()
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        input=input,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def git_out(repo, *args, **kwargs) -> str:
    return git(repo, *args, **kwargs).stdout.decode().strip()


def git_bytes(repo, *args, **kwargs) -> bytes:
    return git(repo, *args, **kwargs).stdout


def all_oids(repo) -> set:
    """Every object id in the repo (commits, trees, blobs, tag objects)."""
    out = git_out(repo, "rev-list", "--objects", "--all")
    oids = {line.split()[0] for line in out.splitlines() if line}
    refs = git_out(repo, "for-each-ref", "--format=%(objectname)")
    oids |= {line for line in refs.splitlines() if line}
    return oids


def pack_objects(repo, oids, *extra) -> bytes:
    """Pack *oids* with ``git pack-objects --stdout`` and return the bytes."""
    stdin = "".join(f"{oid}\n" for oid in sorted(oids))
    return git_bytes(repo, "pack-objects", "--stdout", "-q", *extra, input=stdin)


def make_repo(path) -> Path:
    """Create a repo with an evolving large file (to provoke deltas),
    several commits and one annotated tag."""
    repo = Path(path)
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Pack Test")
    git(repo, "config", "user.email", "pack@example.com")
    rng = random.Random(20240613)
    words = ["".join(rng.choices(string.ascii_lowercase, k=8)) for _ in range(400)]
    lines = [f"{i:04d} {rng.choice(words)} {rng.choice(words)}" for i in range(3000)]
    for version in range(4):
        for _ in range(60):  # mutate ~2% of the lines
            idx = rng.randrange(len(lines))
            lines[idx] = f"{idx:04d} {rng.choice(words)} v{version}"
        (repo / "big.txt").write_text("\n".join(lines) + "\n")
        (repo / f"file{version}.txt").write_text(
            f"small file v{version}\n" * (version + 1)
        )
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"commit {version}")
    git(repo, "tag", "-a", "-m", "annotated tag", "v1.0")
    return repo


def cat_file_env(store) -> dict:
    """Environment that makes git read objects from *store*'s object dir."""
    env = dict(os.environ)
    env["GIT_OBJECT_DIRECTORY"] = str(Path(store.objects_dir).resolve())
    return env


# --- pack builder ----------------------------------------------------------------


def encode_obj_header(obj_type: int, size: int) -> bytes:
    first = (obj_type << 4) | (size & 0x0F)
    size >>= 4
    out = bytearray()
    if size:
        first |= 0x80
    out.append(first)
    while size:
        byte = size & 0x7F
        size >>= 7
        if size:
            byte |= 0x80
        out.append(byte)
    return bytes(out)


def encode_ofs(offset: int) -> bytes:
    """Encode an OFS_DELTA negative offset the way git does."""
    assert offset > 0
    out = bytearray([offset & 0x7F])
    offset >>= 7
    while offset:
        offset -= 1
        out.append(0x80 | (offset & 0x7F))
        offset >>= 7
    return bytes(reversed(out))


def delta_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def op_insert(data: bytes) -> bytes:
    """Literal-insert command(s) for *data* (max 127 bytes per command)."""
    assert data, "insert of zero bytes is the reserved command 0"
    out = bytearray()
    for i in range(0, len(data), 127):
        chunk = data[i : i + 127]
        out.append(len(chunk))
        out += chunk
    return bytes(out)


def op_copy(offset: int, size: int) -> bytes:
    """Copy-from-base command with minimal flag encoding."""
    cmd = 0x80
    out = bytearray()
    for bit, shift in ((0x01, 0), (0x02, 8), (0x04, 16), (0x08, 24)):
        if offset >> shift & 0xFF:
            cmd |= bit
            out.append(offset >> shift & 0xFF)
    for bit, shift in ((0x10, 0), (0x20, 8), (0x40, 16)):
        if size >> shift & 0xFF:
            cmd |= bit
            out.append(size >> shift & 0xFF)
    return bytes([cmd]) + bytes(out)


def make_delta(src_len: int, tgt_len: int, ops: bytes) -> bytes:
    return delta_varint(src_len) + delta_varint(tgt_len) + ops


def insert_delta(src_len: int, content: bytes) -> bytes:
    """A delta that replaces the whole base with *content*."""
    return make_delta(src_len, len(content), op_insert(content))


class PackBuilder:
    """Builds PACK v2 byte strings entry by entry.

    ``add`` accepts raw payloads; ofs-delta bases are referenced by entry
    index so offsets are always computed correctly.  The trailing checksum
    is computed over whatever was built, so tests get a valid envelope
    around arbitrarily broken object records.
    """

    def __init__(self):
        self._body = bytearray()
        self._starts = []

    def _begin(self) -> int:
        self._starts.append(len(self._body) + 12)  # +12: pack header
        return self._starts[-1]

    def add(
        self,
        obj_type: int,
        payload: bytes,
        *,
        base_index=None,
        base_oid=None,
        declared_size=None,
        compress=True,
        raw_payload=None,
    ) -> int:
        """Append one object; returns its entry index."""
        self._begin()
        size = len(payload) if declared_size is None else declared_size
        record = encode_obj_header(obj_type, size)
        if obj_type == OBJ_OFS_DELTA:
            base_start = self._starts[base_index]
            record += encode_ofs(self._starts[-1] - base_start)
        elif obj_type == OBJ_REF_DELTA:
            record += bytes.fromhex(base_oid)
        if raw_payload is not None:
            record += raw_payload
        else:
            record += zlib.compress(payload) if compress else payload
        self._body += record
        return len(self._starts) - 1

    def add_blob(self, content: bytes, **kwargs) -> int:
        return self.add(OBJ_BLOB, content, **kwargs)

    def add_raw_bytes(self, data: bytes) -> None:
        """Splice arbitrary bytes into the object stream (desync attacks)."""
        self._body += data

    def build(self, *, corrupt_checksum=False, count_override=None) -> bytes:
        count = len(self._starts) if count_override is None else count_override
        head = b"PACK" + struct.pack(">II", 2, count) + bytes(self._body)
        digest = hashlib.sha1(head).digest()
        if corrupt_checksum:
            digest = bytes([digest[0] ^ 0xFF]) + digest[1:]
        return head + digest


def blob_oid(content: bytes) -> str:
    return compute_oid("blob", content)
