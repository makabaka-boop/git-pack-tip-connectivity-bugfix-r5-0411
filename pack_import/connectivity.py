"""Tip reachability and object-kind verification for ``--tip`` imports.

A tip import promises *complete commit-graph delivery*: when the import
reports success every commit reachable from the requested tip, every tree
those commits reference, and every file content (blob) those trees
reference must be present locally, with the object kind matching the use
of each reference.  This module performs that walk over the union of the
objects delivered by the incoming pack and the objects already recorded
in the *published manifest* -- loose objects left behind by an
interrupted publish are deliberately **not** considered archived.

Git semantics honoured here:

* a tip may be a commit or an annotated tag; annotated tags can chain
  (a tag on a tag) and must ultimately peel to a commit;
* a commit's ``tree`` must be a tree object, each ``parent`` a commit;
* tree entry modes fix the referenced kind: symlink mode ``120000``
  points at a blob (the link target text), directory mode ``040000`` at
  a tree, and gitlink mode ``160000`` references an *external*
  submodule commit whose objects are not required locally;
* tree entry names are raw bytes (binary or non-UTF-8 names are legal)
  and may contain spaces; only the single NUL delimiter separates a name
  from the following 20-byte id;
* a shared object that already belongs to a previous successful import
  does not have to be re-delivered.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Tuple

from .errors import ConnectivityError

_COMMIT = "commit"
_TREE = "tree"
_BLOB = "blob"
_TAG = "tag"
_VALID_TYPES = (_COMMIT, _TREE, _BLOB, _TAG)

# git tree mode (decimal as stored) -> referenced object kind; None marks
# the gitlink (submodule) mode, an external reference exempt from lookup.
_TREE_MODES: Dict[int, Optional[str]] = {
    0o100644: _BLOB,  # regular, non-executable
    0o100664: _BLOB,  # historically written regular file
    0o100755: _BLOB,  # executable
    0o120000: _BLOB,  # symbolic link (target path stored as blob text)
    0o040000: _TREE,  # subdirectory
    0o160000: None,  # gitlink: submodule commit in another repository
}

_HEX = set(b"0123456789abcdefABCDEF")
_TAG_DEPTH_LIMIT = 42


def verify_tips(tips, incoming, store) -> List[str]:
    """Validate that each tip names a fully available commit graph.

    *incoming* maps the oids delivered by this pack to
    ``(type_name, content)``; *store* is the destination
    :class:`~pack_import.store.ObjectStore`, whose published manifest (not
    the loose-object directory alone) defines the existing archive.

    Returns the normalized list of tip oids.  Raises
    :class:`~pack_import.errors.ConnectivityError` on any missing
    descendant, kind mismatch, malformed object, or bad tip id.
    """
    normalized = [_as_oid(tip) for tip in tips]
    verifier = _GraphVerifier(incoming, store)
    for tip in normalized:
        commit = verifier.follow_tags(tip)
        verifier.require_commit_graph(commit)
    return normalized


def _as_oid(tip) -> str:
    if not isinstance(tip, str):
        raise ConnectivityError(f"tip {tip!r} is not a hex object id")
    if len(tip) != 40:
        raise ConnectivityError(f"tip {tip!r} is not a 40-character object id")
    try:
        value = tip.encode("ascii")
    except UnicodeEncodeError:
        value = b""
    if any(c not in _HEX for c in value):
        raise ConnectivityError(f"tip {tip!r} is not a hex object id")
    return tip.lower()


class _GraphVerifier:
    """Walks and type-checks the object graph reachable from one tip."""

    def __init__(self, incoming: Mapping[str, Tuple[str, bytes]], store):
        self._incoming = dict(incoming)
        self._store = store
        self._published = set(store.published_oids())
        self._cache: Dict[str, Tuple[str, bytes]] = {}

    # -- object retrieval ------------------------------------------------------

    def _load(self, want: str, oid: str, referenced_by: str) -> Tuple[str, bytes]:
        """Return ``(type, content)`` for *oid*, enforcing membership rules.

        Only objects delivered by this pack or recorded in a committed
        manifest count.  A loose file leaked by an interrupted publish is
        not archive content; neither is an object absent or corrupt on
        disk even if the manifest mentions it.
        """
        type_name, content = self._load_any(oid, referenced_by, want=want)
        if type_name != want:
            raise ConnectivityError(
                f"{referenced_by} references {oid} as {want}, but it is a "
                f"{type_name}"
            )
        return type_name, content

    # -- tag peeling ------------------------------------------------------------

    def follow_tags(self, tip: str) -> str:
        """Resolve *tip* (commit or tag chain) to its underlying commit oid."""
        oid = tip
        chain = []
        seen = set()
        for depth in range(_TAG_DEPTH_LIMIT + 1):
            if oid in seen:
                raise ConnectivityError(
                    f"tag cycle detected: {' -> '.join(chain + [oid])}"
                )
            seen.add(oid)
            chain.append(oid)
            type_name, content = self._load_any(oid, f"tip {tip}")
            if type_name == _COMMIT:
                return oid
            if type_name != _TAG:
                raise ConnectivityError(
                    f"tip {tip} is a {type_name}, expected a commit or an "
                    "annotated tag"
                )
            target, target_type = _parse_tag(oid, content)
            if target_type not in _VALID_TYPES:
                raise ConnectivityError(
                    f"tag {oid} declares unsupported target type {target_type!r}"
                )
            if target_type != _TAG:
                # The chain can only continue through tags; validate the
                # final target's existence and declared kind here.
                actual, _ = self._load_any(target, f"tag {oid}")
                if actual != target_type:
                    raise ConnectivityError(
                        f"tag {oid} references {target} as {target_type}, but "
                        f"it is a {actual}"
                    )
            oid = target
        raise ConnectivityError(
            f"tag chain from {tip} exceeds depth {_TAG_DEPTH_LIMIT}"
        )

    def _load_any(
        self, oid: str, referenced_by: str, want: Optional[str] = None
    ) -> Tuple[str, bytes]:
        """Fetch any of the four object kinds, with membership enforcement."""
        cached = self._cache.get(oid)
        if cached is not None:
            return cached
        delivered = self._incoming.get(oid)
        if delivered is not None:
            result = delivered
        elif oid in self._published:
            try:
                result = self._store.read(oid)
            except Exception as exc:
                raise ConnectivityError(
                    f"archived object {oid} (needed by {referenced_by}) is "
                    f"not readable: {exc}"
                ) from exc
        else:
            kind = f"{want} " if want else ""
            raise ConnectivityError(
                f"missing {kind}object {oid} referenced by {referenced_by}"
            )
        self._cache[oid] = result
        return result

    # -- commit/tree graph ------------------------------------------------------

    def require_commit_graph(self, tip_commit: str) -> None:
        """Verify every commit/tree/blob reachable from *tip_commit*."""
        seen_commits = set()
        seen_trees = set()
        seen_blobs = set()
        commit_stack = [tip_commit]
        tree_stack: List[Tuple[str, str]] = []

        while commit_stack:
            oid = commit_stack.pop()
            if oid in seen_commits:
                continue
            seen_commits.add(oid)
            _, content = self._load(_COMMIT, oid, f"commit {oid}")
            tree, parents = _parse_commit(oid, content)
            self._load(_TREE, tree, f"commit {oid}")
            tree_stack.append((tree, f"commit {oid}"))
            for parent in parents:
                self._load(_COMMIT, parent, f"commit {oid}")
                commit_stack.append(parent)

        while tree_stack:
            oid, referenced_by = tree_stack.pop()
            if oid in seen_trees:
                continue
            seen_trees.add(oid)
            _, content = self._load(_TREE, oid, referenced_by)
            for mode, name, child in _parse_tree(oid, content):
                expected = _TREE_MODES.get(mode, "invalid")
                if expected is None:
                    # gitlink: a commit living in an external submodule
                    # repository.  Nothing about it must exist locally.
                    continue
                if expected == "invalid":
                    raise ConnectivityError(
                        f"tree {oid} entry {_name_repr(name)} has unsupported "
                        f"mode {mode:06o}"
                    )
                where = f"tree {oid} entry {_name_repr(name)}"
                if expected == _TREE:
                    self._load(_TREE, child, where)
                    tree_stack.append((child, where))
                else:
                    if child in seen_blobs:
                        continue
                    seen_blobs.add(child)
                    self._load(_BLOB, child, where)


# -- object parsers ------------------------------------------------------------


def _parse_commit(oid: str, content: bytes) -> Tuple[str, List[str]]:
    """Extract ``(tree_oid, parent_oids)`` from a commit object's headers."""
    sep = content.find(b"\n\n")
    headers = content if sep < 0 else content[:sep]
    tree = None
    parents: List[str] = []
    for line in headers.split(b"\n"):
        if line.startswith(b" "):
            continue  # continuation of a previous header (gpgsig, mergetag)
        key, sp, value = line.partition(b" ")
        if not sp:
            raise ConnectivityError(f"malformed commit {oid}: bad header line")
        if key == b"tree":
            tree = _oid_field(value, f"commit {oid}")
        elif key == b"parent":
            parents.append(_oid_field(value, f"commit {oid}"))
    if tree is None:
        raise ConnectivityError(f"malformed commit {oid}: missing tree header")
    if sep < 0:
        raise ConnectivityError(
            f"malformed commit {oid}: no blank line separating the message"
        )
    return tree, parents


