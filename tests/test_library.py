import importlib.util
import io
import json
import zipfile
from pathlib import Path

import pytest
from PIL import Image as PILImage

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("memeharbor_library", ROOT / "library.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Library, inspect_image = module.Library, module.inspect_image
spec = importlib.util.spec_from_file_location(
    "memeharbor_migration", ROOT / "migration.py"
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def picture(color="red", animated=False):
    data = io.BytesIO()
    image = PILImage.new("RGB", (60, 40), color)
    if animated:
        image.save(
            data,
            format="GIF",
            save_all=True,
            append_images=[PILImage.new("RGB", (60, 40), "blue")],
            duration=120,
            loop=0,
        )
    else:
        image.save(data, format="PNG")
    return data.getvalue()


def test_migration_preserves_files_tags_and_ids(tmp_path):
    resource = tmp_path / "resource"
    resource.mkdir()
    (resource / "cat-100.png").write_bytes(picture())
    (resource / "dog-200.gif").write_bytes(picture(animated=True))
    original = json.dumps(
        {
            "version": 1,
            "images": {"cat-100.png": ["猫", "憋笑"], "dog-200.gif": ["狗"]},
        },
        ensure_ascii=False,
    )
    (tmp_path / "image_tags.json").write_text(original, encoding="utf-8")
    library = Library(tmp_path)
    assert library.get(1)["tags"] == ["猫", "憋笑"]
    assert (tmp_path / "image_tags.json").read_text(encoding="utf-8") == original
    assert (tmp_path / "image_tags.v1-backup.json").read_text(
        encoding="utf-8"
    ) == original
    library.update_tags([1], "add", ["可爱"])
    reloaded = Library(tmp_path)
    assert reloaded.get(1)["tags"] == ["猫", "憋笑", "可爱"]
    assert reloaded.path(reloaded.get(2)).read_bytes() == picture(animated=True)


def test_corrupt_legacy_index_does_not_get_marked_migrated(tmp_path):
    (tmp_path / "image_tags.json").write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError, match="停止迁移"):
        Library(tmp_path)
    (tmp_path / "image_tags.json").write_text('{"images": {}}')
    assert Library(tmp_path).search() == []


def test_search_duplicate_recycle_and_rename(tmp_path):
    lib = Library(tmp_path)
    cat, _ = lib.add(picture(), ["猫", "憋笑", "Happy"])
    dog, _ = lib.add(picture("blue"), ["狗", "伤心"])
    same, duplicate = lib.add(picture(), ["可爱"])
    assert duplicate and same["id"] == cat["id"]
    assert len(list(lib.resources.iterdir())) == 2
    assert [i["id"] for i in lib.search("猫 憋笑 -伤心")] == [cat["id"]]
    assert len(lib.search("猫 | 狗")) == 2
    assert lib.search("happy") and (not lib.search("happy", case_sensitive=True))
    assert lib.search(f"#{dog['id']}")[0]["id"] == dog["id"]
    lib.rename_tag("憋笑", "可爱")
    assert lib.get(cat["id"])["tags"].count("可爱") == 1
    lib.update_tags([cat["id"]], "replace", [])
    assert lib.get(cat["id"])["tags"] == [] and lib.path(cat).exists()
    lib.recycle([cat["id"]])
    assert len(lib.search()) == 1 and len(lib.search(deleted=True)) == 1
    with pytest.raises(ValueError, match="回收站"):
        lib.get(cat["id"])
    lib.recycle([cat["id"]], restore=True)
    assert len(lib.search()) == 2


def test_batch_operations_are_atomic(tmp_path):
    lib = Library(tmp_path)
    item, _ = lib.add(picture(), ["猫"])
    with pytest.raises(ValueError):
        lib.update_tags([item["id"], 999], "add", ["可爱"])
    assert lib.get(item["id"])["tags"] == ["猫"]
    with pytest.raises(ValueError):
        lib.recycle([item["id"], 999])
    assert lib.get(item["id"])["deleted"] is None


def test_export_import_preserves_data(tmp_path):
    lib = Library(tmp_path / "old")
    item, _ = lib.add(picture(), ["猫"])
    lib.annotate(item["id"], "红色猫", "你好")
    archive = lib.export()
    target = Library(tmp_path / "new")
    mappings = target.import_archive(archive)
    loaded = target.get(mappings[0]["new"])
    assert loaded["caption"] == "红色猫" and loaded["ocr"] == "你好"
    assert target.path(loaded).read_bytes() == picture()
    target.import_archive(archive)
    assert len(target.search()) == 1


def test_bad_archive_and_image_rejected(tmp_path):
    lib = Library(tmp_path / "lib")
    with pytest.raises(ValueError):
        inspect_image(b"not an image", 100)
    with pytest.raises(ValueError):
        inspect_image(picture(), 10)
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(
            "manifest.json",
            json.dumps(
                {"version": 2, "images": [{"filename": "../escape.png", "tags": []}]}
            ),
        )
    with pytest.raises(ValueError):
        lib.import_archive(archive)
    assert lib.search() == []


def test_import_keeps_untagged_and_does_not_restore_existing_trash(tmp_path):
    source = Library(tmp_path / "source")
    item, _ = source.add(picture(), [])
    source.annotate(item["id"], "原描述", "文字")
    archive = source.export()
    destination = Library(tmp_path / "destination")
    mapping = destination.import_archive(archive)
    assert destination.get(mapping[0]["new"])["tags"] == []
    destination.recycle([mapping[0]["new"]])
    destination.import_archive(archive)
    assert destination.search() == []
    assert len(destination.search(deleted=True)) == 1
