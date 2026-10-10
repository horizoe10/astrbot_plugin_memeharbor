import importlib.util
from pathlib import Path

from test_library import Library, picture

spec = importlib.util.spec_from_file_location(
    "memeharbor_migration", Path(__file__).resolve().parents[1] / "migration.py"
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def test_copy_preserves_ids_tags_recycle_and_source(tmp_path):
    legacy = Library(tmp_path / migration.LEGACY_NAME)
    item, _ = legacy.add(picture(), ["猫", "憋笑"], "bot:user")
    legacy.annotate(item["id"], "caption", "words")
    legacy.recycle([item["id"]])
    destination = tmp_path / "astrbot_plugin_memeharbor"
    assert migration.import_legacy_library(destination)
    new = Library(destination)
    assert new.get(item["id"], True) == legacy.get(item["id"], True)
    assert new.path(new.get(item["id"], True)).read_bytes() == picture()
    new.recycle([item["id"]], restore=True)
    assert legacy.get(item["id"], True)["deleted"] is not None
    assert not migration.import_legacy_library(destination)


def test_nonempty_destination_never_overwritten(tmp_path):
    legacy = Library(tmp_path / migration.LEGACY_NAME)
    legacy.add(picture(), ["old"])
    new = Library(tmp_path / "astrbot_plugin_memeharbor")
    new.add(picture("blue"), ["new"])
    assert not migration.import_legacy_library(new.root)
    assert new.get(1)["tags"] == ["new"]


def test_no_legacy_starts_empty(tmp_path):
    destination = tmp_path / "astrbot_plugin_memeharbor"
    assert not migration.import_legacy_library(destination)
    assert Library(destination).search() == []


def test_sqlite_backup_includes_committed_wal(tmp_path):
    legacy = Library(tmp_path / migration.LEGACY_NAME)
    item, _ = legacy.add(picture(), ["test"])
    with legacy.connect() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "UPDATE images SET caption='WAL update' WHERE id=?", (item["id"],)
        )
        connection.commit()
        destination = tmp_path / "astrbot_plugin_memeharbor"
        assert migration.import_legacy_library(destination)
    assert Library(destination).get(item["id"])["caption"] == "WAL update"
