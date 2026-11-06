"""Authenticated AstrBot Plugin Pages endpoints for the gallery."""

import asyncio
import base64
import math
import uuid

from astrbot.api.web import error_response, file_response, json_response, request

from .library import INBOX_TAGS, SORTS, in_inbox, tags_from

PLUGIN = "astrbot_plugin_memeharbor"
VIEWS = ("all", "inbox", "trash")
PAGE_SIZES = (24, 48, 96)
MAX_BATCH = 10000


def data_url(data, mime="image/webp"):
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def origin_label(origin):
    if not origin:
        return "早期图库"
    if origin == "web":
        return "网页导入"
    kind, _, chat = origin.rpartition(":")
    if kind.endswith(":group"):
        return f"群 {chat}"
    if kind.endswith(":private"):
        return "私聊"
    return origin


class GalleryAPI:
    def register_gallery(self, context):
        for endpoint, handler, methods in (
            ("gallery", self.gallery_list, ["GET"]),
            ("tags", self.gallery_tags, ["GET"]),
            ("image/<image_id>", self.gallery_image, ["GET"]),
            ("stats", self.gallery_stats, ["GET"]),
            ("similar", self.gallery_similar, ["GET"]),
            ("action", self.gallery_action, ["POST"]),
            ("upload", self.gallery_upload, ["POST"]),
            ("export", self.gallery_export, ["GET"]),
            ("file/<image_id>", self.gallery_file, ["GET"]),
        ):
            context.register_web_api(
                f"/{PLUGIN}/{endpoint}", handler, methods, "MemeHarbor gallery"
            )

    @staticmethod
    def check_login():
        if not request.username:
            raise ValueError("请通过已登录的 AstrBot 管理面板打开图库")

    def card(self, item):
        """Public fields for one image card; thumbnails come from the disk cache."""
        public = {
            key: item[key]
            for key in ("id", "tags", "created", "caption", "ocr", "sent", "deleted")
        }
        public["gif"] = item["filename"].lower().endswith(".gif")
        public["inbox"] = in_inbox(item)
        public["origin"] = origin_label(item["origin"])
        try:
            public["thumbnail"] = data_url(self.library.thumbnail_data(item))
        except (OSError, ValueError):
            public["thumbnail"] = None
        return public

    def select(self, query, view, sort="new"):
        if view not in VIEWS:
            raise ValueError("未知的图库视图")
        if sort not in SORTS:
            raise ValueError("未知的排序方式")
        if len(query) > 200:
            raise ValueError("搜索条件请控制在 200 字以内")
        items = self.library.search(
            query,
            view == "trash",
            bool(self.config.get("case_sensitive", False)),
            sort,
        )
        return [item for item in items if in_inbox(item)] if view == "inbox" else items

    async def gallery_list(self):
        try:
            self.check_login()
            query = str(request.query.get("q", ""))
            view = str(request.query.get("view", "all"))
            sort = str(request.query.get("sort", "new"))
            size = request.query.get("page_size", 48, type=int)
            size = size if size in PAGE_SIZES else 48
            page = max(1, request.query.get("page", 1, type=int))

            def build():
                items = self.select(query, view, sort)
                pages = max(1, math.ceil(len(items) / size))
                current = min(page, pages)
                stats = self.library.stats()
                return {
                    "items": [
                        self.card(item)
                        for item in items[(current - 1) * size : current * size]
                    ],
                    "total": len(items),
                    "page": current,
                    "pages": pages,
                    "page_size": size,
                    "counts": {
                        "all": stats["total"],
                        "inbox": stats["inbox"],
                        "trash": stats["deleted"],
                    },
                    "ai_enabled": bool(self.config.get("ai_enabled", False)),
                }

            return json_response(await asyncio.to_thread(build))
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def gallery_tags(self):
        try:
            self.check_login()
            counts = await asyncio.to_thread(self.library.tag_counts)
            return json_response(
                {
                    "tags": [{"tag": tag, "count": n} for tag, n in counts.items()],
                    "inbox_tags": list(INBOX_TAGS),
                }
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def gallery_image(self, image_id):
        try:
            self.check_login()

            def build():
                item = self.library.get(image_id, True)
                data, mime, meta = self.library.original_data(item)
                return dict(
                    self.card(item),
                    preview=data_url(data, mime),
                    owner=item["owner"],
                    **meta,
                )

            return json_response(await asyncio.to_thread(build))
        except (OSError, ValueError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else "原图读取失败"
            return error_response(message, status_code=404)

    async def gallery_stats(self):
        try:
            self.check_login()

            def build():
                stats = self.library.stats()
                stats["top_sent"] = [self.card(item) for item in stats["top_sent"]]
                return stats

            return json_response(await asyncio.to_thread(build))
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def gallery_similar(self):
        try:
            self.check_login()

            def build():
                # Older images are hashed in bounded batches; the page polls until done.
                # No mutation lock: only hash columns change, so chat collection keeps going.
                remaining = self.library.fill_phashes(60)
                groups = self.library.similar_groups()
                return {
                    "pending": remaining,
                    "total_groups": len(groups),
                    "groups": [[self.card(item) for item in g] for g in groups[:40]],
                }

            return json_response(await asyncio.to_thread(build))
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def gallery_action(self):
        try:
            self.check_login()
            body = await request.json(default={})
            if not isinstance(body, dict):
                raise ValueError("请求格式无效")
            action = body.get("action")
            selection = body.get("select")
            if isinstance(selection, dict):
                # "Select every search result" is resolved here, across all pages.
                items = await asyncio.to_thread(
                    self.select,
                    str(selection.get("q", "")),
                    str(selection.get("view", "all")),
                )
                ids = [item["id"] for item in items]
            else:
                ids = body.get("ids", [])
            if (
                not isinstance(ids, list)
                or len(ids) > MAX_BATCH
                or any(type(i) is not int or i < 1 for i in ids)
            ):
                raise ValueError(f"图片编号格式错误，单次最多 {MAX_BATCH} 张")
            ids = list(dict.fromkeys(ids))
            if action == "describe":
                if len(ids) != 1:
                    raise ValueError("一次请选择一张图片识别")
                item = await asyncio.to_thread(self.library.get, ids[0])
                return json_response(await self.describe_image(item))
            async with self.mutation:
                if action in {"add", "remove", "replace"}:
                    if not ids:
                        raise ValueError("请先选择图片")
                    labels = tags_from(body.get("tags", ""))
                    if action != "replace" and not labels:
                        raise ValueError("请先输入标签")
                    await asyncio.to_thread(
                        self.library.update_tags, ids, action, labels
                    )
                elif action in {"delete", "restore"}:
                    if not ids:
                        raise ValueError("请先选择图片")
                    await asyncio.to_thread(
                        self.library.recycle, ids, action == "restore"
                    )
                elif action == "rename":
                    old, new = body.get("old", ""), body.get("new", "")
                    if not isinstance(old, str) or not old or not isinstance(new, str):
                        raise ValueError("请填写原标签和新标签")
                    count = await asyncio.to_thread(self.library.rename_tag, old, new)
                    return json_response({"message": f"已更新 {count} 张图片"})
                elif action == "merge":
                    keep = body.get("keep")
                    if type(keep) is not int or keep not in ids:
                        raise ValueError("请选择要保留的图片")
                    count = await asyncio.to_thread(self.library.merge, keep, ids)
                    return json_response(
                        {
                            "message": f"已合并 {count} 张相似图片到 #{keep}，其余移入回收站"
                        }
                    )
                elif action == "dismiss":
                    await asyncio.to_thread(self.library.dismiss_similar, ids)
                    return json_response({"message": "已标记为不相似，不再提示"})
                else:
                    raise ValueError("未知操作")
            return json_response(
                {"message": f"已更新 {len(ids)} 张图片", "count": len(ids)}
            )
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)

    async def gallery_upload(self):
        temporary = None
        try:
            self.check_login()
            files = await request.files()
            upload = files.get("file")
            if upload is None:
                raise ValueError("请选择图片、普通图片 ZIP 或本插件备份")
            is_zip = str(upload.filename or "").lower().endswith(".zip")
            limit = 30 * 1048576 if is_zip else self.library.max_bytes
            data = await upload.read(limit + 1)
            if len(data) > limit:
                raise ValueError(f"上传文件不能超过 {limit // 1048576} MB")
            async with self.mutation:
                if is_zip:
                    temporary = self.library.cache / f"import-{uuid.uuid4().hex}.zip"
                    await asyncio.to_thread(temporary.write_bytes, data)
                    report = await asyncio.to_thread(
                        self.library.import_upload_archive,
                        temporary,
                        "web:" + request.username,
                    )
                    return json_response(
                        {
                            "message": f"已处理 {report['imported']} 张图片，新增 {report['added']} 张，重复合并 {report['duplicates']} 张，跳过 {report['skipped']} 个文件，失败 {report['failed']} 张",
                            **report,
                        }
                    )
                item, duplicate = await asyncio.to_thread(
                    self.library.add, data, ["待整理"], "web:" + request.username, "web"
                )
                return json_response(
                    {
                        "message": f"图片 {item['id']} {'已在图库中，已合并' if duplicate else '已导入'}",
                        "id": item["id"],
                        "imported": 1,
                        "added": int(not duplicate),
                        "duplicates": int(duplicate),
                        "skipped": 0,
                        "failed": 0,
                        "errors": [],
                    }
                )
        except Exception as exc:
            return error_response(
                str(exc)
                if isinstance(exc, ValueError)
                else "导入失败，请确认文件完整且是支持的图片、普通图片 ZIP 或本插件备份",
                status_code=400,
            )
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    async def gallery_export(self):
        try:
            self.check_login()
            async with self.mutation:
                path = await asyncio.to_thread(self.library.export)
            return file_response(
                path, filename="memeharbor-backup.zip", content_type="application/zip"
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def gallery_file(self, image_id):
        try:
            self.check_login()
            item = await asyncio.to_thread(self.library.get, image_id, True)
            return file_response(
                self.library.path(item),
                filename=f"image-{item['id']}" + self.library.path(item).suffix,
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=404)
