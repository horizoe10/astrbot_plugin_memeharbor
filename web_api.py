"""Authenticated AstrBot Plugin Pages endpoints for the MemeHarbor web app."""

import asyncio
import base64
import uuid

from astrbot.api.web import error_response, file_response, json_response, request

from .library import INBOX_TAGS, SORTS, in_inbox, tags_from

PLUGIN = "astrbot_plugin_memeharbor"
VIEWS = ("all", "inbox", "trash")
MAX_BATCH = 10000


def data_url(data, mime="image/webp"):
    return f"data:{mime};base64," + base64.b64encode(data).decode()


def origin_label(origin):
    if not origin:
        return "早期图库"
    if origin == "web":
        return "网页导入"
    if origin == "batch_record":
        return "聊天记录导入"
    if ":" not in origin:
        return "早期导入"
    kind, _, chat = origin.rpartition(":")
    if len(chat) > 14:
        # QQ official group ids are long hashes; keep both ends readable.
        chat = f"{chat[:6]}…{chat[-4:]}"
    if kind.endswith(":group"):
        return f"群 {chat}"
    if kind.endswith(":private"):
        return "私聊"
    return origin


def light(item):
    """Card fields without image data; thumbnails are fetched separately."""
    return {
        "id": item["id"],
        "tags": item["tags"],
        "created": item["created"],
        "sent": item["sent"],
        "deleted": item["deleted"],
        "gif": item["filename"].lower().endswith(".gif"),
        "inbox": in_inbox(item),
        "origin": origin_label(item["origin"]),
        "width": item.get("width", 0),
        "height": item.get("height", 0),
        "frames": item.get("frames", 0),
    }


def quoted(tags):
    return "".join(f"「{t}」" for t in tags)


