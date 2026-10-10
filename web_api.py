"""Authenticated AstrBot Plugin Pages endpoints for the gallery."""

import asyncio
import base64
import io
import math
import uuid

from astrbot.api.web import error_response, file_response, json_response, request

from .library import tags_from, thumbnail

PLUGIN = "astrbot_plugin_memeharbor"


class GalleryAPI:
    def register_gallery(self, context):
        for endpoint, handler, methods in (
            ("gallery", self.gallery_list, ["GET"]),
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

    async def gallery_list(self):
        try:
            self.check_login()
            query = str(request.query.get("q", ""))[:200]
            deleted = request.query.get("deleted", "0") == "1"
            page = max(1, request.query.get("page", 1, type=int))
            items = await self.search(query, deleted)
            pages = max(1, math.ceil(len(items) / 12))
            page = min(page, pages)
            selection = items[(page - 1) * 12 : page * 12]

            def view():
                output = []
                for item in selection:
                    public = {
                        k: item[k]
                        for k in (
                            "id",
                            "tags",
                            "created",
                            "caption",
                            "ocr",
                            "sent",
                            "deleted",
                        )
                    }
                    try:
                        data = io.BytesIO()
                        thumbnail(self.library.path(item)).save(
                            data, format="JPEG", quality=74
                        )
                        public["thumbnail"] = (
                            "data:image/jpeg;base64,"
                            + base64.b64encode(data.getvalue()).decode()
                        )
                    except (OSError, ValueError):
                        public["thumbnail"] = None
                    output.append(public)
                return output

            return json_response(
                {
                    "items": await asyncio.to_thread(view),
                    "total": len(items),
                    "page": page,
                    "pages": pages,
                    "ai_enabled": bool(self.config.get("ai_enabled", False)),
                    "migration_warnings": self.library.migration_warnings,
                }
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)

    async def gallery_action(self):
        try:
            self.check_login()
            body = await request.json(default={})
            if not isinstance(body, dict):
                raise ValueError("请求格式无效")
            action = body.get("action")
            ids = body.get("ids", [])
            if (
                not isinstance(ids, list)
                or len(ids) > 100
                or any(type(i) is not int or i < 1 for i in ids)
            ):
                raise ValueError("图片编号格式错误，单次最多100张")
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
                else:
                    raise ValueError("未知操作")
            return json_response({"message": f"已更新 {len(ids)} 张图片"})
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)

    async def gallery_upload(self):
        temporary = None
        try:
            self.check_login()
            files = await request.files()
            upload = files.get("file")
            if upload is None:
                raise ValueError("请选择图片或本插件导出的 ZIP 备份")
            is_zip = str(upload.filename or "").lower().endswith(".zip")
            limit = 30 * 1048576 if is_zip else self.library.max_bytes
            data = await upload.read(limit + 1)
            if len(data) > limit:
                raise ValueError(f"上传文件不能超过 {limit // 1048576} MB")
            async with self.mutation:
                if is_zip:
                    temporary = self.library.cache / f"import-{uuid.uuid4().hex}.zip"
                    await asyncio.to_thread(temporary.write_bytes, data)
                    mapping = await asyncio.to_thread(
                        self.library.import_archive, temporary
                    )
                    return json_response(
                        {
                            "message": f"已合并导入 {len(mapping)} 张图片（编号以当前图库为准）",
                            "mapping": mapping,
                        }
                    )
                item, duplicate = await asyncio.to_thread(
                    self.library.add, data, ["待整理"], "web:" + request.username, "web"
                )
                return json_response(
                    {
                        "message": f"#{item['id']} {'已合并标签' if duplicate else '已收集'}",
                        "id": item["id"],
                    }
                )
        except Exception as exc:
            return error_response(
                str(exc)
                if isinstance(exc, ValueError)
                else "导入失败，请确认文件完整且是支持的图片或本插件备份",
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
