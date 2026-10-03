"""Tests for --tip connectivity verification (complete commit-graph delivery).

These tests cover, without git, hand-built commit/tag/tree/blob objects and
the failure contracts: missing descendants, kind/use mismatches, symlinks,
gitlinks (external submodules), binary entry names, tag chains, merge
commits, sharing objects with previous successful imports, and loose
objects leaked by a failed publish not counting as archive content.
"""

import struct
import zlib

import pytest

from pack_import import ConnectivityError, ObjectStore, PackImporter
from pack_import.packfile import compute_oid
from packtools import PackBuilder


# -- object construction ------------------------------------------------------


def obj(type_name, content):
    return compute_oid(type_name, content), content


def tree_entry(mode, name, oid):
    if isinstance(name, str):
        name = name.encode()
    return f"{mode:o} ".encode() + name + b"\0" + bytes.fromhex(oid)


def tree_content(entries):
    return b"".join(tree_entry(mode, name, oid) for mode, name, oid in entries)


def commit_content(tree, parents=(), message="hello\n"):
    lines = [f"tree {tree}"]
    lines += [f"author A <a@example.com> 1 +0000"]
    for p in parents:
        lines.insert(1, f"parent {p}")
    lines.append(f"committer A <a@example.com> 1 +0000")
    return ("\n".join(lines) + "\n\n" + message).encode()


def tag_content(target, target_type="commit", name="v1"):
    return (
        f"object {target}\n"
        f"type {target_type}\n"
        f"tag {name}\n"
        f"tagger A <a@example.com> 1 +0000\n\n"
        f"tag message\n"
    ).encode()


def loose_write(store, type_name, content):
    """Bypass the manifest: plant a loose object straight into objects/."""
    oid = compute_oid(type_name, content)
    path = store.loose_path(oid)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"{type_name} {len(content)}\0".encode("ascii")
    path.write_bytes(zlib.compress(header + content))
    return oid


# -- successful graphs --------------------------------------------------------


def test_tip_import_simple_commit(store, importer):
    blob_id, blob = obj("blob", b"hello\n")
    tree_id, tree = obj("tree", tree_content([(0o100644, "hello.txt", blob_id)]))
    commit_id, commit = obj("commit", commit_content(tree_id))

    b = PackBuilder()
    b.add(1, commit)
    b.add(2, tree)
    b.add_blob(blob)
    record = importer.import_pack(b.build(), tips=[commit_id])

    assert record["tips"] == [commit_id]
    for oid in (commit_id, tree_id, blob_id):
        assert store.has(oid)
        assert oid in store.published_oids()


def test_tip_accepts_uppercase_hex(store, importer):
    blob_id, blob = obj("blob", b"x")
    tree_id, tree = obj("tree", tree_content([(0o100644, "f", blob_id)]))
    commit_id, commit = obj("commit", commit_content(tree_id))

    b = PackBuilder()
    b.add(1, commit)
    b.add(2, tree)
    b.add_blob(blob)
    record = importer.import_pack(b.build(), tips=[commit_id.upper()])
    assert record["tips"] == [commit_id]


