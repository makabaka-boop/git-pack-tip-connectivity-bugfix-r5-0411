"""CLI smoke tests."""

import json
import subprocess
import sys

from packtools import PackBuilder, pack_objects
from conftest import requires_git


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "pack_import", *args],
        capture_output=True,
        text=True,
    )


def test_cli_import_manifest_cleanup(tmp_path):
    store = tmp_path / "store"
    pack_file = tmp_path / "ok.pack"
    b = PackBuilder()
    b.add_blob(b"via cli")
    pack_file.write_bytes(b.build())

    r = run_cli("--store", str(store), "import", str(pack_file))
    assert r.returncode == 0, r.stderr
    record = json.loads(r.stdout)
    assert record["object_count"] == 1

    r = run_cli("--store", str(store), "manifest")
    assert r.returncode == 0
    manifest = json.loads(r.stdout)
    assert [i["id"] for i in manifest["imports"]] == [record["id"]]

    r = run_cli("--store", str(store), "cleanup")
    assert r.returncode == 0


def test_cli_rejects_tampered_pack(tmp_path):
    store = tmp_path / "store"
    pack_file = tmp_path / "bad.pack"
    b = PackBuilder()
    b.add_blob(b"tampered")
    pack_file.write_bytes(b.build(corrupt_checksum=True))

    r = run_cli("--store", str(store), "import", str(pack_file))
    assert r.returncode == 1
    assert "checksum" in r.stderr.lower()
    # failed import published nothing
    r = run_cli("--store", str(store), "manifest")
    assert json.loads(r.stdout)["imports"] == []


def test_cli_no_publish_stages_only(tmp_path):
    store = tmp_path / "store"
    pack_file = tmp_path / "staged.pack"
    b = PackBuilder()
    b.add_blob(b"staged only")
    pack_file.write_bytes(b.build())

    r = run_cli("--store", str(store), "import", "--no-publish", str(pack_file))
    assert r.returncode == 0, r.stderr
    assert (
        json.loads(run_cli("--store", str(store), "manifest").stdout)["imports"] == []
    )
    assert list((store / "staging").iterdir())  # quarantine left in place

    run_cli("--store", str(store), "cleanup")
    assert list((store / "staging").iterdir()) == []


@requires_git
def test_cli_import_git_pack(tmp_path, src_repo):
    from packtools import all_oids

    store = tmp_path / "store"
    pack_file = tmp_path / "git.pack"
    oids = all_oids(src_repo)
    pack_file.write_bytes(pack_objects(src_repo, oids, "--delta-base-offset"))

    r = run_cli("--store", str(store), "import", str(pack_file))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["object_count"] == len(oids)


def _commit_pack(tmp_path):
    """A minimal valid commit graph pack, via raw object construction."""
    from pack_import.packfile import OBJ_BLOB, OBJ_COMMIT, OBJ_TREE, compute_oid

    blob = b"cli tip\n"
    b_oid = compute_oid("blob", blob)
    tree = b"100644 f\0" + bytes.fromhex(b_oid)
    t_oid = compute_oid("tree", tree)
    commit = (
        f"tree {t_oid}\n"
        "author A <a@b.c> 1 +0000\n"
        "committer A <a@b.c> 1 +0000\n\n"
        "m\n"
    ).encode()
    c_oid = compute_oid("commit", commit)

    b = PackBuilder()
    b.add(OBJ_COMMIT, commit)
    b.add(OBJ_TREE, tree)
    b.add(OBJ_BLOB, blob)
    pack_file = tmp_path / "commit.pack"
    pack_file.write_bytes(b.build())
    return pack_file, c_oid, b_oid


def test_cli_import_with_tip_records_it(tmp_path):
    store = tmp_path / "store"
    pack_file, c_oid, _ = _commit_pack(tmp_path)

    r = run_cli("--store", str(store), "import", "--tip", c_oid, str(pack_file))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["tips"] == [c_oid]
    assert json.loads(run_cli("--store", str(store), "manifest").stdout)["imports"]


def test_cli_tip_missing_descendant_publishes_nothing(tmp_path):
    from pack_import.packfile import OBJ_COMMIT, OBJ_TREE, compute_oid

    store = tmp_path / "store"
    # commit+tree reference a blob that is not carried in the pack
    ghost = "a" * 40
    tree = b"100644 f\0" + bytes.fromhex(ghost)
    t_oid = compute_oid("tree", tree)
    commit = (
        f"tree {t_oid}\n"
        "author A <a@b.c> 1 +0000\n"
        "committer A <a@b.c> 1 +0000\n\n"
        "m\n"
    ).encode()
    c_oid = compute_oid("commit", commit)

    b = PackBuilder()
    b.add(OBJ_COMMIT, commit)
    b.add(OBJ_TREE, tree)
    pack_file = tmp_path / "bad-tip.pack"
    pack_file.write_bytes(b.build())

    r = run_cli("--store", str(store), "import", "--tip", c_oid, str(pack_file))
    assert r.returncode == 1
    assert "missing" in r.stderr
    assert json.loads(run_cli("--store", str(store), "manifest").stdout)["imports"] == []
    assert list((store / "objects").rglob("*")) == []
