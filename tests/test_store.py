"""Staging, atomic publication, cancellation and crash-recovery tests."""

import json
import os
import zlib

import pytest

from pack_import import ObjectStore, PackImporter
from packtools import PackBuilder, blob_oid, insert_delta
from pack_import.packfile import OBJ_OFS_DELTA


def _sample_pack():
    b = PackBuilder()
    b.add_blob(b"alpha")
    b.add(OBJ_OFS_DELTA, insert_delta(5, b"beta!"), base_index=0)
    return b.build()


def _second_pack():
    b = PackBuilder()
    b.add_blob(b"gamma")
    return b.build()


def test_objects_quarantined_until_publish(store, importer):
    staged = importer.stage_pack(_sample_pack())

    # staged: quarantine dir holds the objects, nothing is published
    assert staged.directory.is_dir()
    staged_files = list((staged.directory / "objects").rglob("*"))
    assert any(p.is_file() for p in staged_files)
    assert store.read_manifest() == {"version": 1, "imports": []}
    assert list(store.objects_dir.iterdir()) == []

    store.publish(staged)

    # published: objects moved, manifest lists them, staging is gone
    manifest = store.read_manifest()
    assert [i["id"] for i in manifest["imports"]] == [staged.token]
    assert not staged.directory.exists()
    for obj in manifest["imports"][0]["objects"]:
        assert store.has(obj["oid"])


def test_abort_discards_staging_and_keeps_old_manifest(store, importer):
    importer.import_pack(_sample_pack())
    before = store.read_manifest()

    staged = importer.stage_pack(_second_pack())
    store.abort(staged)

    assert not staged.directory.exists()
    assert store.read_manifest() == before  # old manifest still readable
    assert not store.has(blob_oid(b"gamma"))  # nothing leaked into objects/


def test_crash_before_publish_then_cleanup(store, importer, tmp_path):
    importer.import_pack(_sample_pack())
    before = store.read_manifest()

    staged = importer.stage_pack(_second_pack())
    token_dir = staged.directory
    # simulate a crash: no publish, no abort, process "dies" here

    survivor = ObjectStore(store.root)  # a fresh process sees the old state
    assert survivor.read_manifest() == before
    assert token_dir.is_dir()  # quarantine left behind
    assert not survivor.has(blob_oid(b"gamma"))

    survivor.cleanup_staging()
    assert not token_dir.exists()
    assert survivor.read_manifest() == before

    # and the store is fully usable afterwards
    importer2 = PackImporter(survivor)
    importer2.import_pack(_second_pack())
    assert survivor.has(blob_oid(b"gamma"))


def test_crash_during_publish_keeps_old_manifest(store, importer, monkeypatch):
    importer.import_pack(_sample_pack())
    before = store.read_manifest()

    staged = importer.stage_pack(_second_pack())
    real_replace = os.replace

    def bomb(src, dst):
        if os.fspath(dst).endswith("manifest.json"):
            raise OSError("simulated crash at commit point")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", bomb)
    with pytest.raises(OSError, match="simulated crash"):
        store.publish(staged)
    monkeypatch.undo()

    # the commit point was never reached: old manifest is authoritative
    assert store.read_manifest() == before
    # leftover quarantine can be cleaned; re-import then succeeds
    store.cleanup_staging()
    importer.import_pack(_second_pack())
    assert store.has(blob_oid(b"gamma"))
    assert len(store.read_manifest()["imports"]) == 2


def test_failed_import_stages_nothing(store, importer):
    bad = bytearray(_sample_pack())
    bad[-1] ^= 0x01
    with pytest.raises(Exception):
        importer.import_pack(bytes(bad))
    assert list(store.staging_dir.iterdir()) == []
    assert store.read_manifest()["imports"] == []


def test_manifest_is_written_atomically(store, importer):
    importer.import_pack(_sample_pack())
    # no scratch file remains next to the committed manifest
    assert not (store.root / "manifest.json.tmp").exists()
    manifest = json.loads((store.root / "manifest.json").read_text())
    assert manifest["imports"][0]["object_count"] == 2


def test_sequential_imports_accumulate_manifest(store, importer):
    importer.import_pack(_sample_pack())
    importer.import_pack(_second_pack())
    manifest = store.read_manifest()
    assert len(manifest["imports"]) == 2
    oids = {o["oid"] for i in manifest["imports"] for o in i["objects"]}
    assert blob_oid(b"alpha") in oids and blob_oid(b"gamma") in oids


def test_reimport_same_pack_is_idempotent(store, importer):
    pack = _sample_pack()
    importer.import_pack(pack)
    importer.import_pack(pack)  # objects already present: fine
    assert len(store.read_manifest()["imports"]) == 2
    assert store.has(blob_oid(b"alpha"))


def test_staged_loose_object_format(store, importer):
    importer.import_pack(_sample_pack())
    oid = blob_oid(b"alpha")
    raw = zlib.decompress(store.loose_path(oid).read_bytes())
    assert raw == b"blob 5\0alpha"


def test_staged_objects_not_visible_to_base_provider(store, importer):
    # a delta against an object that only exists in an unpublished staging
    # area must not resolve: bases come from the *published* store only
    staged = importer.stage_pack(_sample_pack())
    assert not store.has(blob_oid(b"alpha"))
    store.abort(staged)