def test_tip_import_merge_commit_needs_both_parents(store, importer):
    # parent A: file a ; parent B: file b ; merge tree holds both files
    a_blob_id, a_blob = obj("blob", b"a\n")
    a_tree_id, a_tree = obj("tree", tree_content([(0o100644, "a", a_blob_id)]))
    p1_id, p1 = obj("commit", commit_content(a_tree_id, message="p1\n"))

    b_blob_id, b_blob = obj("blob", b"b\n")
    b_tree_id, b_tree = obj("tree", tree_content([(0o100644, "b", b_blob_id)]))
    p2_id, p2 = obj("commit", commit_content(b_tree_id, message="p2\n"))

    merge_tree_id, merge_tree = obj(
        "tree",
        tree_content(
            [(0o100644, "a", a_blob_id), (0o100644, "b", b_blob_id)]
        ),
    )
    merge_id, merge = obj("commit", commit_content(merge_tree_id, (p1_id, p2_id)))

    pack = PackBuilder()
    pack.add(1, merge)
    pack.add(2, merge_tree)
    pack.add_blob(a_blob)
    pack.add_blob(b_blob)
    pack.add(1, p1)
    pack.add(2, a_tree)
    # p2 and its tree deliberately left out of the pack
    pack.add(1, p2)  # p2 commit present, b_tree missing
    with pytest.raises(ConnectivityError, match="tree"):
        importer.import_pack(pack.build(), tips=[merge_id])
    assert store.read_manifest()["imports"] == []
    assert list(store.staging_dir.iterdir()) == []

    # add p2's tree: whole merge graph now complete
    pack.add(2, b_tree)
    importer.import_pack(pack.build(), tips=[merge_id])
    assert store.read(merge_id)[0] == "commit"


def test_tip_annotated_tag_and_tag_chain(store, importer):
    blob_id, blob = obj("blob", b"data")
    tree_id, tree = obj("tree", tree_content([(0o100644, "d", blob_id)]))
    commit_id, commit = obj("commit", commit_content(tree_id))
    inner_id, inner_tag = obj("tag", tag_content(commit_id))
    outer_id, outer_tag = obj(
        "tag", tag_content(inner_id, target_type="tag", name="outer")
    )

    pack = PackBuilder()
    pack.add(4, outer_tag)
    pack.add(4, inner_tag)
    pack.add(1, commit)
    pack.add(2, tree)
    pack.add_blob(blob)
    record = importer.import_pack(pack.build(), tips=[outer_id])
    assert record["tips"] == [outer_id]


def test_tip_tree_with_binary_name_and_spaces(store, importer):
    blob_id, blob = obj("blob", b"\x00\x01\x02")
    name = b"space in \xe4\xb8\xad name.dat"
    tree_id, tree = obj("tree", tree_content([(0o100644, name, blob_id)]))
    commit_id, commit = obj("commit", commit_content(tree_id))

    pack = PackBuilder()
    pack.add(1, commit)
    pack.add(2, tree)
    pack.add_blob(blob)
    importer.import_pack(pack.build(), tips=[commit_id])
    assert store.read(blob_id) == ("blob", blob)


def test_tip_symlink_is_blob_reference(store, importer):
    target_id, target = obj("blob", b"target file\n")
    link_id, link = obj("blob", b"target file")
    tree_id, tree = obj(
        "tree",
        tree_content(
            [
                (0o120000, "link", link_id),
                (0o100644, "target file", target_id),
            ]
        ),
    )
    commit_id, commit = obj("commit", commit_content(tree_id))

    pack = PackBuilder()
    pack.add(1, commit)
    pack.add(2, tree)
    pack.add_blob(target)
    pack.add_blob(link)
    importer.import_pack(pack.build(), tips=[commit_id])
    assert store.read(link_id) == ("blob", link)

    # same tree, but symlink points at a *tree* oid -> kind mismatch;
    # use a fresh store so the previously committed manifest does not
    # pre-satisfy the second import
    fresh = ObjectStore(store.root.parent / "fresh")
    bad_importer = PackImporter(fresh)
    bad_tree_id, bad_tree = obj(
        "tree", tree_content([(0o120000, "link", tree_id)])
    )
    bad_commit_id, bad_commit = obj("commit", commit_content(bad_tree_id))
    bad = PackBuilder()
    bad.add(1, bad_commit)
    bad.add(2, bad_tree)
    with pytest.raises(ConnectivityError, match="blob"):
        bad_importer.import_pack(bad.build(), tips=[bad_commit_id])
    assert fresh.read_manifest()["imports"] == []


