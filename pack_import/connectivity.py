"""Reachability and type verification for named entry points (tips).

An import that names tips promises a *complete commit delivery*: every
commit reachable from a tip, every tree referenced by those commits, every
blob referenced by those trees and every annotated tag in the chain must
exist in the store with an object type matching the reference that names
it.  Pack-level verification (hashes, sizes, deltas) only proves that each
object is internally valid; it does not prove that the graph is closed or
that a reference points at the *kind* of object it is used as.

Objects may be supplied by the pack under import (*incoming*) or by a
previously published import (the destination object store).  Crucially
*incoming* is only consulted for the pack currently being imported, and
the store view handed in here exposes only manifest-backed objects, so a
loose object orphaned on disk by an interrupted (never committed) publish
can never masquerade as archived content.

Git semantics kept intact here:

* annotated tags are dereferenced, including tag chains; a tip may be a
  ``tag`` object that ultimately peels to a commit;
* merge commits carry multiple ``parent`` lines and every ancestry line is
  walked;
* tree entry names are arbitrary bytes (binary/non-UTF-8 filenames are
  legal), so tree entries are parsed bytewise;
* ``mode 160000`` entries are gitlinks (submodule pointers): an external
  repository reference whose commit object is deliberately not required
  in this store;
* mode ``120000`` entries are symlinks and point at plain blobs holding
  the link target path;
* already-archived objects shared with the current pack satisfy the
  references just as objects carried by the pack itself do.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional, Tuple

from .errors import PackFormatError

OID_LEN = 40
_OID_HEX = frozenset(b"0123456789abcdef")

# tree entry modes git actually emits / accepts
_MODE_TREE = 0o040000
_MODE_GITLINK = 0o160000
_FILE_MODES = (0o100644, 0o100755, 0o120000)


def _is_oid(oid) -> bool:
    return (
        isinstance(oid, str)
        and len(oid) == OID_LEN
        and all(ord(c) < 128 and ord(c) in _OID_HEX for c in oid)
    )


def _parse_tree(content: bytes) -> Iterable[Tuple[int, bytes, str]]:
    """Parse raw tree content into ``(mode, name, oid)`` triples.

    Names are arbitrary bytes; the NUL byte terminates the name field, so
    any binary filename -- but also an adversarial embedded NUL -- is
    bounded correctly.
    """
    pos = 0
    n = len(content)
    while pos < n:
        sp = content.find(b" ", pos)
        if sp < 0:
            raise PackFormatError("tree entry missing mode")
        mode_bytes = content[pos:sp]
        if not mode_bytes or any(c < 0x30 or c > 0x37 for c in mode_bytes):
            raise PackFormatError("tree entry has a non-octal mode")
        mode = int(mode_bytes, 8)
        nul = content.find(b"\0", sp + 1)
        if nul < 0 or nul == sp + 1:
            raise PackFormatError("tree entry missing name")
        name = content[sp + 1 : nul]
        if nul + 20 > n:
            raise PackFormatError("tree entry object id truncated")
        oid = content[nul + 1 : nul + 21].hex()
        yield mode, name, oid
        pos = nul + 21


def _commit_refs(content: bytes, oid: str) -> Tuple[str, Tuple[str, ...]]:
    """Return ``(tree_oid, parent_oids)`` declared by a commit object."""
    tree: Optional[str] = None
    parents = []
    pos = 0
    while True:
        nl = content.find(b"\n", pos)
        if nl < 0:
            raise PackFormatError(f"commit {oid} header is not terminated")
        if nl == pos:
            break  # blank line: end of headers
        line = content[pos:nl]
        if line.startswith(b"tree "):
            if tree is not None:
                raise PackFormatError(f"commit {oid} declares multiple trees")
            tree = line[5:].decode("ascii", "replace")
        elif line.startswith(b"parent "):
            parents.append(line[7:].decode("ascii", "replace"))
        # other headers (author/committer/encoding/...) are not graph edges.
        pos = nl + 1
    if tree is None:
        raise PackFormatError(f"commit {oid} has no tree reference")
    return tree, tuple(parents)


def _tag_target(content: bytes, oid: str) -> str:
    """Return the object id referenced by an annotated tag object."""
    target: Optional[str] = None
    pos = 0
    while True:
        nl = content.find(b"\n", pos)
        if nl < 0:
            raise PackFormatError(f"tag {oid} header is not terminated")
        if nl == pos:
            break  # blank line: end of headers
        line = content[pos:nl]
        if line.startswith(b"object "):
            if target is not None:
                raise PackFormatError(f"tag {oid} has multiple object references")
            target = line[7:].decode("ascii", "replace")
        # other single-line headers and gpgsig continuation lines are
        # irrelevant to graph closure.
        pos = nl + 1
    if target is None:
        raise PackFormatError(f"tag {oid} has no object reference")
    return target


class _GraphVerifier:
    def __init__(self, incoming: Mapping[str, Tuple[str, bytes]], store):
        self.incoming = incoming
        self.store = store
        # (kind, oid) pairs already walked, so diamond histories and shared
        # trees are checked once and reference cycles terminate the walk.
        self.visited: set = set()

    def _load(self, oid: str) -> Tuple[str, bytes]:
        hit = self.incoming.get(oid)
        if hit is not None:
            return hit
        if not self.store.has(oid):
            raise PackFormatError(
                f"referenced object {oid} is missing from the pack and the "
                "published object store"
            )
        return self.store.read(oid)

    def _require(self, oid: str, expected: str, used_as: str) -> bytes:
        try:
            type_name, content = self._load(oid)
        except PackFormatError:
            raise
        except Exception as exc:  # corrupt stored object
            raise PackFormatError(
                f"object {oid} used as {used_as} is unreadable: {exc}"
            ) from exc
        if type_name != expected:
            raise PackFormatError(
                f"object {oid} used as {used_as} has wrong type: "
                f"expected {expected}, found {type_name}"
            )
        return content

    def verify_tip(self, oid: str) -> str:
        if not _is_oid(oid):
            raise PackFormatError(f"tip must be a 40-char hex object id: {oid!r}")
        type_name, _content = self._load(oid)
        if type_name == "tag":
            commit_oid = self._peel_tag(oid, {oid})
        elif type_name == "commit":
            commit_oid = oid
        else:
            raise PackFormatError(
                f"tip {oid} must be a commit or an annotated tag that peels "
                f"to a commit, found {type_name}"
            )
        self._walk_commit(commit_oid)
        return commit_oid

    # -- annotated tags --------------------------------------------------------

    def _peel_tag(self, oid: str, seen: set) -> str:
        content = self._require(oid, "tag", "annotated tag")
        target_oid = _tag_target(content, oid)
        if not _is_oid(target_oid):
            raise PackFormatError(f"tag {oid} has a malformed object reference")
        type_name, target_content = self._load(target_oid)
        if type_name == "tag":  # tag chains are legal in git
            if target_oid in seen:
                raise PackFormatError(f"annotated tag cycle involving {target_oid}")
            seen.add(target_oid)
            return self._peel_tag(target_oid, seen)
        if type_name != "commit":
            raise PackFormatError(
                f"tip tag {oid} peels to {target_oid} of type {type_name}, "
                "expected a commit"
            )
        return target_oid

    # -- commits ---------------------------------------------------------------

    def _walk_commit(self, oid: str) -> None:
        key = ("commit", oid)
        if key in self.visited:
            return
        self.visited.add(key)
        content = self._require(oid, "commit", "commit")
        tree_oid, parent_oids = _commit_refs(content, oid)
        if not _is_oid(tree_oid):
            raise PackFormatError(f"commit {oid} has a malformed tree reference")
        self._walk_tree(tree_oid)
        # Every parent must be a commit; merge commits simply have several.
        for parent_oid in parent_oids:
            if not _is_oid(parent_oid):
                raise PackFormatError(f"commit {oid} has a malformed parent reference")
            self._walk_commit(parent_oid)

    # -- trees -----------------------------------------------------------------

    def _walk_tree(self, oid: str) -> None:
        key = ("tree", oid)
        if key in self.visited:
            return
        self.visited.add(key)
        content = self._require(oid, "tree", "tree")
        for mode, name, entry_oid in _parse_tree(content):
            if mode == _MODE_TREE:
                self._walk_tree(entry_oid)
            elif mode == _MODE_GITLINK:
                # Submodule pointer: the referenced commit lives in another
                # repository, so only validate the entry id shape; do not
                # demand the object locally.
                if not _is_oid(entry_oid):
                    raise PackFormatError(
                        f"tree {oid} gitlink entry {name!r} has a malformed id"
                    )
            elif mode in _FILE_MODES:
                # Regular files and symlinks both resolve to blob objects;
                # the blob bytes of a symlink entry are its target path.
                self._require(entry_oid, "blob", f"tree entry {name!r}")
            else:
                raise PackFormatError(
                    f"tree {oid} entry {name!r} has invalid mode {mode:o}"
                )


def verify_tips(tips, incoming, store):
    """Verify that every tip in *tips* is fully and correctly available.

    *incoming* maps oid -> ``(type_name, content)`` for the objects carried
    by the pack currently being imported; *store* provides already
    published objects (``has``/``read``).  On success returns the list of
    distinct tip oids (order preserved); on any missing descendant, kind
    mismatch, malformed object or invalid tip raises
    :class:`PackFormatError`.

    With no tips this performs no checks, preserving the original
    object-level import semantics (the objects of the pack stand on their
    own).
    """
    verifier = _GraphVerifier(incoming, store)
    result = []
    seen = set()
    for oid in tips:
        verifier.verify_tip(oid)
        if oid not in seen:
            seen.add(oid)
            result.append(oid)
    return result
