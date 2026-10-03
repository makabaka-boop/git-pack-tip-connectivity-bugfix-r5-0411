"""Parser, delta and limit tests using hand-built packs."""

import zlib

import pytest

from pack_import import (
    ChecksumError,
    DeltaChainCycleError,
    DeltaChainTooDeepError,
    LimitExceededError,
    MissingBaseError,
    ObjectTooLargeError,
    PackFormatError,
    PackImporter,
    parse_pack,
)
from pack_import.packfile import (
    MAX_OBJECT_SIZE,
    MAX_OBJECTS,
    MAX_PACK_SIZE,
    OBJ_BLOB,
    OBJ_OFS_DELTA,
    OBJ_REF_DELTA,
    DeltaResolver,
    PackEntry,
)
from packtools import (
    PackBuilder,
    blob_oid,
    insert_delta,
    make_delta,
    op_copy,
    op_insert,
)


def test_valid_pack_roundtrip(store, importer):
    b = PackBuilder()
    b.add_blob(b"hello world")
    base = b"hello world"
    target = b"hello brave new world"
    b.add(OBJ_OFS_DELTA, insert_delta(len(base), target), base_index=0)
    b.add(OBJ_REF_DELTA, insert_delta(len(target), b"done"), base_oid=blob_oid(target))
    record = importer.import_pack(b.build())

    assert record["object_count"] == 3
    by_oid = {o["oid"]: o for o in record["objects"]}
    assert by_oid[blob_oid(b"hello world")]["type"] == "blob"
    assert by_oid[blob_oid(target)]["size"] == len(target)
    assert by_oid[blob_oid(b"done")]["size"] == 4
    # everything landed in the published object dir
    for oid in by_oid:
        assert store.has(oid)
        assert store.read(oid)[0] == "blob"


def test_empty_pack(store, importer):
    record = importer.import_pack(PackBuilder().build())
    assert record["object_count"] == 0
    assert store.read_manifest()["imports"][-1]["id"] == record["id"]


# --- envelope verification -------------------------------------------------------


def test_tampered_trailer_rejected(importer):
    pack = bytearray(PackBuilder().build())
    pack[-1] ^= 0x01  # flip one bit in the checksum
    with pytest.raises(ChecksumError):
        importer.import_pack(bytes(pack))


def test_tampered_body_rejected(importer):
    b = PackBuilder()
    b.add_blob(b"x" * 100)
    pack = bytearray(b.build())
    pack[20] ^= 0x40  # flip a bit inside the object stream
    with pytest.raises(ChecksumError):
        importer.import_pack(bytes(pack))


def test_truncated_pack_rejected(importer):
    pack = PackBuilder().build()
    with pytest.raises(PackFormatError):
        importer.import_pack(pack[:-10])  # checksum no longer matches/aligns


def test_bad_signature_and_version(importer):
    b = PackBuilder()
    b.add_blob(b"data")
    pack = bytearray(b.build())
    pack[0:4] = b"PAKC"
    with pytest.raises(PackFormatError):
        importer.import_pack(bytes(pack))

    b2 = PackBuilder()
    b2.add_blob(b"data")
    pack2 = bytearray(b2.build())
    pack2[7] = 3  # version 3
    with pytest.raises(PackFormatError):
        importer.import_pack(bytes(pack2))


def test_reserved_object_type_rejected(importer):
    b = PackBuilder()
    b.add(5, b"nonsense")  # type 5 is reserved
    with pytest.raises(PackFormatError, match="reserved"):
        importer.import_pack(b.build())


def test_declared_size_mismatch_rejected(importer):
    b = PackBuilder()
    # header claims 10 bytes, zlib stream really holds 4
    b.add_blob(b"1234", declared_size=10)
    with pytest.raises(PackFormatError, match="declared size"):
        importer.import_pack(b.build())


def test_truncated_zlib_stream_rejected(importer):
    b = PackBuilder()
    payload = zlib.compress(b"a" * 500)
    b.add(OBJ_BLOB, b"", raw_payload=payload[:-5], declared_size=500)
    with pytest.raises(PackFormatError, match="truncated zlib"):
        importer.import_pack(b.build())


def test_zlib_garbage_rejected(importer):
    b = PackBuilder()
    b.add(OBJ_BLOB, b"", raw_payload=b"\x78\x9c not a real stream", declared_size=3)
    with pytest.raises(PackFormatError, match="zlib"):
        importer.import_pack(b.build())


def test_stream_boundary_desync_rejected(importer):
    b = PackBuilder()
    b.add_blob(b"first")
    b.add_raw_bytes(b"\x00")  # garbage byte between objects
    b.add_blob(b"second")
    with pytest.raises(PackFormatError):
        importer.import_pack(b.build())


