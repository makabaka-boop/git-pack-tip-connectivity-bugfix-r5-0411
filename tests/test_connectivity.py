"""Tip reachability/type verification tests, built from raw git objects.

These tests construct commits, trees, blobs and annotated tags by hand
(the object formats are tiny and fully specified), so they exercise the
verifier without depending on a git binary.  Git-generated end-to-end
coverage (real merges, submodules, fsck) lives in test_git_roundtrip.py.
"""

import pytest

from pack_import import ObjectStore, PackImporter, PackFormatError
from pack_import.packfile import (
    OBJ_BLOB,
    OBJ_COMMIT,
    OBJ_TAG,
    OBJ_TREE,
    compute_oid,
)
from pack_import.store import loose_object_bytes
from packtools import PackBuilder


# --- tiny raw-object factory --------------------------------------------------


def _tree_entry(mode: bytes, name: bytes, oid: str) -> bytes:
    return mode + b" " + name + b"\0" + bytes.fromhex(oid)


def make_blob(content=b"data\n"):
    return compute_oid("blob", content), content


def make_tree(entries):
    """entries: list of (mode_bytes, name, oid)."""
    content = b"".join(_tree_entry(m, n, o) for m, n, o in entries)
    return compute_oid("tree", content), content


def make_commit(tree_oid, parents=(), message=b"m\n"):
    lines = [b"tree " + tree_oid.encode()]
    lines += [b"parent " + p.encode() for p in parents]
    lines += [
        b"author A <a@b.c> 1 +0000",
        b"committer A <a@b.c> 1 +0000",
        b"",
        message,
    ]
    content = b"\n".join(lines)
    return compute_oid("commit", content), content


def make_tag(target_oid, target_type, name="r"):
    content = (
        b"object " + target_oid.encode() + b"\n"
        b"type " + target_type.encode() + b"\n"
        b"tag " + name.encode() + b"\n"
        b"tagger A <a@b.c> 1 +0000\n\n"
        b"tag message\n"
    )
    return compute_oid("tag", content), content


