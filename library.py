"""Persistent image catalog and preview rendering, independent of AstrBot."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
import zipfile
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "GIF": ".gif",
    "WEBP": ".webp",
    "BMP": ".bmp",
}
MAX_PIXELS = 40_000_000


def tags_from(value: str | list[str]) -> list[str]:
    if not isinstance(value, (str, list)):
        raise ValueError("标签必须是文字或文字列表")
    parts = re.split(r"[\s,，、;；]+", value) if isinstance(value, str) else value
    result = []
    for tag in parts:
        if not isinstance(tag, str):
            raise ValueError("标签必须是文字")
        tag = re.sub(r"[\x00-\x1f]", "", tag).strip()[:32]
        if tag and tag not in result:
            if tag.startswith(("-", "#")) or "|" in tag:
                raise ValueError("标签不能以 - 或 # 开头，也不能包含 |")
            result.append(tag)
    if len(result) > 24:
        raise ValueError("一张图片最多支持 24 个标签")
    return result


def inspect_image(data: bytes, limit: int) -> str:
    if not data or len(data) > limit:
        raise ValueError(f"图片为空或超过 {limit // 1048576} MB 限制")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("图片像素过大，请缩小后再收集")
            extension = EXTENSIONS.get(image.format)
            if not extension:
                raise ValueError("仅支持 JPG、PNG、GIF、WebP 和 BMP 图片")
            image.verify()
        return extension
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("无法识别图片内容，链接可能已失效") from exc


class Library:
    def __init__(self, root: Path, max_bytes: int = 15 * 1048576):
        self.root = root
        self.resources = root / "resource"
        self.cache = root / "preview_cache"
        self.db = root / "library_v2.sqlite3"
        self.max_bytes = max_bytes
        self.migration_warnings = []
        for path in (root, self.resources, self.cache):
            path.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL UNIQUE,
                    digest TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    created REAL NOT NULL,
                    owner TEXT NOT NULL DEFAULT '',
                    origin TEXT NOT NULL DEFAULT '',
                    caption TEXT NOT NULL DEFAULT '',
                    ocr TEXT NOT NULL DEFAULT '',
                    deleted REAL,
                    sent INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS image_digest ON images(digest);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """)
        self.migrate()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def migrate(self):
        with self.connect() as connection:
            if connection.execute("SELECT 1 FROM meta WHERE key='migrated'").fetchone():
                return
            legacy = self.root / "image_tags.json"
            tags = {}
            if legacy.exists():
                try:
                    raw = json.loads(legacy.read_text(encoding="utf-8-sig"))
                    tags = raw.get("images", raw) if isinstance(raw, dict) else None
                    if not isinstance(tags, dict):
                        raise ValueError("Invalid legacy index")
                except (ValueError, OSError) as exc:
                    raise ValueError(
                        "旧 image_tags.json 无法解析，已停止迁移以保护标签；请先修复或恢复备份"
                    ) from exc
                backup = self.root / "image_tags.v1-backup.json"
                if not backup.exists():
                    shutil.copy2(legacy, backup)
            for path in sorted(self.resources.iterdir()):
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or path.suffix.lower() not in {*EXTENSIONS.values(), ".jpeg"}
                ):
                    continue
                try:
                    if path.stat().st_size > self.max_bytes:
                        raise ValueError("Image exceeds size limit")
                    data = path.read_bytes()
                    inspect_image(data, self.max_bytes)
                    original_tags = tags.get(path.name) or [path.stem.split("-", 1)[0]]
                    # Preserve all legacy labels, even ones new commands reserve.
                    if isinstance(original_tags, str):
                        original_tags = [original_tags]
                    if not isinstance(original_tags, list) or not all(
                        isinstance(t, str) for t in original_tags
                    ):
                        raise ValueError("Invalid tags for legacy image")
                    connection.execute(
                        "INSERT OR IGNORE INTO images(filename,digest,tags,created) VALUES(?,?,?,?)",
                        (
                            path.name,
                            hashlib.sha256(data).hexdigest(),
                            json.dumps(original_tags, ensure_ascii=False),
                            path.stat().st_mtime,
                        ),
                    )
                except (ValueError, OSError) as exc:
                    self.migration_warnings.append(f"{path.name}: {exc}")
            connection.execute("INSERT INTO meta(key,value) VALUES('migrated','2')")

    @staticmethod
    def record(row):
        if row is None:
            raise ValueError("没有找到这个图片编号")
        item = dict(row)
        item["tags"] = json.loads(item["tags"])
        return item

    def path(self, item):
        path = (self.resources / item["filename"]).resolve()
        if path.parent != self.resources.resolve() or not path.is_file():
            raise ValueError("原图文件缺失，请在网页图库中检查或重新上传")
        return path

    def get(self, image_id, include_deleted=False):
        try:
            image_id = int(str(image_id).lstrip("#"))
        except ValueError as exc:
            raise ValueError("图片编号应为数字，例如 1024") from exc
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM images WHERE id=?", (image_id,)
            ).fetchone()
        item = self.record(row)
        if item["deleted"] and not include_deleted:
            raise ValueError("这张图片在回收站中，请先恢复")
        return item

    def add(self, data, tags, owner="", origin="", restore_duplicate=True):
        extension = inspect_image(data, self.max_bytes)
        labels = tags_from(tags)
        digest = hashlib.sha256(data).hexdigest()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM images WHERE digest=? ORDER BY deleted IS NOT NULL,id LIMIT 1",
                (digest,),
            ).fetchone()
            duplicate = row is not None
            if duplicate:
                item = self.record(row)
                labels = list(dict.fromkeys(item["tags"] + labels))
                if len(labels) > 24:
                    raise ValueError("这张图片已有较多标签，请先整理到 24 个以内")
                target = self.resources / item["filename"]
                if not target.exists():
                    target.write_bytes(data)
                connection.execute(
                    "UPDATE images SET tags=?,deleted=CASE WHEN ? THEN NULL ELSE deleted END WHERE id=?",
                    (
                        json.dumps(labels, ensure_ascii=False),
                        restore_duplicate,
                        item["id"],
                    ),
                )
                image_id = item["id"]
            else:
                filename = f"{digest}{extension}"
                target = self.resources / filename
                temporary = self.resources / f".{uuid.uuid4().hex}.tmp"
                try:
                    temporary.write_bytes(data)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                cursor = connection.execute(
                    "INSERT INTO images(filename,digest,tags,created,owner,origin) VALUES(?,?,?,?,?,?)",
                    (
                        filename,
                        digest,
                        json.dumps(labels, ensure_ascii=False),
                        time.time(),
                        owner,
                        origin,
                    ),
                )
                image_id = cursor.lastrowid
        return self.get(image_id, include_deleted=True), duplicate

    def search(self, query="", deleted=False, case_sensitive=False):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM images WHERE deleted IS "
                + ("NOT NULL" if deleted else "NULL")
                + " ORDER BY id DESC"
            ).fetchall()
        groups = [
            re.split(r"[\s,，、;；]+", group.strip()) for group in query.split("|")
        ]
        normalize = (lambda value: value) if case_sensitive else str.casefold

        def matches(item, term):
            if term.startswith("#"):
                return str(item["id"]) == term[1:]
            term = normalize(term)
            return term in [normalize(t) for t in item["tags"]] or term in normalize(
                item["caption"] + " " + item["ocr"]
            )

        result = []
        for row in rows:
            item = self.record(row)
            if any(
                all(
                    not matches(item, t[1:]) if t.startswith("-") else matches(item, t)
                    for t in terms
                    if t
                )
                for terms in groups
            ):
                result.append(item)
        return result

    def update_tags(self, ids, action, labels):
        labels = tags_from(labels)
        if action not in {"add", "remove", "replace"}:
            raise ValueError("不支持的标签操作")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for image_id in ids:
                row = connection.execute(
                    "SELECT * FROM images WHERE id=? AND deleted IS NULL",
                    (int(image_id),),
                ).fetchone()
                item = self.record(row)
                if action == "add":
                    tags = list(dict.fromkeys(item["tags"] + labels))
                elif action == "remove":
                    tags = [t for t in item["tags"] if t not in labels]
                else:
                    tags = labels
                if len(tags) > 24:
                    raise ValueError("一张图片最多支持 24 个标签")
                connection.execute(
                    "UPDATE images SET tags=? WHERE id=?",
                    (json.dumps(tags, ensure_ascii=False), image_id),
                )

    def rename_tag(self, old, new):
        labels = tags_from(new)
        if len(labels) != 1:
            raise ValueError("新标签必须是一个词")
        new = labels[0]
        changed = 0
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for row in connection.execute(
                "SELECT * FROM images WHERE deleted IS NULL"
            ).fetchall():
                item = self.record(row)
                if old in item["tags"]:
                    tags = list(
                        dict.fromkeys(new if t == old else t for t in item["tags"])
                    )
                    connection.execute(
                        "UPDATE images SET tags=? WHERE id=?",
                        (json.dumps(tags, ensure_ascii=False), item["id"]),
                    )
                    changed += 1
        return changed

    def recycle(self, ids, restore=False):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for image_id in ids:
                item = self.record(
                    connection.execute(
                        "SELECT * FROM images WHERE id=?", (int(image_id),)
                    ).fetchone()
                )
                if restore:
                    self.path(item)
                connection.execute(
                    "UPDATE images SET deleted=? WHERE id=?",
                    (None if restore else time.time(), image_id),
                )

    def annotate(self, image_id, caption, ocr):
        with self.connect() as connection:
            connection.execute(
                "UPDATE images SET caption=?,ocr=? WHERE id=?",
                (str(caption)[:1000], str(ocr)[:3000], image_id),
            )

    def mark_sent(self, image_id):
        with self.connect() as connection:
            connection.execute("UPDATE images SET sent=sent+1 WHERE id=?", (image_id,))

    def export(self):
        items = self.search() + self.search(deleted=True)
        path = self.cache / f"memeharbor-backup-{uuid.uuid4().hex}.zip"
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps({"version": 2, "images": items}, ensure_ascii=False),
            )
            for item in items:
                archive.write(self.path(item), f"resource/{item['filename']}")
        return path

    def import_archive(self, path):
        # Validate the entire package before changing the catalog; never extract paths.
        pending = []
        with zipfile.ZipFile(path) as archive:
            manifest_info = archive.getinfo("manifest.json")
            if manifest_info.file_size > 5 * 1048576:
                raise ValueError("备份索引过大")
            manifest = json.loads(archive.read(manifest_info))
            if manifest.get("version") != 2 or not isinstance(
                manifest.get("images"), list
            ):
                raise ValueError("仅支持本插件导出的 v2 备份")
            if len(manifest["images"]) > 1000:
                raise ValueError("单次导入最多 1000 张图片")
            total = 0
            for item in manifest["images"]:
                filename = item["filename"]
                if (
                    not isinstance(filename, str)
                    or Path(filename).name != filename
                    or "\\" in filename
                ):
                    raise ValueError("备份中有非法文件名")
                info = archive.getinfo("resource/" + filename)
                total += info.file_size
                if info.file_size > self.max_bytes or total > 150 * 1048576:
                    raise ValueError("备份解压后超过 150 MB 或单图大小限制")
                data = archive.read(info)
                inspect_image(data, self.max_bytes)
                tags = tags_from(item.get("tags", []))
                pending.append((data, tags, item))
        mappings = []
        for data, tags, old in pending:
            item, duplicate = self.add(
                data,
                tags,
                str(old.get("owner", "")),
                str(old.get("origin", "")),
                restore_duplicate=False,
            )
            if not duplicate:
                self.annotate(item["id"], old.get("caption", ""), old.get("ocr", ""))
                if old.get("deleted"):
                    self.recycle([item["id"]])
            mappings.append({"old": old.get("id"), "new": item["id"]})
        return mappings

    def cleanup_cache(self):
        for path in self.cache.iterdir():
            if path.is_file() and time.time() - path.stat().st_mtime > 3600:
                path.unlink(missing_ok=True)