def _parse_tag(oid: str, content: bytes) -> Tuple[str, str]:
    """Extract ``(target_oid, target_type)`` from an annotated tag object."""
    sep = content.find(b"\n\n")
    headers = content if sep < 0 else content[:sep]
    target = None
    target_type = None
    for line in headers.split(b"\n"):
        if line.startswith(b" "):
            continue
        key, sp, value = line.partition(b" ")
        if not sp:
            raise ConnectivityError(f"malformed tag {oid}: bad header line")
        if key == b"object":
            target = _oid_field(value, f"tag {oid}")
        elif key == b"type":
            target_type = value.decode("ascii", "replace")
    if target is None:
        raise ConnectivityError(f"malformed tag {oid}: missing object header")
    if target_type is None:
        raise ConnectivityError(f"malformed tag {oid}: missing type header")
    return target, target_type


def _parse_tree(oid: str, content: bytes):
    """Yield ``(mode, raw_name, child_oid)`` records from a tree object."""
    pos = 0
    n = len(content)
    while pos < n:
        sp = content.find(b" ", pos)
        if sp < 0:
            raise ConnectivityError(f"malformed tree {oid}: mode not terminated")
        mode_text = content[pos:sp]
        try:
            mode = int(mode_text, 8)
        except ValueError:
            raise ConnectivityError(
                f"malformed tree {oid}: invalid mode {mode_text!r}"
            ) from None
        nul = content.find(b"\0", sp + 1)
        if nul < 0 or nul + 20 > n:
            raise ConnectivityError(f"malformed tree {oid}: truncated entry")
        name = content[sp + 1 : nul]
        if not name:
            raise ConnectivityError(f"malformed tree {oid}: empty entry name")
        yield mode, name, content[nul + 1 : nul + 21].hex()
        pos = nul + 21


def _oid_field(value: bytes, referenced_by: str) -> str:
    if len(value) != 40 or any(c not in _HEX for c in value):
        raise ConnectivityError(
            f"{referenced_by} contains malformed object id {value!r}"
        )
    return value.decode("ascii").lower()


def _name_repr(name: bytes) -> str:
    """Show a raw tree-entry name readably, including binary bytes."""
    try:
        text = name.decode("utf-8")
    except UnicodeDecodeError:
        return repr(name)
    if text.isprintable() and "\x7f" not in text:
        return repr(text)
    return repr(name)