def test_tip_gitlink_needs_no_local_objects(store, importer):
    # 160000 entry references a commit in an external submodule repo
    external_commit = "1" * 40
    blob_id, blob = obj("blob", b"main\n")
    tree_id, tree = obj(
        "tree",
        tree_content(
            [
                (0o100644, "README", blob_id),
                (0o160000, "vendor/sub", external_commit),
            ]
        ),
    )
    commit_id, commit = obj("commit", commit_content(tree_id))

    pack = PackBuilder()
    pack.add(1, commit)
    pack.add(2, tree)
    pack.add_blob(blob)
    importer.import_pack(pack.build(), tips=[commit_id])
    assert not store.has(external_commit)  # external reference only


# -- failure contracts --------------------------------------------------------


def test_tip_missing_tree_rejected(store, importer):
    blob_id, blob = obj("blob", b"x")
    tree_id, _tree = obj("tree", tree_content([(0o100644, "f", blob_id)]))
    commit_id, commit = obj("commit", commit_content(tree_id))

    pack = PackBuilder()
    pack.add(1, commit)
    pack.add_blob(blob)  # tree object missing
    with pytest.raises(ConnectivityError, match="missing tree"):
        importer.import_pack(pack.build(), tips=[commit_id])
    _assert_nothing_published(store)


def test_tip_missing_blob_rejected(store, importer):
    blob_id, _blob = obj("blob", b"x")
    tree_id, tree = obj("tree", tree_content([(0o100644, "f", blob_id)]))
    commit_id, commit = obj("commit", commit_content(tree_id))

    pack = PackBuilder()
    pack.add(1, commit)
    pack.add(2, tree)
    with pytest.raises(ConnectivityError, match="missing blob"):
        importer.import_pack(pack.build(), tips=[commit_id])
    _assert_nothing_published(store)


def test_tip_missing_subtree_rejected(store, importer):
    leaf_id, leaf_blob = obj("blob", b"leaf")
    sub_id, _sub = obj(
        "tree", tree_content([(0o100644, "leaf", leaf_id)])
    )
    root_id, root = obj("tree", tree_content([(0o040000, "sub", sub_id)]))
    commit_id, commit = obj("commit", commit_content(root_id))

    pack = PackBuilder()
    pack.add(1, commit)
    pack.add(2, root)
    pack.add_blob(leaf_blob)
    with pytest.raises(ConnectivityError, match="missing tree"):
        importer.import_pack(pack.build(), tips=[commit_id])
    _assert_nothing_published(store)


def test_tip_commit_tree_points_at_blob(store, importer):
    other_id, other = obj("blob", b"not a tree")
    commit_id, commit = obj("commit", commit_content(other_id))

    pack = PackBuilder()
    pack.add(1, commit)
    pack.add_blob(other)
    with pytest.raises(ConnectivityError, match="as tree, but it is a blob"):
        importer.import_pack(pack.build(), tips=[commit_id])
    _assert_nothing_published(store)


def test_tip_tree_entry_points_at_tree_where_blob_expected(store, importer):
    child_tree_id, child_tree = obj("tree", tree_content([]))
    root_id, root = obj(
        "tree", tree_content([(0o100644, "file", child_tree_id)])
    )
    commit_id, commit = obj("commit", commit_content(root_id))

    pack = PackBuilder()
    pack.add(1, commit)
    pack.add(2, root)
    pack.add(2, child_tree)
    with pytest.raises(ConnectivityError, match="as blob, but it is a tree"):
        importer.import_pack(pack.build(), tips=[commit_id])
    _assert_nothing_published(store)


def test_tip_parent_points_at_non_commit(store, importer):
    blob_id, blob = obj("blob", b"x")
    tree_id, tree = obj("tree", tree_content([]))
    commit_id, commit = obj("commit", commit_content(tree_id, parents=(blob_id,)))

    pack = PackBuilder()
    pack.add(1, commit)
    pack.add(2, tree)
    pack.add_blob(blob)
    with pytest.raises(ConnectivityError, match="as commit, but it is a blob"):
        importer.import_pack(pack.build(), tips=[commit_id])
    _assert_nothing_published(store)