def test_object_count_mismatch_rejected(importer):
    b = PackBuilder()
    b.add_blob(b"only one")
    # claim there are two objects: the second "header" would eat checksum bytes
    with pytest.raises(PackFormatError):
        importer.import_pack(b.build(count_override=2))


# --- limits -----------------------------------------------------------------


def test_too_many_objects_rejected(importer):
    b = PackBuilder()
    b.add_blob(b"x")
    pack = b.build(count_override=MAX_OBJECTS + 1)
    with pytest.raises(LimitExceededError):
        importer.import_pack(pack)


def test_oversized_pack_rejected(importer, tmp_path):
    big = tmp_path / "big.pack"
    big.write_bytes(b"\0" * (MAX_PACK_SIZE + 1))
    with pytest.raises(LimitExceededError):
        importer.import_pack(big)
    with pytest.raises(LimitExceededError):
        importer.import_pack(big.read_bytes())


def test_declared_size_over_limit_rejected(importer):
    b = PackBuilder()
    b.add_blob(b"small", declared_size=MAX_OBJECT_SIZE + 1)
    with pytest.raises(ObjectTooLargeError):
        importer.import_pack(b.build())


def test_exactly_max_size_accepted(store, importer):
    content = b"\0" * MAX_OBJECT_SIZE  # compresses to almost nothing
    b = PackBuilder()
    b.add_blob(content)
    record = importer.import_pack(b.build())
    assert record["objects"][0]["size"] == MAX_OBJECT_SIZE
    assert store.read(blob_oid(content))[1] == content


def test_delta_target_over_limit_rejected(importer):
    base = bytes(600_000)
    b = PackBuilder()
    b.add_blob(base)
    # target 1.2 MB built from two copies of the base
    program = make_delta(
        len(base), 1_200_000, op_copy(0, len(base)) + op_copy(0, len(base))
    )
    b.add(OBJ_OFS_DELTA, program, base_index=0)
    with pytest.raises(ObjectTooLargeError):
        importer.import_pack(b.build())


# --- malicious delta programs -----------------------------------------------


def _two_entry_pack(program, base=b"base content"):
    b = PackBuilder()
    b.add_blob(base)
    b.add(OBJ_OFS_DELTA, program, base_index=0)
    return b.build()


def test_delta_copy_out_of_range(importer):
    base = b"base content"
    program = make_delta(len(base), 4, op_copy(len(base) - 2, 4))  # runs past base
    with pytest.raises(PackFormatError, match="copy range"):
        importer.import_pack(_two_entry_pack(program, base))


def test_delta_copy_far_beyond_base(importer):
    base = b"base content"
    program = make_delta(len(base), 4, op_copy(1 << 20, 4))
    with pytest.raises(PackFormatError, match="copy range"):
        importer.import_pack(_two_entry_pack(program, base))


def test_delta_source_size_mismatch(importer):
    base = b"base content"
    program = make_delta(len(base) + 1, 3, op_insert(b"abc"))
    with pytest.raises(PackFormatError, match="source size"):
        importer.import_pack(_two_entry_pack(program, base))


def test_delta_literal_overrun(importer):
    base = b"base content"
    # insert command promises 100 bytes but the program ends immediately
    program = make_delta(len(base), 100, bytes([100]) + b"short")
    with pytest.raises(PackFormatError, match="truncated"):
        importer.import_pack(_two_entry_pack(program, base))


def test_delta_command_zero_rejected(importer):
    base = b"base content"
    program = make_delta(len(base), 1, b"\x00")
    with pytest.raises(PackFormatError, match="command 0"):
        importer.import_pack(_two_entry_pack(program, base))


def test_delta_output_exceeds_target(importer):
    base = b"base content"
    program = make_delta(len(base), 3, op_insert(b"ten bytes long"))
    with pytest.raises(PackFormatError, match="target size"):
        importer.import_pack(_two_entry_pack(program, base))


def test_delta_output_short_of_target(importer):
    base = b"base content"
    program = make_delta(len(base), 100, op_insert(b"abc"))
    with pytest.raises(PackFormatError, match="target size"):
        importer.import_pack(_two_entry_pack(program, base))


def test_delta_copy_command_truncated(importer):
    base = b"base content"
    program = make_delta(len(base), 4, bytes([0x80 | 0x01 | 0x10]))  # flags, no args
    with pytest.raises(PackFormatError, match="truncated"):
        importer.import_pack(_two_entry_pack(program, base))


# --- delta chains ---------------------------------------------------------------


def _chain_pack(depth):
    """Base blob plus *depth* ofs-deltas, each on the previous entry."""
    b = PackBuilder()
    b.add_blob(b"v0")
    prev = b"v0"
    for i in range(1, depth + 1):
        content = f"v{i}".encode()
        b.add(OBJ_OFS_DELTA, insert_delta(len(prev), content), base_index=i - 1)
        prev = content
    return b.build(), prev