class GalleryAPI:
    def register_gallery(self, context):
        for endpoint, handler, methods in (
            ("home", self.web_home, ["GET"]),
            ("analyze", self.web_analyze, ["POST"]),
            ("list", self.web_list, ["GET"]),
            ("thumbs", self.web_thumbs, ["GET"]),
            ("image/<image_id>", self.gallery_image, ["GET"]),
            ("recommend/<image_id>", self.web_recommend, ["GET"]),
            ("clusters", self.web_clusters, ["GET"]),
            ("tags", self.gallery_tags, ["GET"]),
            ("tag_health", self.web_tag_health, ["GET"]),
            ("insights", self.web_insights, ["GET"]),
            ("scopes", self.web_scopes, ["GET"]),
            ("preview", self.web_preview, ["GET"]),
            ("similar", self.gallery_similar, ["GET"]),
            ("operations", self.web_operations, ["GET"]),
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

    async def respond(self, build, status=400):
        """Run a blocking builder off the event loop with the shared error shape."""
        try:
            self.check_login()
            return json_response(await asyncio.to_thread(build))
        except (ValueError, TypeError, KeyError) as exc:
            return error_response(str(exc), status_code=status)
        except OSError:
            return error_response("读取图库文件失败", status_code=500)

    def card(self, item):
        public = light(item)
        public["caption"], public["ocr"] = item["caption"], item["ocr"]
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

    def counts(self):
        stats = self.library.stats()
        return {
            "all": stats["total"],
            "inbox": stats["inbox"],
            "trash": stats["deleted"],
        }

    # ---------- overview ----------

    async def web_home(self):
        def build():
            with self.library.connect() as connection:
                pending = connection.execute(
                    "SELECT COUNT(*) FROM images WHERE phash_checked=0"
                ).fetchone()[0]
            stats = self.library.stats()
            insights = self.library.insights(30)
            health = self.library.tag_health(30)
            return {
                "stats": {k: v for k, v in stats.items() if k != "top_sent"},
                "analysis_pending": pending,
                "similar_groups": None
                if pending
                else len(self.library.similar_groups()),
                "searches": insights["searches"],
                "top_searches": insights["top"][:8],
                "misses": [m for m in insights["misses"] if not m["now"]][:6],
                "recent": [
                    {"origin": origin_label(o), "count": n}
                    for o, n in self.library.recent_collections(7)[:6]
                ],
                "health": {
                    "duplicates": len(health["duplicates"]),
                    "singletons": len(health["singletons"]),
                    "example": health["duplicates"][0]
                    if health["duplicates"]
                    else None,
                },
                "operations": self.library.operations(5),
                "ai_enabled": bool(self.config.get("ai_enabled", False)),
                "library_scope": self.config.get("library_scope", "shared"),
            }

        return await self.respond(build)

    async def web_analyze(self):
        # Older images are analysed in small batches; the page calls this until done.
        return await self.respond(lambda: {"pending": self.library.fill_phashes(40)})

    # ---------- browsing ----------

    async def web_list(self):
        query = str(request.query.get("q", ""))
        view = str(request.query.get("view", "all"))
        sort = str(request.query.get("sort", "new"))
        offset = max(0, request.query.get("offset", 0, type=int) or 0)
        limit = min(300, max(1, request.query.get("limit", 120, type=int) or 120))

        def build():
            items = self.select(query, view, sort)
            return {
                "items": [light(i) for i in items[offset : offset + limit]],
                "total": len(items),
                "offset": offset,
                "counts": self.counts(),
            }

        return await self.respond(build)

    async def web_thumbs(self):
        raw = str(request.query.get("ids", ""))

        def build():
            ids = [int(i) for i in raw.split(",") if i.strip().isdigit()][:80]
            thumbs = {}
            for image_id in ids:
                try:
                    item = self.library.get(image_id, True)
                    thumbs[str(image_id)] = data_url(self.library.thumbnail_data(item))
                except (OSError, ValueError):
                    thumbs[str(image_id)] = None
            return {"thumbs": thumbs}

        return await self.respond(build)

    async def gallery_image(self, image_id):
        def build():
            item = self.library.get(image_id, True)
            data, mime, meta = self.library.original_data(item)
            return dict(
                self.card(item),
                preview=data_url(data, mime),
                owner=item["owner"],
                **meta,
            )

        return await self.respond(build, status=404)

    async def web_recommend(self, image_id):
        return await self.respond(
            lambda: {"tags": self.library.recommendations(image_id)}, status=404
        )

    async def web_clusters(self):
        def build():
            clusters = self.library.inbox_clusters()
            return {
                "clusters": [[light(i) for i in group] for group in clusters[:30]],
                "total": len(clusters),
            }

        return await self.respond(build)

    # ---------- tags ----------

    async def gallery_tags(self):
        def build():
            return {
                "tags": self.library.tag_overview(30),
                "inbox_tags": list(INBOX_TAGS),
                "aliases": self.library.aliases(),
            }

        return await self.respond(build)

    async def web_tag_health(self):
        return await self.respond(lambda: self.library.tag_health(30))

    # ---------- what groups look for ----------

    async def web_insights(self):
        days = request.query.get("days", 30, type=int) or 30
        return await self.respond(lambda: self.library.insights(min(max(days, 1), 180)))

    def known_scopes(self):
        found = {}
        for item in self.library.search() + self.library.search(deleted=True):
            for origin in [item["origin"], *item["scopes"]]:
                if ":group:" in origin:
                    found[origin] = origin_label(origin)
        with self.library.insights_db() as connection:
            for (scope,) in connection.execute("SELECT DISTINCT scope FROM search_log"):
                if ":group:" in scope:
                    found[scope] = origin_label(scope)
        return found

    async def web_scopes(self):
        def build():
            return {
                "mode": self.config.get("library_scope", "shared"),
                "scopes": [
                    {"scope": scope, "label": label}
                    for scope, label in sorted(
                        self.known_scopes().items(), key=lambda e: e[1]
                    )
                ],
            }

        return await self.respond(build)

    async def web_preview(self):
        """Show what a group would get for a query, using the chat's own rules."""
        query = str(request.query.get("q", "")).strip()
        scope = str(request.query.get("scope", ""))
        mode = str(request.query.get("mode", "search"))

        def build():
            if not query:
                raise ValueError("请输入要预览的内容")
            if len(query) > 200:
                raise ValueError("搜索条件请控制在 200 字以内")
            visible = self.visible_scope(scope) if scope else None
            case = bool(self.config.get("case_sensitive", False))
            if mode == "quick":
                # Quick replies need an exact tag (or alias) and send one image.
                tag = self.library.alias_target(query) or query
                for entry in self.config.get("commands", []) or []:
                    if isinstance(entry, dict) and entry.get("command") == query:
                        tag = entry.get("prefix") or tag
                fold = (lambda x: x) if case else str.casefold
                items = [
                    i
                    for i in self.library.search(tag, False, case, "new", visible)
                    if fold(tag) in [fold(t) for t in i["tags"]]
                ]
                command = query
            else:
                items = self.library.search(query, False, case, "new", visible)
                command = "搜图 " + query
            recent = set(self.recent.get(scope, [])) if scope else set()
            cards = [dict(light(i), recent=i["digest"] in recent) for i in items[:60]]
            return {
                "items": cards,
                "total": len(items),
                "command": command,
                "mode": mode,
                "scope_mode": self.config.get("library_scope", "shared"),
                "recent": sum(c["recent"] for c in cards),
            }

        return await self.respond(build)

    async def gallery_similar(self):
        def build():
            remaining = self.library.fill_phashes(60)
            groups = self.library.similar_groups()
            return {
                "pending": remaining,
                "total_groups": len(groups),
                "groups": [[self.card(item) for item in g] for g in groups[:40]],
            }

        return await self.respond(build)

    async def web_operations(self):
        return await self.respond(lambda: {"operations": self.library.operations(30)})

    # ---------- changes ----------

    def logged(self, action, summary, ids, change):
        """Apply a change and record before/after states so it can be undone."""
        before = self.library.snapshot(ids)
        result = change()
        after = self.library.snapshot(ids)
        operation = self.library.log_operation(
            "web:" + str(request.username), action, summary, before, after
        )
        return result, operation

    async def gallery_action(self):
        try:
            self.check_login()
            body = await request.json(default={})
            if not isinstance(body, dict):
                raise ValueError("请求格式无效")
            action = body.get("action")
            selection = body.get("select")
            if isinstance(selection, dict):
                # "Select every result" is resolved here, across all pages.
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
                result = await asyncio.to_thread(self.apply_action, action, ids, body)
            return json_response(result)
        except (ValueError, TypeError) as exc:
            return error_response(str(exc), status_code=400)

    def apply_action(self, action, ids, body):
        library = self.library
        if action in {"add", "remove", "replace"}:
            if not ids:
                raise ValueError("请先选择图片")
            labels = tags_from(body.get("tags", ""))
            if action != "replace" and not labels:
                raise ValueError("请先输入标签")
            summary = {
                "add": f"为 {len(ids)} 张图片添加{quoted(labels)}",
                "remove": f"从 {len(ids)} 张图片移除{quoted(labels)}",
                "replace": f"修改了 {len(ids)} 张图片的标签"
                if len(ids) > 1
                else f"修改了图片 #{ids[0]} 的标签",
            }[action]
            _, operation = self.logged(
                "tags", summary, ids, lambda: library.update_tags(ids, action, labels)
            )
            return {
                "message": f"已更新 {len(ids)} 张图片",
                "count": len(ids),
                "undo": operation,
            }
        if action == "finish":
            if not ids:
                raise ValueError("请先选择图片")
            labels = [t for t in tags_from(body.get("tags", "")) if t not in INBOX_TAGS]
            if not labels:
                raise ValueError("请先输入标签")
            count, operation = self.logged(
                "finish",
                f"整理了 {len(ids)} 张图片，标签{quoted(labels)}",
                ids,
                lambda: library.finish(ids, labels),
            )
            return {
                "message": f"已为这 {count} 张图打上标签",
                "count": count,
                "undo": operation,
            }
        if action in {"delete", "restore"}:
            if not ids:
                raise ValueError("请先选择图片")
            restore = action == "restore"
            summary = f"{'恢复' if restore else '把'} {len(ids)} 张图片{'' if restore else '移入回收站'}"
            _, operation = self.logged(
                action, summary, ids, lambda: library.recycle(ids, restore)
            )
            return {
                "message": f"已{'恢复' if restore else '移入回收站'} {len(ids)} 张图片",
                "count": len(ids),
                "undo": operation,
            }
        if action == "rename":
            old, new = body.get("old", ""), body.get("new", "")
            if (
                not isinstance(old, str)
                or not old
                or not isinstance(new, str)
                or not new
            ):
                raise ValueError("请填写原标签和新标签")
            affected = library.tagged_ids([old])
            count, operation = self.logged(
                "rename",
                f"把「{old}」改为「{new}」",
                affected,
                lambda: library.rename_tag(old, new),
            )
            return {
                "message": f"已把 {count} 张图片的「{old}」改为「{new}」",
                "count": count,
                "undo": operation,
            }
        if action == "merge_tags":
            sources = [str(t) for t in body.get("sources", []) if str(t).strip()]
            target = str(body.get("target", "")).strip()
            if not sources or not target:
                raise ValueError("请选择要合并的标签和目标标签")
            affected = library.tagged_ids(sources)
            count, operation = self.logged(
                "merge_tags",
                f"把{quoted(sources)}合并到「{target}」",
                affected,
                lambda: library.merge_tags(sources, target),
            )
            return {
                "message": f"已合并，{count} 张图片的标签有变化",
                "count": count,
                "undo": operation,
            }
        if action == "merge":
            keep = body.get("keep")
            if type(keep) is not int or keep not in ids:
                raise ValueError("请选择要保留的图片")
            count, operation = self.logged(
                "merge",
                f"合并 {len(ids)} 张相似图片到 #{keep}",
                ids,
                lambda: library.merge(keep, ids),
            )
            return {
                "message": f"已合并 {count} 张相似图片到 #{keep}，其余移入回收站",
                "count": count,
                "undo": operation,
            }
        if action == "dismiss":
            library.dismiss_similar(ids)
            return {"message": "已标记为不相似，不再提示"}
        if action == "alias_set":
            alias, tag = library.set_alias(body.get("alias", ""), body.get("tag", ""))
            return {"message": f"已设置：发「{alias}」等于找「{tag}」"}
        if action == "alias_remove":
            library.remove_alias(body.get("alias", ""))
            return {"message": "已删除别名"}
        if action == "undo":
            result = library.undo(body.get("operation"))
            message = f"已撤销：{result['summary']}"
            if result["conflicts"]:
                message += f"（{result['conflicts']} 张之后又被改过，已保留现状）"
            return {"message": message, **result}
        raise ValueError("未知操作")

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
