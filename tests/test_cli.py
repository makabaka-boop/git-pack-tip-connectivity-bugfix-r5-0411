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