def test_delta_chain_at_depth_limit_accepted(store, importer):
    pack, final = _chain_pack(8)  # exactly 8 layers: allowed
    record = importer.import_pack(pack)
    assert store.read(blob_oid(final))[1] == final
    assert record["object_count"] == 9


def test_delta_chain_beyond_limit_rejected(importer):
    pack, _ = _chain_pack(9)  # 9 layers: one too many
    with pytest.raises(DeltaChainTooDeepError):
        importer.import_pack(pack)


def test_missing_ref_delta_base_rejected(importer):
    b = PackBuilder()
    b.add(OBJ_REF_DELTA, insert_delta(3, b"new"), base_oid=blob_oid(b"not in pack"))
    with pytest.raises(MissingBaseError):
        importer.import_pack(b.build())


def test_ref_delta_cycle_rejected(importer):
    # A's base is the object B produces and vice versa: unresolvable.
    content_a, content_b = b"content-a", b"content-b"
    b = PackBuilder()
    b.add(OBJ_REF_DELTA, insert_delta(9, content_a), base_oid=blob_oid(content_b))
    b.add(OBJ_REF_DELTA, insert_delta(9, content_b), base_oid=blob_oid(content_a))
    with pytest.raises(MissingBaseError):
        importer.import_pack(b.build())


def test_resolver_detects_direct_cycle():
    # The parser can never produce this (ofs offsets point backwards), so
    # exercise the resolver's cycle guard with hand-made entries.
    a = PackEntry(
        offset=100, type=OBJ_OFS_DELTA, declared_size=1, payload=b"", base_offset=200
    )
    c = PackEntry(
        offset=200, type=OBJ_OFS_DELTA, declared_size=1, payload=b"", base_offset=100
    )
    resolver = DeltaResolver([a, c])
    with pytest.raises(DeltaChainCycleError):
        resolver.resolve_all()


# --- ofs-delta offset validation --------------------------------------------


def test_ofs_delta_zero_offset_rejected(importer):
    b = PackBuilder()
    b.add_blob(b"base")
    b._begin()
    import packtools

    b._body += packtools.encode_obj_header(OBJ_OFS_DELTA, 1) + b"\x00"
    b._body += zlib.compress(b"x")
    with pytest.raises(PackFormatError, match="zero"):
        importer.import_pack(b.build())


def test_ofs_delta_into_middle_of_object_rejected(importer):
    b = PackBuilder()
    b.add_blob(b"base")
    b._begin()
    import packtools

    # offset 3 lands inside the first object's header, not on an object start
    b._body += packtools.encode_obj_header(OBJ_OFS_DELTA, 1) + packtools.encode_ofs(3)
    b._body += zlib.compress(b"x")
    with pytest.raises(PackFormatError, match="preceding object"):
        importer.import_pack(b.build())


def test_ofs_delta_before_pack_start_rejected(importer):
    b = PackBuilder()
    b.add_blob(b"base")
    b._begin()
    import packtools

    b._body += packtools.encode_obj_header(OBJ_OFS_DELTA, 1) + packtools.encode_ofs(
        10_000
    )
    b._body += zlib.compress(b"x")
    with pytest.raises(PackFormatError):
        importer.import_pack(b.build())


# --- external bases ----------------------------------------------------------


def test_ref_delta_against_store_object(store, importer):
    base = b"external base object " * 40
    base_id = blob_oid(base)
    # seed the store with the base object via a first import
    seed = PackBuilder()
    seed.add_blob(base)
    importer.import_pack(seed.build())

    b = PackBuilder()
    target = b"derived from external base"
    b.add(OBJ_REF_DELTA, insert_delta(len(base), target), base_oid=base_id)
    record = importer.import_pack(b.build())
    assert record["objects"][0]["oid"] == blob_oid(target)
    assert store.read(blob_oid(target)) == ("blob", target)


# --- implementation constraints --------------------------------------------


def test_no_git_subprocess_in_implementation():
    """The importer must parse packs itself: no subprocess, no index-pack."""
    import ast

    import pack_import

    for module in ("packfile", "store", "importer", "cli", "errors"):
        path = pack_import.__path__[0] + f"/{module}.py"
        tree = ast.parse(open(path).read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names]
                root = getattr(node, "module", "") or ""
                assert "subprocess" not in names and "subprocess" not in root
                assert "pty" not in names
            if isinstance(node, ast.Call):
                func = node.func
                name = ""
                while isinstance(func, ast.Attribute):
                    name = "." + func.attr + name
                    func = func.value
                if isinstance(func, ast.Name):
                    name = func.id + name
                assert "system" not in name and "popen" not in name.lower(), name