def test_tip_that_is_plain_blob_rejected(store, importer):
    blob_id, blob = obj("blob", b"i am no commit")
    pack = PackBuilder()
    pack.add_blob(blob)
    with pytest.raises(ConnectivityError, match="blob"):
        importer.import_pack(pack.build(), tips=[blob_id])
    _assert_nothing_published(store)


def test_tip_unknown_id_rejected(store, importer):
    pack = PackBuilder()
    pack.add_blob(b"lonely")
    with pytest.raises(ConnectivityError, match="missing object"):
        importer.import_pack(pack.build(), tips=["ab" * 20])
    _assert_nothing_published(store)


def test_tip_bad_id_format(store, importer):
    pack = PackBuilder().build()
    for bad in ("zz" * 20, "deadbeef", "", 12345):
        with pytest.raises(ConnectivityError):
            importer.import_pack(pack, tips=[bad])


def test_tag_target_type_lie_rejected(store, importer):
    # tag claims the target is a commit, but it is really a tree
    tree_id, tree = obj("tree", tree_content([]))
    tag_id, tag = obj("tag", tag_content(tree_id, target_type="commit"))
    pack = PackBuilder()
    pack.add(4, tag)
    pack.add(2, tree)
    with pytest.raises(ConnectivityError, match="as commit, but it is a tree"):
        importer.import_pack(pack.build(), tips=[tag_id])
    _assert_nothing_published(store)


def test_tag_cycle_rejected(store, importer):
    # A real mutual tag cycle needs hash preimages to pack, so exercise the
    # guard at the verifier boundary with a controllable object provider.
    from pack_import.connectivity import verify_tips

    class CycleStore:
        def __init__(self, objects):
            self._objects = objects

        def published_oids(self):
            return set(self._objects)

        def read(self, oid):
            return self._objects[oid]

    a_id = "a" * 40
    b_id = "b" * 40
    objects = {
        a_id: ("tag", tag_content(b_id, target_type="tag", name="a")),
        b_id: ("tag", tag_content(a_id, target_type="tag", name="b")),
    }
    with pytest.raises(ConnectivityError, match="tag cycle"):
        verify_tips([a_id], {}, CycleStore(objects))

    # same machinery through the importer: tip->tag->tag->commit never ends
    tag1 = "1" * 40
    tag2 = "2" * 40
    cyclic = {
        tag1: ("tag", tag_content(tag2, target_type="tag", name="t1")),
        tag2: ("tag", tag_content(tag1, target_type="tag", name="t2")),
    }
    with pytest.raises(ConnectivityError, match="tag cycle"):
        verify_tips([tag1], {}, CycleStore(cyclic))


def test_unsupported_tree_mode_rejected(store, importer):
    blob_id, blob = obj("blob", b"x")
    tree_id, tree = obj(
        "tree", tree_entry(0o100700, "weird", blob_id)
    )
    commit_id, commit = obj("commit", commit_content(tree_id))
    pack = PackBuilder()
    pack.add(1, commit)
    pack.add(2, tree)
    pack.add_blob(blob)
    with pytest.raises(ConnectivityError, match="unsupported mode"):
        importer.import_pack(pack.build(), tips=[commit_id])
    _assert_nothing_published(store)


def test_malformed_commit_rejected(store, importer):
    blob_id, blob = obj("blob", b"x")
    bad_commit = b"not a valid commit at all"
    bad_id = compute_oid("commit", bad_commit)
    pack = PackBuilder()
    pack.add(1, bad_commit)
    pack.add_blob(blob)
    with pytest.raises(ConnectivityError):
        importer.import_pack(pack.build(), tips=[bad_id])
    _assert_nothing_published(store)


# -- shared objects and leftovers ---------------------------------------------


