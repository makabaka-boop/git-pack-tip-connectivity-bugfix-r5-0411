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


class MissingBaseError(PackImportError):
    """A delta base object is neither in the pack nor in the object store."""


class DeltaChainError(PackImportError):
    """Generic delta-chain failure (see the more specific subclasses)."""


class DeltaChainTooDeepError(DeltaChainError):
    """The delta chain leading to an object is deeper than allowed."""


class DeltaChainCycleError(DeltaChainError):
    """The delta dependency graph contains a cycle."""
