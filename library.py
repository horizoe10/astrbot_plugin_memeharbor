"""Persistent image catalog and preview rendering, independent of AstrBot."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path, PurePosixPath

from PIL import Image, ImageDraw, ImageFont, ImageOps

EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "GIF": ".gif",
    "WEBP": ".webp",
    "BMP": ".bmp",
}
MAX_PIXELS = 40_000_000
INBOX_TAGS = ("待整理", "临时")
SORTS = ("new", "old", "sent", "inbox")
# 256-bit dHash per frame. Calibrated on a real sticker library: re-encoded
# copies stay within about 16 bits, while different expressions of one character
# on the same background differ by 22+ bits, so 16 keeps such variants apart.
HASH_SIDE = 16
HASH_BITS = HASH_SIDE * HASH_SIDE
HASH_VERSION = "2"
SIMILAR_DISTANCE = 16
# Pigeonhole: two first-frame hashes within 16 bits share one of 17 bands.
BANDS = tuple(
    (
        HASH_BITS * i // (SIMILAR_DISTANCE + 1),
        HASH_BITS * (i + 1) // (SIMILAR_DISTANCE + 1),
    )
    for i in range(SIMILAR_DISTANCE + 1)
)


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


def _dhash(frame) -> int:
    gray = frame.convert("L").resize(
        (HASH_SIDE + 1, HASH_SIDE), Image.Resampling.LANCZOS
    )
    pixels = gray.tobytes()
    bits = 0
    for row in range(HASH_SIDE):
        offset = row * (HASH_SIDE + 1)
        for column in range(HASH_SIDE):
            bits = (bits << 1) | (pixels[offset + column] > pixels[offset + column + 1])
    return bits


def perceptual_hash(source) -> str:
    """First-frame and middle-frame dHash as hex, or '' for flat images.

    Animated images compare two frames, so different GIFs that merely start from
    the same still are not treated as copies.
    """
    with Image.open(
        io.BytesIO(source) if isinstance(source, bytes) else source
    ) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("图片像素过大")
        frames = getattr(image, "n_frames", 1)
        image.seek(0)
        first = _dhash(ImageOps.exif_transpose(image))
        if frames > 1:
            image.seek(frames // 2)
            middle = _dhash(image)
        else:
            middle = first
    # Flat images carry no structure and would all look alike.
    if first.bit_count() < 4 or first.bit_count() > HASH_BITS - 4:
        return ""
    width = HASH_BITS // 4
    return f"{first:0{width}x}{middle:0{width}x}"


def hash_parts(value):
    width = HASH_BITS // 4
    return int(value[:width], 16), int(value[width : width * 2], 16)


def hash_distance(a, b) -> int:
    return max((a[0] ^ b[0]).bit_count(), (a[1] ^ b[1]).bit_count())


def in_inbox(item) -> bool:
    return not item["tags"] or any(tag in INBOX_TAGS for tag in item["tags"])


class Library:
    def __init__(self, root: Path, max_bytes: int = 15 * 1048576):
        self.root = root
        self.resources = root / "resource"
        self.cache = root / "preview_cache"
        self.thumbs = root / "thumb_cache"
        self.db = root / "library_v2.sqlite3"
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._cache = None
        for path in (root, self.resources, self.cache, self.thumbs):
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
                CREATE TABLE IF NOT EXISTS similar_dismissed (
                    a INTEGER NOT NULL,
                    b INTEGER NOT NULL,
                    PRIMARY KEY (a, b)
                );
            """)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(images)")
            }
            if "phash" not in columns:
                connection.execute(
                    "ALTER TABLE images ADD COLUMN phash TEXT NOT NULL DEFAULT ''"
                )
            if "phash_checked" not in columns:
                connection.execute(
                    "ALTER TABLE images ADD COLUMN phash_checked INTEGER NOT NULL DEFAULT 0"
                )
            if "scopes" not in columns:
                # Extra chats that collected the same image (per-group library mode).
                connection.execute(
                    "ALTER TABLE images ADD COLUMN scopes TEXT NOT NULL DEFAULT '[]'"
                )
            version = connection.execute(
                "SELECT value FROM meta WHERE key='phash_version'"
            ).fetchone()
            if not version or version[0] != HASH_VERSION:
                # A new hash algorithm invalidates stored hashes; recompute lazily.
                connection.execute("UPDATE images SET phash='', phash_checked=0")
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('phash_version', ?)",
                    (HASH_VERSION,),
                )

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def invalidate(self):
        with self._lock:
            self._cache = None

    def _signature(self):
        try:
            info = self.db.stat()
            return info.st_mtime_ns, info.st_size
        except OSError:
            return None

    def _entries(self):
        """Parsed catalog, rebuilt only after this library or another writer changes it."""
        signature = self._signature()
        with self._lock:
            if self._cache is not None and self._cache[0] == signature:
                return self._cache[1]
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM images ORDER BY id DESC"
            ).fetchall()
        entries = []
        for row in rows:
            item = self.record(row)
            entries.append(
                (
                    item,
                    [tag.casefold() for tag in item["tags"]],
                    (item["caption"] + " " + item["ocr"]).casefold(),
                )
            )
        with self._lock:
            self._cache = (signature, entries)
        return entries

    @staticmethod
    def record(row):
        if row is None:
            raise ValueError("没有找到这个图片编号")
        item = dict(row)
        item["tags"] = json.loads(item["tags"])
        item["scopes"] = json.loads(item.get("scopes") or "[]")
        item.pop("phash_checked", None)
        return item

    @staticmethod
    def copy(item):
        return dict(item, tags=list(item["tags"]), scopes=list(item["scopes"]))

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
        try:
            phash = perceptual_hash(data)
        except (OSError, ValueError):
            phash = ""
        try:
            return self._add(
                data, extension, labels, digest, phash, owner, origin, restore_duplicate
            )
        finally:
            self.invalidate()

    def _add(
        self, data, extension, labels, digest, phash, owner, origin, restore_duplicate
    ):
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
                scopes = item["scopes"]
                if origin and origin != item["origin"] and origin not in scopes:
                    scopes = scopes + [origin]
                connection.execute(
                    "UPDATE images SET tags=?,scopes=?,deleted=CASE WHEN ? THEN NULL ELSE deleted END WHERE id=?",
                    (
                        json.dumps(labels, ensure_ascii=False),
                        json.dumps(scopes, ensure_ascii=False),
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
                    "INSERT INTO images(filename,digest,tags,created,owner,origin,phash,phash_checked) VALUES(?,?,?,?,?,?,?,1)",
                    (
                        filename,
                        digest,
                        json.dumps(labels, ensure_ascii=False),
                        time.time(),
                        owner,
                        origin,
                        phash,
                    ),
                )
                image_id = cursor.lastrowid
        return self.get(image_id, include_deleted=True), duplicate

    def search(
        self, query="", deleted=False, case_sensitive=False, sort="new", visible=None
    ):
        groups = [
            re.split(r"[\s,，、;；]+", group.strip()) for group in query.split("|")
        ]

        def matches(entry, term):
            item, folded_tags, folded_text = entry
            if term.startswith("#"):
                return str(item["id"]) == term[1:]
            if case_sensitive:
                return term in item["tags"] or term in (
                    item["caption"] + " " + item["ocr"]
                )
            term = term.casefold()
            return term in folded_tags or term in folded_text

        result = []
        for entry in self._entries():
            item = entry[0]
            if bool(item["deleted"]) != deleted:
                continue
            if visible is not None and not visible(item):
                continue
            if any(
                all(
                    not matches(entry, t[1:])
                    if t.startswith("-")
                    else matches(entry, t)
                    for t in terms
                    if t
                )
                for terms in groups
            ):
                result.append(self.copy(item))
        if sort == "old":
            result.reverse()
        elif sort == "sent":
            result.sort(key=lambda item: (-item["sent"], -item["id"]))
        elif sort == "inbox":
            result.sort(key=lambda item: (not in_inbox(item), -item["id"]))
        return result

    def tag_counts(self, visible=None):
        counts = {}
        for item, _, _ in self._entries():
            if item["deleted"] or (visible is not None and not visible(item)):
                continue
            for tag in item["tags"]:
                counts[tag] = counts.get(tag, 0) + 1
        return dict(sorted(counts.items(), key=lambda entry: (-entry[1], entry[0])))

    def stats(self, visible=None):
        active, deleted = [], 0
        for item, _, _ in self._entries():
            if visible is not None and not visible(item):
                continue
            if item["deleted"]:
                deleted += 1
            else:
                active.append(item)
        counts = self.tag_counts(visible)
        week = time.time() - 7 * 86400
        return {
            "total": len(active),
            "deleted": deleted,
            "inbox": sum(in_inbox(item) for item in active),
            "tags": len(counts),
            "sent": sum(item["sent"] for item in active),
            "added_week": sum(item["created"] >= week for item in active),
            "top_tags": [
                {"tag": tag, "count": count}
                for tag, count in list(counts.items())[:12]
                if tag not in INBOX_TAGS
            ],
            "top_sent": [
                self.copy(item)
                for item in sorted(active, key=lambda i: (-i["sent"], -i["id"]))[:8]
                if item["sent"]
            ],
        }

    def update_tags(self, ids, action, labels):
        try:
            return self._update_tags(ids, action, labels)
        finally:
            self.invalidate()

    def _update_tags(self, ids, action, labels):
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
        try:
            return self._rename_tag(old, new)
        finally:
            self.invalidate()

    def _rename_tag(self, old, new):
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
        try:
            return self._recycle(ids, restore)
        finally:
            self.invalidate()

    def _recycle(self, ids, restore=False):
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
        try:
            return self._annotate(image_id, caption, ocr)
        finally:
            self.invalidate()

    def _annotate(self, image_id, caption, ocr):
        with self.connect() as connection:
            connection.execute(
                "UPDATE images SET caption=?,ocr=? WHERE id=?",
                (str(caption)[:1000], str(ocr)[:3000], image_id),
            )

    def mark_sent(self, image_id):
        try:
            return self._mark_sent(image_id)
        finally:
            self.invalidate()

    def _mark_sent(self, image_id):
        with self.connect() as connection:
            connection.execute("UPDATE images SET sent=sent+1 WHERE id=?", (image_id,))

    def thumbnail_data(self, item, size=320):
        """Return a cached WebP thumbnail keyed by content digest."""
        target = self.thumbs / f"{item['digest']}-{size}.webp"
        if not target.is_file():
            with Image.open(self.path(item)) as source:
                if source.width * source.height > MAX_PIXELS:
                    raise ValueError("图片像素过大")
                image = ImageOps.exif_transpose(source).convert("RGBA")
            image.thumbnail((size, size))
            temporary = self.thumbs / f".{uuid.uuid4().hex}.tmp"
            try:
                image.save(temporary, format="WEBP", quality=80, method=4)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return target.read_bytes()

    def original_data(self, item, limit=8 * 1048576):
        """Original bytes for in-page preview (GIFs animate); large files fall back."""
        path = self.path(item)
        if path.stat().st_size <= limit:
            data = path.read_bytes()
            mime = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".bmp": "image/bmp",
            }.get(path.suffix.lower(), "application/octet-stream")
        else:
            data, mime = self.thumbnail_data(item, 1024), "image/webp"
        with Image.open(path) as image:
            width, height = image.size
            frames = getattr(image, "n_frames", 1)
        return (
            data,
            mime,
            {
                "width": width,
                "height": height,
                "bytes": path.stat().st_size,
                "animated": frames > 1,
            },
        )

    def fill_phashes(self, limit=300):
        """Hash images saved before similarity detection existed; return remaining."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, filename FROM images WHERE phash_checked=0 LIMIT ?",
                (limit,),
            ).fetchall()
        updates = []
        for row in rows:
            try:
                value = perceptual_hash(self.path({"filename": row["filename"]}))
            except (OSError, ValueError):
                value = ""
            updates.append((value, row["id"]))
        if updates:
            with self.connect() as connection:
                connection.executemany(
                    "UPDATE images SET phash=?, phash_checked=1 WHERE id=?", updates
                )
            self.invalidate()
        with self.connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM images WHERE phash_checked=0"
            ).fetchone()[0]

    def similar_groups(self, distance=SIMILAR_DISTANCE):
        """Group near-identical active images around one anchor each.

        Members must each be close to the anchor itself; chaining A~B~C is not
        allowed, otherwise a sticker series on one background collapses into
        one huge group.
        """
        items = [
            item
            for item, _, _ in self._entries()
            if not item["deleted"] and len(item["phash"]) == HASH_BITS // 2
        ]
        values = {item["id"]: hash_parts(item["phash"]) for item in items}
        with self.connect() as connection:
            dismissed = {
                (row[0], row[1])
                for row in connection.execute("SELECT a, b FROM similar_dismissed")
            }
        buckets = {}
        for item in items:
            first = values[item["id"]][0]
            for index, (start, end) in enumerate(BANDS):
                part = (first >> (HASH_BITS - end)) & ((1 << (end - start)) - 1)
                buckets.setdefault((index, part), []).append(item["id"])
        neighbours = {item["id"]: set() for item in items}
        checked = set()
        for members in buckets.values():
            # A huge bucket means a generic pattern; comparing it would be quadratic.
            if len(members) < 2 or len(members) > 400:
                continue
            for i, a in enumerate(members):
                for b in members[i + 1 :]:
                    pair = (a, b) if a < b else (b, a)
                    if pair in checked:
                        continue
                    checked.add(pair)
                    if (
                        pair not in dismissed
                        and hash_distance(values[a], values[b]) <= distance
                    ):
                        neighbours[a].add(b)
                        neighbours[b].add(a)
        lookup = {item["id"]: item for item in items}

        def rank(item_id):
            item = lookup[item_id]
            return (in_inbox(item), -item["sent"], -len(item["tags"]), item_id)

        assigned, result = set(), []
        for anchor in sorted((i for i in neighbours if neighbours[i]), key=rank):
            if anchor in assigned:
                continue
            members = [m for m in neighbours[anchor] if m not in assigned]
            if not members:
                continue
            group = [anchor, *sorted(members, key=rank)]
            assigned.update(group)
            result.append([self.copy(lookup[i]) for i in group])
        result.sort(key=lambda group: (-len(group), -max(i["id"] for i in group)))
        return result

    def dismiss_similar(self, ids):
        ids = sorted({int(i) for i in ids})
        if len(ids) < 2:
            raise ValueError("请至少选择两张图片")
        with self.connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO similar_dismissed(a, b) VALUES(?, ?)",
                [(a, b) for index, a in enumerate(ids) for b in ids[index + 1 :]],
            )

    def merge(self, keep, ids):
        try:
            return self._merge(keep, ids)
        finally:
            self.invalidate()

    def _merge(self, keep, ids):
        """Keep one image, absorb the others' tags/notes/counts, recycle the rest."""
        others = [int(i) for i in dict.fromkeys(ids) if int(i) != int(keep)]
        if not others:
            raise ValueError("请至少选择一张要合并的图片")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            def load(image_id):
                item = self.record(
                    connection.execute(
                        "SELECT * FROM images WHERE id=? AND deleted IS NULL",
                        (image_id,),
                    ).fetchone()
                )
                return item

            target = load(int(keep))
            sources = [load(image_id) for image_id in others]
            tags = [t for t in target["tags"] if t not in INBOX_TAGS]
            for source in sources:
                tags.extend(t for t in source["tags"] if t not in INBOX_TAGS)
            tags = list(dict.fromkeys(tags)) or target["tags"]
            if len(tags) > 24:
                raise ValueError("合并后标签超过 24 个，请先整理标签")
            caption = target["caption"] or next(
                (s["caption"] for s in sources if s["caption"]), ""
            )
            ocr = target["ocr"] or next((s["ocr"] for s in sources if s["ocr"]), "")
            connection.execute(
                "UPDATE images SET tags=?, caption=?, ocr=?, sent=? WHERE id=?",
                (
                    json.dumps(tags, ensure_ascii=False),
                    caption,
                    ocr,
                    target["sent"] + sum(s["sent"] for s in sources),
                    target["id"],
                ),
            )
            connection.executemany(
                "UPDATE images SET deleted=? WHERE id=?",
                [(time.time(), source["id"]) for source in sources],
            )
        return len(sources)

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
                data = self.read_archive_image(archive, info)
                inspect_image(data, self.max_bytes)
                tags = tags_from(item.get("tags", []))
                pending.append((info, tags, item))
            mappings = []
            for info, tags, old in pending:
                item, duplicate = self.add(
                    self.read_archive_image(archive, info),
                    tags,
                    str(old.get("owner", "")),
                    str(old.get("origin", "")),
                    restore_duplicate=False,
                )
                if not duplicate:
                    self.annotate(
                        item["id"], old.get("caption", ""), old.get("ocr", "")
                    )
                    if old.get("deleted"):
                        self.recycle([item["id"]])
                mappings.append(
                    {"old": old.get("id"), "new": item["id"], "duplicate": duplicate}
                )
        return mappings

    def read_archive_image(self, archive, info):
        name = info.filename.replace("\\", "/")
        parts = PurePosixPath(name)
        if parts.is_absolute() or ".." in parts.parts or ":" in name:
            raise ValueError("压缩包中有非法路径")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ValueError("不支持压缩包中的符号链接")
        if info.flag_bits & 1:
            raise ValueError("请先解除 ZIP 密码保护")
        if info.file_size > self.max_bytes:
            raise ValueError("图片超过单张大小限制")
        # Read each member with a bound; never extract user-controlled paths.
        with archive.open(info) as stream:
            data = stream.read(self.max_bytes + 1)
        inspect_image(data, self.max_bytes)
        return data

    def import_upload_archive(self, path, owner=""):
        with zipfile.ZipFile(path) as archive:
            if "manifest.json" in archive.namelist():
                # A recognized backup keeps its metadata; unrelated JSON in an
                # ordinary image ZIP does not prevent importing its images.
                info = archive.getinfo("manifest.json")
                if info.file_size > 5 * 1048576:
                    raise ValueError("备份索引过大")
                try:
                    manifest = json.loads(archive.read(info))
                except (ValueError, UnicodeError):
                    manifest = None
                if (
                    isinstance(manifest, dict)
                    and "version" in manifest
                    and "images" in manifest
                ):
                    mapping = self.import_archive(path)
                    duplicates = sum(item["duplicate"] for item in mapping)
                    return dict(
                        imported=len(mapping),
                        added=len(mapping) - duplicates,
                        duplicates=duplicates,
                        skipped=0,
                        failed=0,
                        errors=[],
                    )
            report = dict(
                imported=0, added=0, duplicates=0, skipped=0, failed=0, errors=[]
            )
            candidates = []
            total = 0
            for info in archive.infolist():
                if info.is_dir():
                    continue
                parts = PurePosixPath(info.filename.replace("\\", "/"))
                if (
                    "__MACOSX" in parts.parts
                    or parts.name.startswith("._")
                    or parts.suffix.lower() not in {*EXTENSIONS.values(), ".jpeg"}
                ):
                    report["skipped"] += 1
                    continue
                candidates.append(info)
                total += info.file_size
            if total > 150 * 1048576:
                raise ValueError(
                    "ZIP 图片解压后超过 150 MB，请分包导入或使用文件夹导入"
                )
            for info in candidates:
                try:
                    data = self.read_archive_image(archive, info)
                    _, duplicate = self.add(data, ["待整理"], owner, "web")
                except (
                    ValueError,
                    OSError,
                    RuntimeError,
                    zipfile.BadZipFile,
                    NotImplementedError,
                ) as exc:
                    report["failed"] += 1
                    if len(report["errors"]) < 20:
                        report["errors"].append(f"{info.filename}：{exc}")
                    continue
                report["imported"] += 1
                report["duplicates" if duplicate else "added"] += 1
            return report

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
    title = "表情港湾 MemeHarbor · " + (query[:60] or "全部图片")
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
        f"第 {page}/{pages} 页 · 共 {total} 张 · 发送「发图 编号」取原图",
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