def test_tip_shares_objects_with_previous_import(store, importer):
    shared_id, shared = obj("blob", b"shared content\n")
    old_tree_id, old_tree = obj(
        "tree", tree_content([(0o100644, "shared.txt", shared_id)])
    )
    old_commit_id, old_commit = obj("commit", commit_content(old_tree_id))
    first = PackBuilder()
    first.add(1, old_commit)
    first.add(2, old_tree)
    first.add_blob(shared)
    importer.import_pack(first.build(), tips=[old_commit_id])

    # second commit reuses the shared blob but the pack omits it
    new_id, new_blob = obj("blob", b"brand new\n")
    new_tree_id, new_tree = obj(
        "tree",
        tree_content(
            [
                (0o100644, "shared.txt", shared_id),
                (0o100644, "new.txt", new_id),
            ]
        ),
    )
    new_commit_id, new_commit = obj(
        "commit", commit_content(new_tree_id, parents=(old_commit_id,))
    )
    second = PackBuilder()
    second.add(1, new_commit)
    second.add(2, new_tree)
    second.add_blob(new_blob)
    importer.import_pack(second.build(), tips=[new_commit_id])
    assert len(store.read_manifest()["imports"]) == 2


def test_leaked_loose_object_does_not_count_as_archive(store, importer):
    # Simulate the crash window of a previous failed publish: objects were
    # moved into objects/ but the manifest commit point was never reached.
    blob_id, blob = obj("blob", b"leaked\n")
    loose_write(store, "blob", blob)
    assert store.has(blob_id)  # physically present ...
    assert blob_id not in store.published_oids()  # ... but never archived

    tree_id, tree = obj(
        "tree", tree_content([(0o100644, "leaked.txt", blob_id)])
    )
    commit_id, commit = obj("commit", commit_content(tree_id))
    pack = PackBuilder()
    pack.add(1, commit)
    pack.add(2, tree)  # blob deliberately not included
    with pytest.raises(ConnectivityError, match="missing blob"):
        importer.import_pack(pack.build(), tips=[commit_id])
    _assert_nothing_published(store)

    # object-level import without --tip keeps the legacy behaviour: it
    # trusts the caller, delivers the pack's own objects and never checks
    # descendants (the leaked blob happens to make the tree readable)
    record = importer.import_pack(pack.build())
    published = store.published_oids()
    assert {o["oid"] for o in record["objects"]} <= published
    assert commit_id in published and tree_id in published


def test_manifest_object_missing_on_disk_rejected(store, importer, tmp_path):
    # Publish a complete graph, then delete one loose file: the manifest
    # claims an archive object that is physically unreadable.
    blob_id, blob = obj("blob", b"fragile\n")
    tree_id, tree = obj(
        "tree", tree_content([(0o100644, "f", blob_id)])
    )
    commit_id, commit = obj("commit", commit_content(tree_id))
    first = PackBuilder()
    first.add(1, commit)
    first.add(2, tree)
    first.add_blob(blob)
    importer.import_pack(first.build(), tips=[commit_id])
    store.loose_path(blob_id).unlink()

    # second commit reuses the blob claimed by the manifest
    tree2_id, tree2 = obj(
        "tree", tree_content([(0o100644, "f", blob_id)])
    )
    commit2_id, commit2 = obj(
        "commit", commit_content(tree2_id, parents=(commit_id,))
    )
    second = PackBuilder()
    second.add(1, commit2)
    second.add(2, tree2)
    with pytest.raises(ConnectivityError, match="not readable"):
        importer.import_pack(second.build(), tips=[commit2_id])


# -- no-tip legacy behaviour ---------------------------------------------------


def test_no_tip_keeps_object_level_import(store, importer):
    blob_id, blob = obj("blob", b"whatever")
    builder = PackBuilder()
    builder.add_blob(blob)
    record = importer.import_pack(builder.build())
    assert record["tips"] == []
    assert store.published_oids() == {blob_id}


def _assert_nothing_published(store):
    assert store.read_manifest()["imports"] == []
    assert list(store.staging_dir.iterdir()) == []
    assert store.published_oids() == set()
