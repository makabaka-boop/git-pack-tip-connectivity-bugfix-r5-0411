"""Verifying importer for Git PACK v2 files (pure Python, no index-pack)."""

from .errors import (
    ChecksumError,
    DeltaChainCycleError,
    DeltaChainError,
    DeltaChainTooDeepError,
    LimitExceededError,
    MissingBaseError,
    ObjectTooLargeError,
    PackFormatError,
    PackImportError,
)
from .importer import PackImporter
from .packfile import (
    MAX_DELTA_DEPTH,
    MAX_OBJECT_SIZE,
    MAX_OBJECTS,
    MAX_PACK_SIZE,
    DeltaResolver,
    apply_delta_program,
    compute_oid,
    parse_pack,
)
from .store import ObjectStore, StagedImport

__all__ = [
    "PackImporter",
    "ObjectStore",
    "StagedImport",
    "parse_pack",
    "apply_delta_program",
    "compute_oid",
    "DeltaResolver",
    "MAX_PACK_SIZE",
    "MAX_OBJECTS",
    "MAX_OBJECT_SIZE",
    "MAX_DELTA_DEPTH",
    "PackImportError",
    "LimitExceededError",
    "ObjectTooLargeError",
    "PackFormatError",
    "ChecksumError",
    "MissingBaseError",
    "DeltaChainError",
    "DeltaChainTooDeepError",
    "DeltaChainCycleError",
]
