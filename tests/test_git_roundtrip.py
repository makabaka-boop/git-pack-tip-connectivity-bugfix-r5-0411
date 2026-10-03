"""End-to-end tests: packs produced by git, objects verified with git cat-file.

The importer never calls git; these tests use git only as the reference
implementation to (a) generate legitimate packs and (b) independently
verify every imported object.
"""

import pytest

from pack_import import PackImporter, parse_pack
from pack_import.packfile import OBJ_OFS_DELTA, OBJ_REF_DELTA
from packtools import all_oids, cat_file_env, git, git_bytes, git_out, pack_objects
from conftest import requires_git

pytestmark = requires_git

PACK_ARGS = ("--window=10", "--depth=6")


def _verify_against_git(repo, store, oids):
    """Every imported object must be readable by git and byte-identical."""
    env = cat_file_env(store)
    for oid in sorted(oids):
        expected_type = git_out(repo, "cat-file", "-t", oid)
        assert git_out(repo, "cat-file", "-t", oid, env=env) == expected_type
        expected = git_bytes(repo, "cat-file", expected_type, oid)
        actual = git_bytes(repo, "cat-file", expected_type, oid, env=env)
        assert actual == expected, f"{oid} ({expected_type}) content mismatch"


def test_import_ofs_delta_pack(src_repo, store, importer):
    oids = all_oids(src_repo)
    pack = pack_objects(src_repo, oids, "--delta-base-offset", *PACK_ARGS)

    parsed = parse_pack(pack)  # sanity: the pack really exercises ofs deltas
    assert any(e.type == OBJ_OFS_DELTA for e in parsed.entries)
    assert len(parsed.entries) == len(oids)

    record = importer.import_pack(pack)
    assert {o["oid"] for o in record["objects"]} == oids
    _verify_against_git(src_repo, store, oids)


def test_import_ref_delta_pack(src_repo, store, importer):
    oids = all_oids(src_repo)
    pack = pack_objects(src_repo, oids, *PACK_ARGS)  # default: REF_DELTA

    parsed = parse_pack(pack)
    assert any(e.type == OBJ_REF_DELTA for e in parsed.entries)

    record = importer.import_pack(pack)
    assert {o["oid"] for o in record["objects"]} == oids
    _verify_against_git(src_repo, store, oids)


def test_import_thin_pack_with_external_bases(tmp_path, store, importer):
    """A --thin pack carries ref-deltas against objects we already have."""
    from packtools import make_repo

    repo = make_repo(tmp_path / "thin-src")
    base_commit = git_out(repo, "rev-parse", "HEAD~1")
    tip_commit = git_out(repo, "rev-parse", "HEAD")

    base_oids = set(git_out(repo, "rev-list", "--objects", base_commit).split())
    base_oids = {
        line.split()[0]
        for line in git_out(repo, "rev-list", "--objects", base_commit).splitlines()
    }
    importer.import_pack(pack_objects(repo, base_oids, *PACK_ARGS))

    # thin pack: everything in tip but not in base, delta'd against base
    thin = git_bytes(
        repo,
        "pack-objects",
        "--stdout",
        "--thin",
        "-q",
        "--revs",
        input=f"{tip_commit}\n^{base_commit}\n",
    )
    parsed = parse_pack(thin)
    record = importer.import_pack(thin)
    imported = {o["oid"] for o in record["objects"]}

    # at least one delta must reference a base outside the pack (the "thin" part)
    external = [
        e
        for e in parsed.entries
        if e.type == OBJ_REF_DELTA and e.base_oid not in imported
    ]
    assert external, "thin pack contained no external ref-delta"
    for e in external:
        assert store.has(e.base_oid)

    _verify_against_git(repo, store, imported)


def test_git_fsck_accepts_imported_objects(src_repo, store, importer, tmp_path):
    """Imported loose objects must satisfy git's own consistency checks."""
    oids = all_oids(src_repo)
    importer.import_pack(
        pack_objects(src_repo, oids, "--delta-base-offset", *PACK_ARGS)
    )

    # a real repo whose object store is the import target
    clone = tmp_path / "clone"
    git(tmp_path, "init", "-q", str(clone))
    env = cat_file_env(store)
    for oid in oids:
        # reading every object through git validates the loose format
        git(clone, "cat-file", "-e", oid, env=env)
    # fsck over the imported object dir (reachable from the imported tips)
    refs = git_out(src_repo, "for-each-ref", "--format=%(objectname)").splitlines()
    result = git(clone, "fsck", "--strict", *refs, env=env)
    assert result.returncode == 0
