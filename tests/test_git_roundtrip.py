"""End-to-end tests: packs produced by git, objects verified with git cat-file.

The importer never calls git; these tests use git only as the reference
implementation to (a) generate legitimate packs and (b) independently
verify every imported object.
"""

import pytest

from pack_import import ObjectStore, PackImporter, parse_pack
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


# --- named-tip delivery -------------------------------------------------------


def _reachable_oids(repo, *tips):
    oids = set()
    for tip in tips:
        out = git_bytes(repo, "rev-list", "--objects", tip)
        for line in out.splitlines():
            if line:
                oids.add(line.split()[0].decode())
        oids.add(tip)
    return oids


def _make_history_repo(path):
    """Repo with two branches, a merge commit, a tag and a submodule."""
    import os

    repo = path / "repo"
    sub = path / "sub"
    for r in (repo, sub):
        git(r.parent, "init", "-q", str(r.name))
        git(r, "config", "user.name", "T")
        git(r, "config", "user.email", "t@e.com")
    (sub / "sub.txt").write_text("submodule\n")
    git(sub, "add", "-A")
    git(sub, "commit", "-q", "-m", "sub")
    sub_head = git_out(sub, "rev-parse", "HEAD")

    (repo / "main.txt").write_text("main 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c1")
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.txt").write_text("feature\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feature")
    feature_head = git_out(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "master")
    (repo / "main.txt").write_text("main 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "c2")
    git(repo, "merge", "-q", "--no-ff", "feature", "-m", "merge")
    merge_head = git_out(repo, "rev-parse", "HEAD")

    # binary-named file and a symlink in the tree
    open(os.path.join(repo, os.fsdecode(b"\xff-name")), "wb").write(b"\x00bin")
    os.symlink("main.txt", os.path.join(repo, "lnk"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "binary and symlink")
    head = git_out(repo, "rev-parse", "HEAD")

    git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(sub),
        "vendor",
    )
    git(repo, "commit", "-q", "-m", "submodule")
    tip = git_out(repo, "rev-parse", "HEAD")
    git(repo, "tag", "-a", "-m", "release", "rel")
    tag = git_out(repo, "rev-parse", "rel")
    return {
        "repo": repo,
        "tip": tip,
        "head": head,
        "merge": merge_head,
        "feature": feature_head,
        "tag": tag,
        "sub_head": sub_head,
    }


def test_tip_full_history_with_tag_merge_submodule(tmp_path, store, importer):
    h = _make_history_repo(tmp_path)
    repo = h["repo"]
    oids = _reachable_oids(repo, h["tip"], h["tag"])
    pack = pack_objects(repo, oids, "--delta-base-offset", *PACK_ARGS)

    for tip in (h["tip"], h["tag"]):
        s = ObjectStore(tmp_path / f"store-{tip[:6]}")
        rec = PackImporter(s).import_pack(pack, tips=[tip])
        assert rec["tips"] == [tip]

        clone = tmp_path / f"clone-{tip[:6]}"
        git(tmp_path, "init", "-q", str(clone))
        result = git(clone, "fsck", "--strict", tip, env=cat_file_env(s))
        assert result.returncode == 0, result.stderr

    # the external submodule commit must not have been demanded
    assert not ObjectStore(tmp_path / f"store-{h['tip'][:6]}").has(h["sub_head"])


def test_tip_split_pack_shared_archived_objects(tmp_path, store, importer):
    """First import delivers the merge; a second pack delivers only the new tip."""
    h = _make_history_repo(tmp_path)
    repo = h["repo"]

    base_oids = _reachable_oids(repo, h["merge"])
    importer.import_pack(
        pack_objects(repo, base_oids, "--delta-base-offset", *PACK_ARGS),
        tips=[h["merge"]],
    )

    new_oids = _reachable_oids(repo, h["tip"]) - base_oids
    # the second pack carries only new objects, but tip needs the old graph too
    rec = importer.import_pack(
        pack_objects(repo, new_oids, "--delta-base-offset", *PACK_ARGS),
        tips=[h["tip"]],
    )
    assert h["merge"] not in {o["oid"] for o in rec["objects"]}
    assert len(store.read_manifest()["imports"]) == 2

    clone = tmp_path / "clone"
    git(tmp_path, "init", "-q", str(clone))
    assert git(clone, "fsck", "--strict", h["tip"], env=cat_file_env(store)).returncode == 0


def test_tip_with_missing_descendant_is_atomic(tmp_path, store, importer):
    """A tip import whose pack drops a blob publishes nothing."""
    h = _make_history_repo(tmp_path)
    repo = h["repo"]
    oids = _reachable_oids(repo, h["merge"])
    dropped = git_out(repo, "rev-parse", f"{h['merge']}:main.txt")
    oids.discard(dropped)

    from pack_import import PackFormatError

    with pytest.raises(PackFormatError, match="missing"):
        importer.import_pack(
            pack_objects(repo, oids, "--delta-base-offset", *PACK_ARGS),
            tips=[h["merge"]],
        )
    assert store.read_manifest()["imports"] == []
    assert not [p for p in store.objects_dir.rglob("*") if p.is_file()]
    assert list(store.staging_dir.iterdir()) == []


def test_tip_pack_missing_side_branch_rejected(tmp_path, store, importer):
    h = _make_history_repo(tmp_path)
    repo = h["repo"]
    oids = _reachable_oids(repo, h["merge"])
    side = _reachable_oids(repo, h["feature"])
    oids -= side
    oids.add(h["merge"])  # the merge object itself is present, its ancestry is not

    from pack_import import PackFormatError

    with pytest.raises(PackFormatError, match="missing"):
        importer.import_pack(
            pack_objects(repo, oids, "--delta-base-offset", *PACK_ARGS),
            tips=[h["merge"]],
        )
    assert store.read_manifest()["imports"] == []
