import shutil

import pytest

from pack_import import ObjectStore, PackImporter


@pytest.fixture
def store(tmp_path):
    return ObjectStore(tmp_path / "store")


@pytest.fixture
def importer(store):
    return PackImporter(store)


@pytest.fixture
def src_repo(tmp_path):
    from packtools import make_repo

    return make_repo(tmp_path / "src")


requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not found")