@lru_cache(maxsize=16)
def font(size):
    for name in (
        "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def thumbnail(path, size=(300, 210)):
    with Image.open(path) as source:
        if source.width * source.height > MAX_PIXELS:
            raise ValueError("Image exceeds pixel limit")
        image = ImageOps.exif_transpose(source).convert("RGBA")
        image.thumbnail(size)
        tile = Image.new("RGB", size, "#f1f4f9")
        tile.paste(
            image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2), image
        )
    return tile


def render_preview(library, items, query, page, pages, total):
    columns = 3
    rows = (len(items) + columns - 1) // columns
    canvas = Image.new("RGB", (960, 122 + rows * 290), "#eef2f8")
    draw = ImageDraw.Draw(canvas)
    title = "MemeHarbor · " + (query[:60] or "全部图片")
    while draw.textlength(title, font=font(27)) > 910:
        title = title[:-2] + "…"
    draw.text(
        (26, 18),
        title,
        font=font(27),
        fill="#132340",
    )
    draw.text(
        (26, 62),
        f"第 {page}/{pages} 页 · 共 {total} 张  |  发图 编号 · 下一页",
        font=font(19),
        fill="#596778",
    )
    for index, item in enumerate(items):
        x, y = 20 + (index % columns) * 314, 110 + (index // columns) * 290
        draw.rounded_rectangle((x, y, x + 300, y + 274), radius=12, fill="white")
        try:
            tile = thumbnail(library.path(item), (284, 194))
            canvas.paste(tile, (x + 8, y + 8))
        except (OSError, ValueError):
            draw.text(
                (x + 30, y + 70), "原图缺失或无法预览", font=font(18), fill="#b35454"
            )
        draw.text((x + 12, y + 210), f"#{item['id']}", font=font(21), fill="#284fbe")
        label = " · ".join(item["tags"]) or "无标签"
        while draw.textlength(label, font=font(17)) > 273:
            label = label[:-2] + "…"
        draw.text((x + 12, y + 241), label, font=font(17), fill="#526075")
    path = library.cache / f"preview-{uuid.uuid4().hex}.jpg"
    canvas.save(path, quality=87)
    return path
