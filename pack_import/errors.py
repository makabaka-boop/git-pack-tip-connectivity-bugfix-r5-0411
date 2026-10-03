"""Exception hierarchy for :mod:`pack_import`.

Every error raised during parsing, verification or resolution derives from
:class:`PackImportError`.  Raising any of them aborts the *entire* batch:
no object is published and the previously published manifest stays intact.
"""


class PackImportError(Exception):
    """Base class for all pack import failures."""


class LimitExceededError(PackImportError):
    """A hard resource limit was exceeded (pack size, object count, ...)."""


class ObjectTooLargeError(LimitExceededError):
    """A single (reconstructed) object exceeds the per-object size limit."""


class PackFormatError(PackImportError):
    """The pack byte stream is malformed or fails verification."""


class ChecksumError(PackFormatError):
    """The trailing SHA-1 does not match the pack contents."""


class ConnectivityError(PackFormatError):
    """A tip's reachable object graph is incomplete or mistyped.

    Raised for ``--tip`` imports when a reachable commit, tree, blob or tag
    is missing from both the incoming pack and the published archive, when
    a reference points at the wrong object kind (tree entry at a blob,
    commit parent at a tree, ...), or when a tip is neither a commit nor a
    tag chain that ends at a commit.
    """


class MissingBaseError(PackImportError):
    """A delta base object is neither in the pack nor in the object store."""


class DeltaChainError(PackImportError):
    """Generic delta-chain failure (see the more specific subclasses)."""


class DeltaChainTooDeepError(DeltaChainError):
    """The delta chain leading to an object is deeper than allowed."""


class DeltaChainCycleError(DeltaChainError):
    """The delta dependency graph contains a cycle."""
