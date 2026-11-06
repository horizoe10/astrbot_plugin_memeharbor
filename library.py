"""Persistent image catalog and preview rendering, independent of AstrBot."""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import re
import sqlite3
import stat
import threading
import time
import unicodedata
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
SORTS = ("new", "old", "sent", "inbox", "best")
# Fields an undoable operation may change; undo restores only what it changed.
SNAPSHOT_FIELDS = ("tags", "deleted", "caption", "ocr", "sent", "review")
# 256-bit dHash per frame. Calibrated on a real sticker library: re-encoded
# copies stay within about 16 bits, while different expressions of one character
# on the same background differ by 22+ bits, so 16 keeps such variants apart.
HASH_SIDE = 16
HASH_BITS = HASH_SIDE * HASH_SIDE
HASH_VERSION = "3"
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


def _edit_distance(a, b):
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


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


def analyze(source) -> dict:
    """Perceptual hash plus size and frame count, read in one pass.

    The hash is first-frame and middle-frame dHash as hex ('' for flat images).
    Animated images compare two frames, so different GIFs that merely start from
    the same still are not treated as copies.
    """
    with Image.open(
        io.BytesIO(source) if isinstance(source, bytes) else source
    ) as image:
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("图片像素过大")
        width, height = image.size
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
        phash = ""
    else:
        size = HASH_BITS // 4
        phash = f"{first:0{size}x}{middle:0{size}x}"
    return {"phash": phash, "width": width, "height": height, "frames": frames}