class Graph:
    """Collects raw objects and emits them as one (un-deltaed) pack."""

    def __init__(self):
        self.objects = {}  # oid -> (type, content)

    def add(self, oid, type_name, content):
        self.objects[oid] = (type_name, content)
        return oid

    @staticmethod
    def _mk(type_name, content):
        return compute_oid(type_name, content), type_name, content

    def add_obj(self, oid, type_name, content):
        return self.add(oid, type_name, content)

    def tree(self, entries):
        oid, content = make_tree(entries)
        return self.add_obj(oid, "tree", content)

    def commit(self, tree_oid, parents=()):
        oid, content = make_commit(tree_oid, parents)
        return self.add_obj(oid, "commit", content)

    def tag(self, target_oid, target_type="commit"):
        oid, content = make_tag(target_oid, target_type)
        return self.add_obj(oid, "tag", content)

    def pack(self, exclude=()):
        codes = {"commit": OBJ_COMMIT, "tree": OBJ_TREE, "blob": OBJ_BLOB, "tag": OBJ_TAG}
        b = PackBuilder()
        for oid, (type_name, content) in self.objects.items():
            if oid in exclude:
                continue
            b.add(codes[type_name], content)
        return b.build()

    def loose(self, store, oid, committed=True):
        """Write *oid* into the store; only committed objects hit the manifest."""
        type_name, content = self.objects[oid]
        path = store.loose_path(oid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(loose_object_bytes(type_name, content))
        if committed:
            manifest = store.read_manifest()
            manifest["imports"].append(
                {
                    "id": f"seed-{oid[:8]}",
                    "tips": [],
                    "objects": [
                        {"oid": oid, "type": type_name, "size": len(content)}
                    ],
                }
            )
            (store.root / "manifest.json").write_bytes(
                __import__("json").dumps(manifest).encode()
            )
        return oid


# --- happy paths --------------------------------------------------------------


def test_tip_commit_tree_blob(store, importer):
    g = Graph()
    b_oid = g.add_obj(*Graph._mk("blob", b"hello\n"))
    t_oid = g.tree([(b"100644", b"f", b_oid)])
    c_oid = g.commit(t_oid)

    record = importer.import_pack(g.pack(), tips=[c_oid])
    assert record["tips"] == [c_oid]
    assert store.read(c_oid)[0] == "commit"
    assert store.read(b_oid) == ("blob", b"hello\n")


def test_tip_executable_and_symlink_entries(store, importer):
    g = Graph()
    exe = g.add_obj(*Graph._mk("blob", b"#!/bin/sh\n"))
    link = g.add_obj(*Graph._mk("blob", b"target/path"))
    t_oid = g.tree(
        [
            (b"100755", b"run", exe),
            (b"120000", b"ln", link),
        ]
    )
    c_oid = g.commit(t_oid)
    record = importer.import_pack(g.pack(), tips=[c_oid])
    assert record["tips"] == [c_oid]


def test_tip_binary_filename(store, importer):
    g = Graph()
    b_oid = g.add_obj(*Graph._mk("blob", b"\x00\xffbin"))
    # any byte except NUL is legal in a tree entry name (NUL terminates it)
    name = b"\xff\xfe/weird \xe2\x98\x83 name"
    t_oid = g.tree([(b"100644", name, b_oid)])
    c_oid = g.commit(t_oid)
    importer.import_pack(g.pack(), tips=[c_oid])  # must not raise
    assert store.has(b_oid)


def test_tip_merge_commit_requires_both_parents(store, importer):
    g = Graph()
    # diverging parents: different trees so the parent oids are distinct
    blob1 = g.add_obj(*Graph._mk("blob", b"one\n"))
    blob2 = g.add_obj(*Graph._mk("blob", b"two\n"))
    t1 = g.tree([(b"100644", b"f", blob1)])
    t2 = g.tree([(b"100644", b"f", blob2)])
    p1 = g.commit(t1)
    p2 = g.commit(t2)
    merge_tree = g.tree(
        [(b"40000", b"a", t1), (b"40000", b"b", t2)]
    )
    merge = g.commit(merge_tree, parents=[p1, p2])

    importer.import_pack(g.pack(), tips=[merge])  # both parents present: ok

    # a second, empty archive rejects a pack carrying the merge but missing
    # p2's ancestry -- the missing objects were never committed there
    store2 = ObjectStore(store.root.parent / "store2")
    importer2 = PackImporter(store2)
    g2 = Graph()
    for oid in (merge, merge_tree, p1, t1, blob1):
        g2.add_obj(oid, *g.objects[oid])
    with pytest.raises(PackFormatError, match="missing"):
        importer2.import_pack(g2.pack(), tips=[merge])
    assert store2.read_manifest()["imports"] == []


def test_tip_nested_trees(store, importer):
    g = Graph()
    leaf_blob = g.add_obj(*Graph._mk("blob", b"deep\n"))
    leaf = g.tree([(b"100644", b"x", leaf_blob)])
    root = g.tree([(b"40000", b"dir", leaf)])
    c_oid = g.commit(root)
    importer.import_pack(g.pack(), tips=[c_oid])


def test_annotated_tag_tip(store, importer):
    g = Graph()
    b_oid = g.add_obj(*Graph._mk("blob", b"x"))
    t_oid = g.tree([(b"100644", b"f", b_oid)])
    c_oid = g.commit(t_oid)
    tag_oid = g.tag(c_oid, "commit")

    record = importer.import_pack(g.pack(), tips=[tag_oid])
    assert record["tips"] == [tag_oid]


def test_annotated_tag_chain(store, importer):
    g = Graph()
    t_oid = g.tree([])
    c_oid = g.commit(t_oid)
    inner = g.tag(c_oid, "commit")
    outer = g.tag(inner, "tag")
    importer.import_pack(g.pack(), tips=[outer])


def test_gitlink_does_not_require_submodule_commit(store, importer):
    g = Graph()
    # a 160000 entry points at a commit that is nowhere in the pack/store
    foreign = "f" * 40
    t_oid = g.tree([(b"160000", b"vendor", foreign)])
    c_oid = g.commit(t_oid)
    importer.import_pack(g.pack(), tips=[c_oid])
    assert not store.has(foreign)


def test_tip_satisfied_partly_from_archive(store, importer):
    """Objects of a prior successful import back a later tip."""
    g = Graph()
    shared_blob = g.add_obj(*Graph._mk("blob", b"shared\n"))
    t1 = g.tree([(b"100644", b"s", shared_blob)])
    c1 = g.commit(t1)
    importer.import_pack(g.pack(), tips=[c1])

    # a child commit reusing the same tree arrives alone in a new pack
    g2 = Graph()
    c2_oid, c2_content = make_commit(t1, parents=[c1])
    g2.add_obj(c2_oid, "commit", c2_content)
    record = importer.import_pack(g2.pack(), tips=[c2_oid])
    assert record["object_count"] == 1
    assert len(store.read_manifest()["imports"]) == 2


def test_no_tips_keeps_object_level_import(store, importer):
    g = Graph()
    lone = g.add_obj(*Graph._mk("blob", b"orphan blob, no commit"))
    record = importer.import_pack(g.pack())
    assert record["tips"] == []
    assert store.has(lone)


# --- rejection paths -----------------------------------------------------------


def test_missing_descendant_blob_rejected(store, importer):
    g = Graph()
    b_oid = g.add_obj(*Graph._mk("blob", b"x"))
    t_oid = g.tree([(b"100644", b"f", b_oid)])
    c_oid = g.commit(t_oid)
    with pytest.raises(PackFormatError, match="missing"):
        importer.import_pack(g.pack(exclude={b_oid}), tips=[c_oid])
    assert not store.has(c_oid)
    assert store.read_manifest()["imports"] == []


def test_missing_tree_rejected(store, importer):
    g = Graph()
    b_oid = g.add_obj(*Graph._mk("blob", b"x"))
    t_oid = g.tree([(b"100644", b"f", b_oid)])
    c_oid = g.commit(t_oid)
    with pytest.raises(PackFormatError):
        importer.import_pack(g.pack(exclude={t_oid}), tips=[c_oid])


def test_file_entry_pointing_at_tree_rejected(store, importer):
    g = Graph()
    victim = g.tree([])  # an actual tree object...
    t_oid = g.tree([(b"100644", b"f", victim)])  # ...used as a regular file
    c_oid = g.commit(t_oid)
    with pytest.raises(PackFormatError, match="wrong type"):
        importer.import_pack(g.pack(), tips=[c_oid])
    assert store.read_manifest()["imports"] == []


def test_tree_entry_pointing_at_blob_rejected(store, importer):
    g = Graph()
    b_oid = g.add_obj(*Graph._mk("blob", b"not a tree"))
    t_oid = g.tree([(b"40000", b"dir", b_oid)])
    c_oid = g.commit(t_oid)
    with pytest.raises(PackFormatError, match="expected tree"):
        importer.import_pack(g.pack(), tips=[c_oid])


def test_symlink_entry_pointing_at_tree_rejected(store, importer):
    g = Graph()
    victim = g.tree([])
    t_oid = g.tree([(b"120000", b"ln", victim)])
    c_oid = g.commit(t_oid)
    with pytest.raises(PackFormatError, match="expected blob"):
        importer.import_pack(g.pack(), tips=[c_oid])


def test_commit_tree_pointing_at_blob_rejected(store, importer):
    g = Graph()
    fake = g.add_obj(*Graph._mk("blob", b"i am not a tree"))
    c_oid, c_content = make_commit(fake)
    g.add_obj(c_oid, "commit", c_content)
    with pytest.raises(PackFormatError, match="expected tree"):
        importer.import_pack(g.pack(), tips=[c_oid])


def test_commit_parent_pointing_at_blob_rejected(store, importer):
    g = Graph()
    tree_oid = g.tree([])
    parent = g.add_obj(*Graph._mk("blob", b"i am not a commit"))
    c_oid, c_content = make_commit(tree_oid, parents=[parent])
    g.add_obj(c_oid, "commit", c_content)
    with pytest.raises(PackFormatError, match="expected commit"):
        importer.import_pack(g.pack(), tips=[c_oid])


def test_blob_tip_rejected(store, importer):
    g = Graph()
    b_oid = g.add_obj(*Graph._mk("blob", b"tip"))
    with pytest.raises(PackFormatError, match="commit"):
        importer.import_pack(g.pack(), tips=[b_oid])


def test_tree_tip_rejected(store, importer):
    g = Graph()
    t_oid = g.tree([])
    with pytest.raises(PackFormatError, match="commit"):
        importer.import_pack(g.pack(), tips=[t_oid])


def test_tag_peeling_to_blob_rejected(store, importer):
    g = Graph()
    b_oid = g.add_obj(*Graph._mk("blob", b"x"))
    tag_oid = g.tag(b_oid, "blob")
    with pytest.raises(PackFormatError, match="expected a commit"):
        importer.import_pack(g.pack(), tips=[tag_oid])


def test_tag_missing_target_rejected(store, importer):
    g = Graph()
    t_oid = g.tree([])
    c_oid = g.commit(t_oid)
    tag_oid = g.tag("a" * 40, "commit")
    with pytest.raises(PackFormatError, match="missing"):
        importer.import_pack(g.pack(exclude={"a" * 40}), tips=[tag_oid])


def test_malformed_tip_id_rejected(store, importer):
    g = Graph()
    g.add_obj(*Graph._mk("blob", b"x"))
    with pytest.raises(PackFormatError, match="hex"):
        importer.import_pack(g.pack(), tips=["not-an-oid"])


def test_treeless_commit_rejected(store, importer):
    g = Graph()
    content = b"author A <a@b.c> 1 +0000\ncommitter A <a@b.c> 1 +0000\n\nm\n"
    c_oid = g.add_obj(compute_oid("commit", content), "commit", content)
    with pytest.raises(PackFormatError, match="tree"):
        importer.import_pack(g.pack(), tips=[c_oid])


def test_commit_with_two_trees_rejected(store, importer):
    g = Graph()
    t1 = g.tree([])
    t2, t2c = make_tree([])
    g.add_obj(t2, "tree", t2c)
    content = (
        b"tree " + t1.encode() + b"\n"
        b"tree " + t2.encode() + b"\n"
        b"author A <a@b.c> 1 +0000\n\nm\n"
    )
    c_oid = g.add_obj(compute_oid("commit", content), "commit", content)
    with pytest.raises(PackFormatError, match="multiple trees"):
        importer.import_pack(g.pack(), tips=[c_oid])


def test_tag_without_object_rejected(store, importer):
    g = Graph()
    content = b"type commit\ntag x\ntag A <a@b.c> 1 +0000\n\nm\n"
    oid = g.add_obj(compute_oid("tag", content), "tag", content)
    with pytest.raises(PackFormatError, match="object reference"):
        importer.import_pack(g.pack(), tips=[oid])


def test_invalid_tree_mode_rejected(store, importer):
    # NOTE: leading-zero modes like "0100644" parse (octal) to the canonical
    # mode and are accepted, matching git's integer-mode macros.
    for mode in (b"666", b"0", b"100666", b"400000"):
        g = Graph()
        b_oid = g.add_obj(*Graph._mk("blob", b"x"))
        t_content = _tree_entry(mode, b"f", b_oid)
        t_oid = g.add_obj(compute_oid("tree", t_content), "tree", t_content)
        c_oid = g.commit(t_oid)
        with pytest.raises(PackFormatError):
            importer.import_pack(g.pack(), tips=[c_oid])


def test_malformed_tree_entry_truncated(store, importer):
    g = Graph()
    t_content = b"100644 f\0" + b"00"  # oid short
    t_oid = g.add_obj(compute_oid("tree", t_content), "tree", t_content)
    c_oid = g.commit(t_oid)
    with pytest.raises(PackFormatError):
        importer.import_pack(g.pack(), tips=[c_oid])


def test_tree_entry_empty_name_rejected(store, importer):
    g = Graph()
    b_oid = g.add_obj(*Graph._mk("blob", b"x"))
    t_content = b"100644 \0" + bytes.fromhex(b_oid)
    t_oid = g.add_obj(compute_oid("tree", t_content), "tree", t_content)
    c_oid = g.commit(t_oid)
    with pytest.raises(PackFormatError, match="name"):
        importer.import_pack(g.pack(), tips=[c_oid])


def test_gitlink_zero_id_is_an_external_reference(store, importer):
    """The all-zero id, like any hex 20-byte gitlink id, needs no object."""
    g = Graph()
    t_oid = g.tree([(b"160000", b"sub", "0" * 40)])
    c_oid = g.commit(t_oid)
    importer.import_pack(g.pack(), tips=[c_oid])
    assert not store.has("0" * 40)


def test_gitlink_entry_truncated_rejected(store, importer):
    g = Graph()
    t_content = b"160000 sub\0" + b"f" * 10  # id truncated to 10 bytes
    t_oid = g.add_obj(compute_oid("tree", t_content), "tree", t_content)
    c_oid = g.commit(t_oid)
    with pytest.raises(PackFormatError, match="truncated"):
        importer.import_pack(g.pack(), tips=[c_oid])


# --- orphans from failed imports must not look archived -------------------------


def test_orphan_loose_object_cannot_satisfy_tip(store, importer):
    g = Graph()
    b_oid = g.add_obj(*Graph._mk("blob", b"x"))
    t_oid = g.tree([(b"100644", b"f", b_oid)])
    c_oid = g.commit(t_oid)

    # put the whole graph on disk WITHOUT any manifest record, exactly the
    # state an interrupted publish leaves behind
    for oid in (b_oid, t_oid, c_oid):
        g.loose(store, oid, committed=False)
    assert store.has(c_oid) and c_oid not in store.published_oids()

    # an empty pack naming that tip must fail: orphans are not archive content
    with pytest.raises(PackFormatError, match="missing"):
        importer.import_pack(PackBuilder().build(), tips=[c_oid])
    assert store.read_manifest()["imports"] == []


def test_orphan_cannot_serve_as_delta_base(store, importer):
    from pack_import.packfile import OBJ_REF_DELTA
    from packtools import insert_delta

    base = b"some external base"
    base_oid = compute_oid("blob", base)
    type_name, content = "blob", base
    path = store.loose_path(base_oid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(loose_object_bytes(type_name, content))
    # deliberately no manifest entry: this is an orphan, not archived

    b = PackBuilder()
    b.add(OBJ_REF_DELTA, insert_delta(len(base), b"derived"), base_oid=base_oid)
    with pytest.raises(Exception):
        importer.import_pack(b.build())
    assert store.read_manifest()["imports"] == []


def test_committed_object_does_serve_as_delta_base(store, importer):
    """The manifest snapshot must not break legitimate thin-pack imports."""
    from pack_import.packfile import OBJ_REF_DELTA
    from packtools import insert_delta

    base = b"some published base " * 10
    base_oid = compute_oid("blob", base)
    seed = PackBuilder()
    seed.add_blob(base)
    importer.import_pack(seed.build())  # committed: usable as a base

    b = PackBuilder()
    b.add(OBJ_REF_DELTA, insert_delta(len(base), b"derived"), base_oid=base_oid)
    record = importer.import_pack(b.build())
    assert record["object_count"] == 1


# --- failure leaves no published objects and no manifest entry -------------------


def test_failed_tip_publish_leaves_store_pristine(store, importer):
    g = Graph()
    b_oid = g.add_obj(*Graph._mk("blob", b"x"))
    t_oid = g.tree([(b"100644", b"f", b_oid)])
    c_oid = g.commit(t_oid)
    with pytest.raises(PackFormatError):
        importer.import_pack(g.pack(exclude={b_oid}), tips=[c_oid])

    published = [p for p in store.objects_dir.rglob("*") if p.is_file()]
    assert published == []
    assert list(store.staging_dir.iterdir()) == []
    assert store.read_manifest()["imports"] == []