def perceptual_hash(source) -> str:
    return analyze(source)["phash"]


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
        self.insights_path = root / "insights.sqlite3"
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
                CREATE TABLE IF NOT EXISTS aliases (
                    alias TEXT PRIMARY KEY,
                    tag TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at REAL NOT NULL,
                    actor TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    before TEXT NOT NULL,
                    after TEXT NOT NULL,
                    undone INTEGER NOT NULL DEFAULT 0
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
            for column in ("width", "height", "frames"):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE images ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                    )
            if "review" not in columns:
                # 1 = submitted by a member and waiting for an admin (review mode).
                connection.execute(
                    "ALTER TABLE images ADD COLUMN review INTEGER NOT NULL DEFAULT 0"
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
        with self.insights_db() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS search_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at REAL NOT NULL,
                    scope TEXT NOT NULL DEFAULT '',
                    query TEXT NOT NULL,
                    results INTEGER NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'search'
                );
                CREATE INDEX IF NOT EXISTS search_at ON search_log(at);
                CREATE TABLE IF NOT EXISTS send_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at REAL NOT NULL,
                    scope TEXT NOT NULL DEFAULT '',
                    image_id INTEGER NOT NULL,
                    digest TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT '',
                    query TEXT NOT NULL DEFAULT '',
                    feedback INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS send_scope_at ON send_log(scope, at);
            """)
        self._fold_alias_tags()

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

    def canonical(self, labels):
        """Store the main tag when someone writes an alias, e.g. 喵 -> 猫."""
        aliases = self.aliases()
        if not aliases:
            return list(labels)
        return list(dict.fromkeys(aliases.get(t.casefold(), t) for t in labels))

    def _fold_alias_tags(self):
        """Repair tags saved under an alias before collecting resolved aliases."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            aliases = {
                row[0]: row[1]
                for row in connection.execute("SELECT alias, tag FROM aliases")
            }
            if not aliases:
                return
            for row in connection.execute("SELECT id, tags FROM images").fetchall():
                tags = json.loads(row["tags"])
                fixed = list(dict.fromkeys(aliases.get(t.casefold(), t) for t in tags))
                if fixed != tags:
                    connection.execute(
                        "UPDATE images SET tags=? WHERE id=?",
                        (json.dumps(fixed, ensure_ascii=False), row["id"]),
                    )
        self.invalidate()

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
            aliases = {
                row[0]: row[1]
                for row in connection.execute("SELECT alias, tag FROM aliases")
            }
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
            self._cache = (signature, entries, aliases)
        return entries

    @staticmethod
    def record(row):
        if row is None:
            raise ValueError("没有找到这个图片编号")
        item = dict(row)
        item["tags"] = json.loads(item["tags"])
        item["scopes"] = json.loads(item.get("scopes") or "[]")
        item.pop("phash_checked", None)
        for key in ("width", "height", "frames", "review"):
            item.setdefault(key, 0)
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

    def add(
        self, data, tags, owner="", origin="", restore_duplicate=True, review=False
    ):
        extension = inspect_image(data, self.max_bytes)
        labels = self.canonical(tags_from(tags))
        digest = hashlib.sha256(data).hexdigest()
        try:
            facts = analyze(data)
        except (OSError, ValueError):
            facts = {"phash": "", "width": 0, "height": 0, "frames": 0}
        try:
            return self._add(
                data,
                extension,
                labels,
                digest,
                facts,
                owner,
                origin,
                restore_duplicate,
                review,
            )
        finally:
            self.invalidate()

    def _add(
        self,
        data,
        extension,
        labels,
        digest,
        facts,
        owner,
        origin,
        restore_duplicate,
        review=False,
    ):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM images WHERE digest=? ORDER BY deleted IS NOT NULL,id LIMIT 1",
                (digest,),
            ).fetchone()
            duplicate = row is not None
            if duplicate and review:
                # A submission waiting for review must not change a known image.
                image_id = row["id"]
            elif duplicate:
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
                    "INSERT INTO images(filename,digest,tags,created,owner,origin,phash,phash_checked,width,height,frames,review) VALUES(?,?,?,?,?,?,?,1,?,?,?,?)",
                    (
                        filename,
                        digest,
                        json.dumps(labels, ensure_ascii=False),
                        time.time(),
                        owner,
                        origin,
                        facts["phash"],
                        facts["width"],
                        facts["height"],
                        facts["frames"],
                        int(bool(review)),
                    ),
                )
                image_id = cursor.lastrowid
        return self.get(image_id, include_deleted=True), duplicate

    def search(
        self,
        query="",
        deleted=False,
        case_sensitive=False,
        sort="new",
        visible=None,
        partial=False,
        review=False,
        boost=None,
    ):
        """Find images; the query syntax is shared by chat and the web page.

        Words in a group must all match, "-word" excludes, "|" separates
        alternatives and "#12" picks an id. A word matches a tag exactly, an
        alias, part of a tag (when partial is on) or the description/OCR text.
        "best" sorts by how well an image matched, then by boost (how often a
        group used it), then newest first.
        """
        groups = [
            [t for t in re.split(r"[\s,，、;；]+", group.strip()) if t]
            for group in query.split("|")
        ]
        entries = self._entries()
        aliases = self.aliases()
        fold = (lambda x: x) if case_sensitive else str.casefold

        def score(entry, term, allow_partial):
            item, folded_tags, folded_text = entry
            if term.startswith("#"):
                return 3 if str(item["id"]) == term[1:] else 0
            key = fold(term)
            tags = item["tags"] if case_sensitive else folded_tags
            if key in tags:
                return 3
            target = aliases.get(term.casefold())
            if target is not None and fold(target) in tags:
                return 2.5
            if allow_partial and any(key in t for t in tags):
                return 1
            text = (
                item["caption"] + " " + item["ocr"] if case_sensitive else folded_text
            )
            return 0.5 if key and key in text else 0

        result, scores = [], {}
        for entry in entries:
            item = entry[0]
            if bool(item["deleted"]) != deleted:
                continue
            if not deleted and bool(item.get("review")) != review:
                continue
            if visible is not None and not visible(item):
                continue
            best = None
            for terms in groups:
                total, ok = 0, True
                for term in terms:
                    if term.startswith("-"):
                        if term[1:] and score(entry, term[1:], False):
                            ok = False
                            break
                        continue
                    value = score(entry, term, partial)
                    if not value:
                        ok = False
                        break
                    total += value
                if ok and (best is None or total > best):
                    best = total
            if best is not None:
                copy = self.copy(item)
                scores[copy["id"]] = best
                result.append(copy)
        if sort == "old":
            result.reverse()
        elif sort == "sent":
            result.sort(key=lambda item: (-item["sent"], -item["id"]))
        elif sort == "inbox":
            result.sort(key=lambda item: (not in_inbox(item), -item["id"]))
        elif sort == "best":
            boost = boost or {}
            result.sort(
                key=lambda item: (
                    -scores[item["id"]],
                    -boost.get(item["id"], 0),
                    -item["id"],
                )
            )
        return result

    def tag_counts(self, visible=None):
        counts = {}
        for item, _, _ in self._entries():
            if (
                item["deleted"]
                or item.get("review")
                or (visible is not None and not visible(item))
            ):
                continue
            for tag in item["tags"]:
                counts[tag] = counts.get(tag, 0) + 1
        return dict(sorted(counts.items(), key=lambda entry: (-entry[1], entry[0])))

    def stats(self, visible=None):
        active, deleted, review = [], 0, 0
        for item, _, _ in self._entries():
            if visible is not None and not visible(item):
                continue
            if item["deleted"]:
                deleted += 1
            elif item.get("review"):
                review += 1
            else:
                active.append(item)
        counts = self.tag_counts(visible)
        week = time.time() - 7 * 86400
        return {
            "total": len(active),
            "deleted": deleted,
            "review": review,
            "inbox": sum(in_inbox(item) for item in active),
            "auto": sum(in_inbox(item) and item["owner"] == "auto" for item in active),
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
        if action != "remove":
            labels = self.canonical(labels)
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
        labels = self.canonical(tags_from(new))
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
        # Sending is frequent: update the cached entry in place instead of
        # re-parsing the whole catalog after every image a group receives.
        with self._lock:
            fresh = self._cache is not None and self._cache[0] == self._signature()
        with self.connect() as connection:
            connection.execute("UPDATE images SET sent=sent+1 WHERE id=?", (image_id,))
        with self._lock:
            if fresh and self._cache is not None:
                for item, _, _ in self._cache[1]:
                    if item["id"] == int(image_id):
                        item["sent"] += 1
                        break
                self._cache = (self._signature(), *self._cache[1:])
            else:
                self._cache = None

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
                facts = analyze(self.path({"filename": row["filename"]}))
            except (OSError, ValueError):
                facts = {"phash": "", "width": 0, "height": 0, "frames": 0}
            updates.append(
                (
                    facts["phash"],
                    facts["width"],
                    facts["height"],
                    facts["frames"],
                    row["id"],
                )
            )
        if updates:
            with self.connect() as connection:
                connection.executemany(
                    "UPDATE images SET phash=?, width=?, height=?, frames=?, phash_checked=1 WHERE id=?",
                    updates,
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
            if not item["deleted"]
            and not item.get("review")
            and len(item["phash"]) == HASH_BITS // 2
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

    # ---------- tag aliases ----------

    def aliases(self):
        self._entries()
        with self._lock:
            return dict(self._cache[2]) if self._cache else {}

    def alias_target(self, word):
        return self.aliases().get(word.casefold())

    def set_alias(self, alias, tag):
        alias_words, tag_words = tags_from(alias), tags_from(tag)
        if len(alias_words) != 1 or len(tag_words) != 1:
            raise ValueError("别名和目标标签都只能是一个词")
        alias, tag = alias_words[0], tag_words[0]
        if alias.casefold() == tag.casefold():
            raise ValueError("别名不能和目标标签相同")
        existing = self.aliases()
        if tag.casefold() in existing:
            raise ValueError(
                f"「{tag}」本身是别名，请直接指向「{existing[tag.casefold()]}」"
            )
        counts = {t.casefold(): n for t, n in self.tag_counts().items()}
        if alias.casefold() in counts:
            raise ValueError(f"「{alias}」本身已经是标签，请改用合并标签")
        if tag.casefold() not in counts:
            raise ValueError(f"图库里还没有「{tag}」这个标签")
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO aliases(alias, tag) VALUES(?, ?)",
                (alias.casefold(), tag),
            )
        self.invalidate()
        return alias, tag

    def remove_alias(self, alias):
        with self.connect() as connection:
            removed = connection.execute(
                "DELETE FROM aliases WHERE alias=?", (str(alias).casefold(),)
            ).rowcount
        self.invalidate()
        if not removed:
            raise ValueError("没有找到这个别名")

    # ---------- chat search log (separate file, so it never invalidates the index) ----------

    @contextmanager
    def insights_db(self):
        connection = sqlite3.connect(self.insights_path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def record_search(self, scope, query, results, kind="search"):
        query = re.sub(r"\s+", " ", str(query)).strip()[:200]
        if not query:
            return
        with self.insights_db() as connection:
            connection.execute(
                "INSERT INTO search_log(at, scope, query, results, kind) VALUES(?,?,?,?,?)",
                (time.time(), scope, query, int(results), kind),
            )
            connection.execute(
                "DELETE FROM search_log WHERE id <= (SELECT MAX(id) FROM search_log) - 20000"
            )

    @staticmethod
    def query_terms(query):
        return [
            term.lstrip("-")
            for term in re.split(r"[\s,，、;；|]+", query)
            if term.lstrip("-") and not term.startswith("#")
        ]

    def searched_terms(self, days=30):
        since = time.time() - days * 86400
        counts = {}
        with self.insights_db() as connection:
            for (query,) in connection.execute(
                "SELECT query FROM search_log WHERE at >= ?", (since,)
            ):
                for term in self.query_terms(query):
                    key = term.casefold()
                    counts[key] = counts.get(key, 0) + 1
        return counts

    def insights(self, days=30):
        since = time.time() - days * 86400
        with self.insights_db() as connection:
            rows = connection.execute(
                "SELECT at, scope, query, results, kind FROM search_log WHERE at >= ? ORDER BY at",
                (since,),
            ).fetchall()
        top, misses, daily = {}, {}, {}
        for at, scope, query, results, _kind in rows:
            entry = top.setdefault(query, {"query": query, "count": 0, "last": 0})
            entry["count"] += 1
            entry["last"] = at
            day = time.strftime("%m-%d", time.localtime(at))
            daily[day] = daily.get(day, 0) + 1
            if results == 0:
                miss = misses.setdefault(
                    query, {"query": query, "count": 0, "last": 0, "chats": set()}
                )
                miss["count"] += 1
                miss["last"] = at
                miss["chats"].add(scope)
        missed = []
        for miss in sorted(misses.values(), key=lambda m: (-m["count"], -m["last"])):
            # A query that finds images now has been filled since it missed.
            now = len(self.search(miss["query"]))
            missed.append(
                {
                    "query": miss["query"],
                    "count": miss["count"],
                    "last": miss["last"],
                    "chats": len(miss["chats"]),
                    "now": now,
                }
            )
        return {
            "days": days,
            "searches": len(rows),
            "top": sorted(top.values(), key=lambda t: (-t["count"], -t["last"]))[:20],
            "misses": missed[:30],
            "daily": [{"day": d, "count": n} for d, n in daily.items()][-14:],
        }

    # ---------- tag management ----------

    def tag_overview(self, days=30):
        searched = self.searched_terms(days)
        aliases = {}
        for alias, tag in self.aliases().items():
            aliases.setdefault(tag, []).append(alias)
        tags = {}
        for item, _, _ in self._entries():
            if item["deleted"] or item.get("review"):
                continue
            for tag in item["tags"]:
                info = tags.setdefault(
                    tag, {"tag": tag, "count": 0, "sent": 0, "latest": 0, "cover": None}
                )
                info["count"] += 1
                info["sent"] += item["sent"]
                info["latest"] = max(info["latest"], item["created"])
                best = info["cover"]
                if best is None or (item["sent"], item["id"]) > (
                    best["sent"],
                    best["id"],
                ):
                    info["cover"] = {"id": item["id"], "sent": item["sent"]}
        result = []
        for info in tags.values():
            info["cover"] = info["cover"]["id"]
            info["aliases"] = sorted(aliases.get(info["tag"], []))
            info["searches"] = searched.get(info["tag"].casefold(), 0) + sum(
                searched.get(a, 0) for a in info["aliases"]
            )
            info["inbox"] = info["tag"] in INBOX_TAGS
            result.append(info)
        return sorted(result, key=lambda t: (-t["count"], t["tag"]))

    @staticmethod
    def _tag_key(tag):
        text = unicodedata.normalize("NFKC", tag).casefold()
        text = re.sub(r"[\s~～!！?？.。…,，、·'\"]+", "", text)
        return re.sub(r"(.)\1+", r"\1", text)

    @staticmethod
    def _group_cooccurring(pairs, overview):
        """Fold tags that always appear together into one suggestion per group.

        Four tags used on the same images would otherwise produce six
        pairwise suggestions with conflicting targets. Each group keeps its
        biggest tag and points every other member at it.
        """
        together = [p for p in pairs if p["reason"] == "几乎总是一起出现"]
        if not together:
            return pairs
        parent = {}

        def find(tag):
            parent.setdefault(tag, tag)
            while parent[tag] != tag:
                parent[tag] = parent[parent[tag]]
                tag = parent[tag]
            return tag

        for pair in together:
            parent[find(pair["other"])] = find(pair["keep"])
        info = {t["tag"]: (i, t) for i, t in enumerate(overview)}
        groups = {}
        for tag in parent:
            groups.setdefault(find(tag), []).append(tag)
        grouped = []
        for members in groups.values():
            members.sort(
                key=lambda t: (-info[t][1]["count"], -info[t][1]["sent"], info[t][0])
            )
            keep = info[members[0]][1]
            for name in members[1:]:
                other = info[name][1]
                grouped.append(
                    {
                        "keep": keep["tag"],
                        "other": other["tag"],
                        "reason": "几乎总是一起出现",
                        "keep_count": keep["count"],
                        "other_count": other["count"],
                    }
                )
        result, seen = [], set()
        for pair in [p for p in pairs if p["reason"] != "几乎总是一起出现"] + grouped:
            key = frozenset((pair["keep"], pair["other"]))
            if key not in seen:
                seen.add(key)
                result.append(pair)
        return result

    def tag_health(self, days=30):
        overview = [t for t in self.tag_overview(days) if not t["inbox"]]
        members = {}
        for item, _, _ in self._entries():
            if not item["deleted"] and not item.get("review"):
                for tag in item["tags"]:
                    members.setdefault(tag, set()).add(item["id"])
        pairs, seen = [], set()

        def add(a, b, reason):
            key = tuple(sorted((a["tag"], b["tag"])))
            if key in seen:
                return
            seen.add(key)
            keep, other = (
                (a, b) if (a["count"], a["sent"]) >= (b["count"], b["sent"]) else (b, a)
            )
            pairs.append(
                {
                    "keep": keep["tag"],
                    "other": other["tag"],
                    "reason": reason,
                    "keep_count": keep["count"],
                    "other_count": other["count"],
                }
            )

        by_key = {}
        for tag in overview:
            by_key.setdefault(self._tag_key(tag["tag"]), []).append(tag)
        for group in by_key.values():
            for i, a in enumerate(group):
                for b in group[i + 1 :]:
                    add(a, b, "写法不同")
        for i, a in enumerate(overview[:600]):
            for b in overview[i + 1 : 600]:
                sa, sb = members[a["tag"]], members[b["tag"]]
                if min(len(sa), len(sb)) >= 3 and len(sa & sb) / len(sa | sb) >= 0.8:
                    add(a, b, "几乎总是一起出现")
                elif (
                    min(len(a["tag"]), len(b["tag"])) >= 3
                    and abs(len(a["tag"]) - len(b["tag"])) <= 1
                    and _edit_distance(a["tag"].casefold(), b["tag"].casefold()) == 1
                ):
                    add(a, b, "只差一个字")
        pairs = self._group_cooccurring(pairs, overview)
        has_history = bool(self.searched_terms(days))
        return {
            "duplicates": pairs[:50],
            "singletons": [t["tag"] for t in overview if t["count"] == 1][:100],
            "unsearched": [t["tag"] for t in overview if not t["searches"]][:100]
            if has_history
            else [],
            "has_history": has_history,
        }

    def finish(self, ids, labels):
        try:
            return self._finish(ids, labels)
        finally:
            self.invalidate()

    def _finish(self, ids, labels):
        """Add tags and take images out of the inbox in one undoable step."""
        labels = [t for t in self.canonical(tags_from(labels)) if t not in INBOX_TAGS]
        if not labels:
            raise ValueError("请先输入标签")
        changed = 0
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for image_id in ids:
                row = connection.execute(
                    "SELECT * FROM images WHERE id=? AND deleted IS NULL",
                    (int(image_id),),
                ).fetchone()
                if row is None:
                    # Trashed or merged since the page loaded; tag the rest.
                    continue
                item = self.record(row)
                tags = list(
                    dict.fromkeys(
                        [t for t in item["tags"] if t not in INBOX_TAGS] + labels
                    )
                )
                if len(tags) > 24:
                    raise ValueError("一张图片最多支持 24 个标签")
                connection.execute(
                    "UPDATE images SET tags=? WHERE id=?",
                    (json.dumps(tags, ensure_ascii=False), int(image_id)),
                )
                changed += 1
        if not changed:
            raise ValueError("这些图片已经不在图库里了，请刷新页面")
        return changed

    def merge_tags(self, sources, target):
        """Fold several tags into one across all active images."""
        changed = 0
        for source in dict.fromkeys(sources):
            if source != target:
                changed += self.rename_tag(source, target)
        return changed

    # ---------- triage helpers ----------

    def recommendations(self, image_id, limit=9):
        item = self.get(image_id, True)
        own = set(item["tags"]) | set(INBOX_TAGS)
        scores, reasons = {}, {}

        def vote(tag, weight, reason):
            if tag in own:
                return
            scores[tag] = scores.get(tag, 0) + weight
            reasons.setdefault(tag, reason)

        entries = [e[0] for e in self._entries() if not e[0]["deleted"]]
        if len(item["phash"]) == HASH_BITS // 2:
            mine = hash_parts(item["phash"])
            near = sorted(
                (hash_distance(mine, hash_parts(o["phash"])), o["id"], o)
                for o in entries
                if o["id"] != item["id"] and len(o["phash"]) == HASH_BITS // 2
            )
            for distance, _, other in near[:8]:
                if distance <= 70:
                    for tag in other["tags"]:
                        vote(tag, 4 - distance / 25, "长得像的图用过")
        same_owner = [
            o
            for o in entries
            if item["owner"] and o["owner"] == item["owner"] and o["id"] != item["id"]
        ]
        for other in sorted(same_owner, key=lambda o: -o["created"])[:20]:
            for tag in other["tags"]:
                vote(tag, 1.2, "同一人最近用过")
        same_chat = [
            o
            for o in entries
            if item["origin"]
            and o["origin"] == item["origin"]
            and o["id"] != item["id"]
        ]
        for other in sorted(same_chat, key=lambda o: -o["created"])[:30]:
            for tag in other["tags"]:
                vote(tag, 0.8, "这个群最近常用")
        for rank, tag in enumerate(list(self.tag_counts())[:12]):
            vote(tag, 0.6 - rank * 0.03, "常用标签")
        ordered = sorted(scores, key=lambda t: -scores[t])[:limit]
        return [{"tag": tag, "reason": reasons[tag]} for tag in ordered]

    def inbox_clusters(self, distance=45, limit=1000):
        """Group inbox images that look like one series, for tagging together."""
        items = [
            e[0]
            for e in self._entries()
            if not e[0]["deleted"]
            and in_inbox(e[0])
            and len(e[0]["phash"]) == HASH_BITS // 2
        ][:limit]
        values = {i["id"]: hash_parts(i["phash"]) for i in items}
        assigned, clusters = set(), []
        for anchor in items:
            if anchor["id"] in assigned:
                continue
            group = [anchor] + [
                other
                for other in items
                if other["id"] != anchor["id"]
                and other["id"] not in assigned
                and hash_distance(values[anchor["id"]], values[other["id"]]) <= distance
            ]
            if len(group) > 1:
                assigned.update(i["id"] for i in group)
                clusters.append([self.copy(i) for i in group])
        clusters.sort(key=lambda g: -len(g))
        return clusters

    def recent_collections(self, days=7):
        since = time.time() - days * 86400
        counts = {}
        for item, _, _ in self._entries():
            if item["created"] >= since and not item["deleted"]:
                counts[item["origin"]] = counts.get(item["origin"], 0) + 1
        return sorted(counts.items(), key=lambda entry: -entry[1])

    # ---------- chat helpers: suggestions, look-alikes, sending ----------

    def suggest(self, term, visible=None, limit=5):
        """Existing tags close to a word that found nothing: contained or a typo away."""
        word = term.strip().lstrip("-").casefold()
        if not word or word.startswith("#"):
            return []
        counts = {
            t: n for t, n in self.tag_counts(visible).items() if t not in INBOX_TAGS
        }
        ranked = []
        for tag, count in counts.items():
            key = tag.casefold()
            if key == word:
                continue
            if word in key or (len(key) >= 2 and key in word):
                ranked.append((0, -count, tag))
                continue
            allowed = 1 if len(word) <= 3 else 2
            if abs(len(key) - len(word)) <= allowed:
                distance = _edit_distance(word, key)
                if distance <= allowed:
                    ranked.append((distance, -count, tag))
        for alias, tag in self.aliases().items():
            if tag in counts and (word in alias or alias in word):
                ranked.append((0, -counts[tag], tag))
        seen, result = set(), []
        for _, negative, tag in sorted(ranked):
            if tag not in seen:
                seen.add(tag)
                result.append((tag, -negative))
        return result[:limit]

    def nearest(self, phash, visible=None, limit=5, exclude=()):
        """Active images closest to a perceptual hash, as (distance, item)."""
        if len(phash or "") != HASH_BITS // 2:
            return []
        mine = hash_parts(phash)
        found = []
        for item, _, _ in self._entries():
            if (
                item["deleted"]
                or item.get("review")
                or item["id"] in exclude
                or len(item["phash"]) != HASH_BITS // 2
                or (visible is not None and not visible(item))
            ):
                continue
            found.append((hash_distance(mine, hash_parts(item["phash"])), item))
        found.sort(key=lambda entry: (entry[0], -entry[1]["id"]))
        return [(d, self.copy(i)) for d, i in found[:limit]]

    def image_reply(self, phash, visible=None, digest=""):
        """Answer a picture with another one: guess its tags from look-alikes.

        Returns (candidates, tags). The look-alikes themselves are skipped, so
        the group never gets its own picture back.
        """
        near = self.nearest(phash, visible, limit=8)
        votes = {}
        for distance, item in near:
            if distance > 70:
                break
            weight = 3 if distance <= SIMILAR_DISTANCE else (70 - distance) / 54
            for tag in item["tags"]:
                if tag not in INBOX_TAGS:
                    votes[tag] = votes.get(tag, 0) + weight
        if not votes:
            return [], []
        ranked = sorted(votes, key=lambda t: -votes[t])
        # A second tag only counts when look-alikes agree on it nearly as much.
        top = [t for t in ranked[:2] if votes[t] >= votes[ranked[0]] * 0.6]
        same = {item["id"] for distance, item in near if distance <= SIMILAR_DISTANCE}
        pool = []
        for item, _, _ in self._entries():
            if (
                item["deleted"]
                or item.get("review")
                or item["id"] in same
                or (digest and item["digest"] == digest)
                or (visible is not None and not visible(item))
            ):
                continue
            overlap = len(set(top) & set(item["tags"]))
            if overlap:
                pool.append((overlap, item))
        best = max((overlap for overlap, _ in pool), default=0)
        return [self.copy(i) for overlap, i in pool if overlap == best], top

    def record_send(self, scope, item, kind="", query=""):
        with self.insights_db() as connection:
            connection.execute(
                "INSERT INTO send_log(at, scope, image_id, digest, kind, query) VALUES(?,?,?,?,?,?)",
                (
                    time.time(),
                    scope,
                    int(item["id"]),
                    item["digest"],
                    kind,
                    query[:200],
                ),
            )
            connection.execute(
                "DELETE FROM send_log WHERE id <= (SELECT MAX(id) FROM send_log) - 50000"
            )

    def feedback(self, scope, image_ids, value):
        """Mark the latest send of these images in a group: -1 swapped away, +1 liked."""
        with self.insights_db() as connection:
            for image_id in image_ids:
                connection.execute(
                    "UPDATE send_log SET feedback=? WHERE id=(SELECT MAX(id) FROM send_log WHERE scope=? AND image_id=?)",
                    (int(value), scope, int(image_id)),
                )

    def sent_recently(self, scope, hours):
        """Digest -> last send time in this group within the window."""
        since = time.time() - max(0, hours) * 3600
        with self.insights_db() as connection:
            return {
                digest: at
                for digest, at in connection.execute(
                    "SELECT digest, MAX(at) FROM send_log WHERE scope=? AND at>=? GROUP BY digest",
                    (scope, since),
                )
            }

    def send_stats(self, scope=None, days=30):
        """Image id -> {sends, swaps, likes} over recent days, per group or overall."""
        since = time.time() - days * 86400
        sql = "SELECT image_id, COUNT(*), SUM(feedback<0), SUM(feedback>0) FROM send_log WHERE at>=?"
        params = [since]
        if scope is not None:
            sql += " AND scope=?"
            params.append(scope)
        with self.insights_db() as connection:
            rows = connection.execute(sql + " GROUP BY image_id", params).fetchall()
        return {
            r[0]: {"sends": r[1], "swaps": r[2] or 0, "likes": r[3] or 0} for r in rows
        }

    def pick(self, items, count, scope, hours=6, rng=random):
        """Choose images for a group: skip recent ones, prefer the less used.

        Images sent here within the window are used only when nothing else is
        left (oldest first). Among the rest, each image's chance falls with how
        often this group already got it and how often it was swapped away, and
        rises a little for new and liked images.
        """
        unique = list({item["digest"]: item for item in items}.values())
        if not unique:
            return []
        recent = self.sent_recently(scope, hours) if hours else {}
        stats = self.send_stats(scope)
        fresh = [i for i in unique if i["digest"] not in recent]
        older = sorted(
            (i for i in unique if i["digest"] in recent),
            key=lambda i: recent[i["digest"]],
        )
        week = time.time() - 7 * 86400

        def weight(item):
            info = stats.get(item["id"], {})
            value = 1 / (1 + info.get("sends", 0)) ** 0.7
            value *= 0.6 ** info.get("swaps", 0) * 1.3 ** min(3, info.get("likes", 0))
            return value * (1.5 if item["created"] >= week else 1)

        chosen = []
        pool = list(fresh)
        while pool and len(chosen) < count:
            weights = [weight(i) for i in pool]
            index = rng.choices(range(len(pool)), weights)[0]
            chosen.append(pool.pop(index))
        return (chosen + older)[:count]

    def top_sent(self, days=30, limit=24):
        stats = self.send_stats(None, days)
        ranked = sorted(stats.items(), key=lambda e: (-e[1]["sends"], e[0]))
        result = []
        for image_id, info in ranked:
            try:
                item = self.get(image_id)
            except ValueError:
                continue
            result.append(dict(self.copy(item), **info))
            if len(result) >= limit:
                break
        return result

    def sendable(self, item, limit):
        """A file within the platform size limit; larger images get a smaller copy."""
        path = self.path(item)
        if not limit or path.stat().st_size <= limit:
            return path
        folder = self.root / "send_cache"
        folder.mkdir(exist_ok=True)
        with Image.open(path) as source:
            animated = getattr(source, "n_frames", 1) > 1
            alpha = source.mode in ("RGBA", "LA", "P") and not animated
        suffix = ".gif" if animated else (".png" if alpha else ".jpg")
        target = folder / f"{item['digest']}-{limit}{suffix}"
        if target.is_file():
            return target
        scale = 1.0
        for _ in range(8):
            scale *= 0.8
            buffer = io.BytesIO()
            with Image.open(path) as source:
                size = (
                    max(16, int(source.width * scale)),
                    max(16, int(source.height * scale)),
                )
                if animated:
                    frames, durations = [], []
                    for index in range(source.n_frames):
                        source.seek(index)
                        frames.append(source.convert("RGBA").resize(size))
                        durations.append(source.info.get("duration", 80))
                    frames[0].save(
                        buffer,
                        format="GIF",
                        save_all=True,
                        append_images=frames[1:],
                        duration=durations,
                        loop=source.info.get("loop", 0),
                        disposal=2,
                        optimize=True,
                    )
                elif alpha:
                    source.convert("RGBA").resize(size).save(
                        buffer, format="PNG", optimize=True
                    )
                else:
                    source.convert("RGB").resize(size).save(
                        buffer, format="JPEG", quality=85
                    )
            if buffer.tell() <= limit:
                temporary = folder / f".{uuid.uuid4().hex}.tmp"
                temporary.write_bytes(buffer.getvalue())
                os.replace(temporary, target)
                cached = sorted(folder.glob("*-*.*"), key=lambda p: p.stat().st_mtime)
                for old in cached[:-200]:
                    old.unlink(missing_ok=True)
                return target
        return path

    def set_review(self, ids, approve):
        """Approve submissions, or reject them into the trash (they stay unreviewed)."""
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                changed = 0
                for image_id in ids:
                    if approve:
                        cursor = connection.execute(
                            "UPDATE images SET review=0 WHERE id=? AND review=1",
                            (int(image_id),),
                        )
                    else:
                        cursor = connection.execute(
                            "UPDATE images SET deleted=? WHERE id=? AND review=1 AND deleted IS NULL",
                            (time.time(), int(image_id)),
                        )
                    changed += cursor.rowcount
            return changed
        finally:
            self.invalidate()

    def backup(self, folder, keep=3):
        """Write a dated backup into folder and keep only the newest few."""
        folder.mkdir(parents=True, exist_ok=True)
        path = self.export()
        target = folder / time.strftime("memeharbor-backup-%Y%m%d-%H%M%S.zip")
        os.replace(path, target)
        backups = sorted(folder.glob("memeharbor-backup-*.zip"))
        for old in backups[: max(0, len(backups) - max(1, keep))]:
            old.unlink(missing_ok=True)
        return target

    @staticmethod
    def last_backup(folder):
        backups = (
            sorted(folder.glob("memeharbor-backup-*.zip")) if folder.is_dir() else []
        )
        return backups[-1].stat().st_mtime if backups else None

    # ---------- undo log for web edits ----------

    def snapshot(self, ids):
        ids = [int(i) for i in ids]
        result = {}
        with self.connect() as connection:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                for row in connection.execute(
                    f"SELECT id, {', '.join(SNAPSHOT_FIELDS)} FROM images WHERE id IN ({','.join('?' * len(chunk))})",
                    chunk,
                ):
                    state = {field: row[field] for field in SNAPSHOT_FIELDS}
                    state["tags"] = json.loads(row["tags"])
                    result[str(row["id"])] = state
        return result

    def tagged_ids(self, tags):
        wanted = set(tags)
        return [
            e[0]["id"]
            for e in self._entries()
            if not e[0]["deleted"] and wanted & set(e[0]["tags"])
        ]

    def log_operation(self, actor, action, summary, before, after):
        changed = {k: v for k, v in after.items() if before.get(k) != v}
        if not changed:
            return None
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO operations(at, actor, action, summary, before, after) VALUES(?,?,?,?,?,?)",
                (
                    time.time(),
                    actor,
                    action,
                    summary,
                    json.dumps({k: before[k] for k in changed}, ensure_ascii=False),
                    json.dumps(changed, ensure_ascii=False),
                ),
            )
            connection.execute(
                "DELETE FROM operations WHERE id <= (SELECT MAX(id) FROM operations) - 200"
            )
        self.invalidate()
        return cursor.lastrowid

    def operations(self, limit=20):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, at, actor, action, summary, undone, after FROM operations ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "at": r["at"],
                "actor": r["actor"],
                "action": r["action"],
                "summary": r["summary"],
                "undone": bool(r["undone"]),
                "count": len(json.loads(r["after"])),
            }
            for r in rows
        ]

    def undo(self, operation_id):
        """Restore images still exactly as the operation left them; skip later edits."""
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM operations WHERE id=?", (int(operation_id),)
                ).fetchone()
                if row is None:
                    raise ValueError("没有找到这次操作")
                if row["undone"]:
                    raise ValueError("这次操作已经撤销过了")
                before, after = json.loads(row["before"]), json.loads(row["after"])
                restored = conflicts = 0
                for key, state in after.items():
                    current = connection.execute(
                        f"SELECT {', '.join(SNAPSHOT_FIELDS)} FROM images WHERE id=?",
                        (int(key),),
                    ).fetchone()
                    if current is None:
                        conflicts += 1
                        continue
                    old = before[key]
                    # Only compare and restore what this operation changed, so a
                    # later send (which bumps "sent") does not block undoing tags.
                    fields = [
                        f
                        for f in SNAPSHOT_FIELDS
                        if f in state and old.get(f) != state[f]
                    ]
                    if not fields:
                        restored += 1
                        continue
                    now = {f: current[f] for f in fields}
                    if "tags" in now:
                        now["tags"] = json.loads(now["tags"])
                    if any(now[f] != state[f] for f in fields):
                        conflicts += 1
                        continue
                    values = [
                        json.dumps(old[f], ensure_ascii=False)
                        if f == "tags"
                        else old[f]
                        for f in fields
                    ]
                    connection.execute(
                        f"UPDATE images SET {', '.join(f + '=?' for f in fields)} WHERE id=?",
                        (*values, int(key)),
                    )
                    restored += 1
                connection.execute(
                    "UPDATE operations SET undone=1 WHERE id=?", (row["id"],)
                )
            return {
                "restored": restored,
                "conflicts": conflicts,
                "summary": row["summary"],
            }
        finally:
            self.invalidate()

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
        f"第 {page}/{pages} 页 · 共 {total} 张 · 回复序号取原图，例如「1」或「发 1 3」",
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
        # Big position number for "reply 3"; the permanent id stays readable.
        draw.ellipse(
            (x + 14, y + 14, x + 58, y + 58), fill="#2f6bea", outline="white", width=3
        )
        number = str(index + 1)
        width = draw.textlength(number, font=font(26))
        draw.text((x + 36 - width / 2, y + 18), number, font=font(26), fill="white")
        draw.text(
            (x + 12, y + 212), f"编号 {item['id']}", font=font(18), fill="#8a96a8"
        )
        label = " · ".join(item["tags"]) or "无标签"
        while draw.textlength(label, font=font(17)) > 273:
            label = label[:-2] + "…"
        draw.text((x + 12, y + 241), label, font=font(17), fill="#526075")
    path = library.cache / f"preview-{uuid.uuid4().hex}.jpg"
    canvas.save(path, quality=87)
    return path
