# ruff: noqa: E402
import asyncio
import base64
import importlib.util
import io
import json
import os
import sys
import types
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image as PILImage

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT
import tempfile

_runtime = tempfile.TemporaryDirectory(
    prefix="memeharbor-tests-", ignore_cleanup_errors=True
)
os.environ["ASTRBOT_ROOT"] = _runtime.name
sys.dont_write_bytecode = True
if os.environ.get("ASTRBOT_SOURCE"):
    sys.path.insert(0, os.environ["ASTRBOT_SOURCE"])
pytest.importorskip("astrbot")
package = types.ModuleType("memeharbor_test")
package.__path__ = [str(SOURCE)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(
    "memeharbor_test.main", SOURCE / "main.py"
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
import memeharbor_test.web_api as web_api
from memeharbor_test.library import Library, inspect_image
from memeharbor_test.media import MediaLoader

from astrbot.api.message_components import Image, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import (
    QQOfficialMessageEvent,
)
from astrbot.core.star.star_manager import PluginManager


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


class Event:
    def __init__(self, text="", user="u1", group="g1", admin=True, chain=None):
        self.message_str = text
        self.user, self.group, self.admin = user, group, admin
        self.unified_msg_origin = f"qq_official:GroupMessage:{group}"
        self.is_at_or_wake_command = True
        self.chain = chain or []
        self.message_obj = types.SimpleNamespace(raw_message=None)
        self.sent = []
        self.stopped = False
        self.extra = {}

    def get_platform_name(self):
        return "qq_official"

    def get_group_id(self):
        return self.group

    def get_sender_id(self):
        return self.user

    def get_messages(self):
        return self.chain

    def get_message_str(self):
        return self.message_str

    def get_extra(self, key=None, default=None):
        return self.extra if key is None else self.extra.get(key, default)

    def set_extra(self, key, value):
        self.extra[key] = value

    def is_admin(self):
        return self.admin

    def stop_event(self):
        self.stopped = True

    def chain_result(self, value):
        return MessageChain(chain=value)

    def plain_result(self, value):
        return self.chain_result([Plain(value)])

    async def send(self, value):
        self.sent.append(value)

    def text(self):
        return "\n".join(
            p.text for chain in self.sent for p in chain.chain if isinstance(p, Plain)
        )

    def images(self):
        return [
            p.file for chain in self.sent for p in chain.chain if isinstance(p, Image)
        ]


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(
        module.StarTools, "get_data_dir", lambda name: tmp_path / "data"
    )
    config = {
        k: v["default"]
        for k, v in json.loads(
            (SOURCE / "_conf_schema.json").read_text(encoding="utf-8")
        ).items()
    }
    config["send_cooldown"] = 0
    context = types.SimpleNamespace(
        register_web_api=lambda *args: None, llm_generate=AsyncMock()
    )
    return module.MemeHarborPlugin(context, config)


def test_metadata_and_config():
    assert (
        PluginManager._get_plugin_dir_name_from_metadata(str(SOURCE)) == module.PLUGIN
    )
    assert (
        PluginManager._load_plugin_config_schema(str(SOURCE / "_conf_schema.json"))[
            "ai_enabled"
        ]["default"]
        is False
    )


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
    assert lib.search("happy") and not lib.search("happy", case_sensitive=True)
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


@pytest.mark.asyncio
async def test_qq_direct_and_quoted_collection(plugin):
    segment = Image.fromBase64(base64.b64encode(picture()).decode())
    event = Event("收集 猫 憋笑", chain=[segment])
    await plugin.command(event)
    assert event.stopped and "#1" in event.text()
    quoted = Event("收集 可爱", chain=[Reply(id="123", chain=[segment])])
    await plugin.command(quoted)
    assert len(plugin.library.search()) == 1
    assert plugin.library.get(1)["tags"] == ["猫", "憋笑", "可爱"]
    assert "已有图片" in quoted.text()
    await plugin.terminate()


@pytest.mark.asyncio
async def test_qq_two_step_user_isolation(plugin):
    start = Event("收集 猫", admin=False)
    await plugin.command(start)
    segment = Image.fromBase64(base64.b64encode(picture()).decode())
    stranger = Event(user="u2", admin=False, chain=[segment])
    await plugin.quick_reply(stranger)
    assert plugin.library.search() == []
    follow = Event(admin=False, chain=[segment])
    await plugin.quick_reply(follow)
    assert plugin.library.get(1)["owner"] == "qq_official:u1"
    assert plugin.user_key(follow) not in plugin.pending
    await plugin.terminate()


@pytest.mark.asyncio
async def test_qq_raw_attachment_loading(plugin, monkeypatch):
    event = Event()
    event.message_obj.raw_message = types.SimpleNamespace(
        message_type=103,
        msg_elements=[
            {
                "attachments": [
                    {"url": "//qq.example/cat.png", "content_type": "image/png"}
                ]
            }
        ],
        attachments=[],
    )
    read = AsyncMock(return_value=picture())
    monkeypatch.setattr(plugin.media, "read", read)
    data, errors, _ = await plugin.media.load(event)
    assert data == [picture()] and not errors
    read.assert_awaited_once_with("//qq.example/cat.png")


@pytest.mark.asyncio
async def test_dedup_scope_and_send_failure(plugin):
    for color in ("red", "blue", "green"):
        plugin.library.add(picture(color), ["猫"])
    event = Event()
    for _ in range(3):
        await plugin.send_images(event, plugin.library.search())
    assert len(set(event.images())) == 3
    await plugin.send_images(event, plugin.library.search())
    assert event.images()[3] == event.images()[0]
    other = Event(group="g2")
    await plugin.send_images(other, plugin.library.search())
    assert len(plugin.recent) == 2
    broken = Event(group="g3")
    broken.send = AsyncMock(side_effect=RuntimeError("failed delivery"))
    with pytest.raises(RuntimeError):
        await plugin.send_images(broken, plugin.library.search())
    assert plugin.scope(broken) not in plugin.recent


@pytest.mark.asyncio
async def test_paging_per_user_and_qq_preview_conversion(plugin):
    plugin.config["page_size"] = 3
    for color in ("red", "blue", "green", "yellow", "purple"):
        plugin.library.add(picture(color), ["猫", "憋笑"])
    user1, user2 = Event(), Event(user="u2")
    await plugin.show_page(user1, "猫")
    await plugin.show_page(user2, "憋笑")
    await plugin.dispatch(user1, "下一页", "")
    assert plugin.pages[plugin.user_key(user1)]["page"] == 2
    assert plugin.pages[plugin.user_key(user2)]["page"] == 1
    assert len(user1.images()) == 2
    parsed = await QQOfficialMessageEvent._parse_to_qqofficial(user1.sent[0])
    assert parsed[1] and parsed[2]
    assert "1/2" in parsed[0]
    assert all(isinstance(s, (Image, Plain)) for s in user1.sent[0].chain)
    preview = next(plugin.library.cache.glob("preview-*.jpg"))
    assert preview.stat().st_size > 0


@pytest.mark.asyncio
async def test_permission_and_empty_tag_keeps_image(plugin):
    item, _ = plugin.library.add(picture(), ["猫"], "qq_official:u1")
    other = Event("删除图片 1", user="u2", admin=False)
    await plugin.command(other)
    assert "仅管理员" in other.text() and plugin.library.get(1)
    owner = Event("标签移除 1 猫", admin=False)
    await plugin.command(owner)
    assert plugin.library.get(1)["tags"] == []
    owner = Event("删除图片 1", admin=False)
    await plugin.command(owner)
    assert plugin.library.search() == []
    owner = Event("恢复图片 1", admin=False)
    await plugin.command(owner)
    assert plugin.library.get(1)["id"] == item["id"]


@pytest.mark.asyncio
async def test_cooldown_and_blacklist(plugin):
    plugin.config["send_cooldown"] = 30
    plugin.library.add(picture(), ["猫"])
    event = Event()
    await plugin.send_images(event, plugin.library.search())
    with pytest.raises(ValueError, match="慢一点"):
        await plugin.send_images(event, plugin.library.search())
    plugin.config["user_blacklist"] = ["u1"]
    blocked = Event("搜图 猫")
    await plugin.command(blocked)
    assert blocked.sent == []


@pytest.mark.asyncio
async def test_ai_disabled_and_suggestions_only(plugin):
    item, _ = plugin.library.add(picture(), ["猫"])
    event = Event("识图 1")
    await plugin.command(event)
    plugin.context.llm_generate.assert_not_called()
    assert "默认关闭" in event.text()
    plugin.config.update(ai_enabled=True, ai_provider_id="vision")
    plugin.context.llm_generate.return_value = types.SimpleNamespace(
        completion_text='{"caption":"一只可爱猫","ocr":"哈哈","tags":["可爱"]}'
    )
    event = Event("识图 1")
    await plugin.command(event)
    assert plugin.library.get(item["id"])["caption"] == "一只可爱猫"
    assert plugin.library.get(item["id"])["tags"] == ["猫"]
    assert "标签添加 1 可爱" in event.text()


@pytest.mark.asyncio
async def test_web_api_login_and_batch(plugin, monkeypatch):
    item, _ = plugin.library.add(picture(), ["猫"])
    request = types.SimpleNamespace(
        username=None,
        json=AsyncMock(return_value={"action": "delete", "ids": [item["id"]]}),
    )
    monkeypatch.setattr(web_api, "request", request)
    response = await plugin.gallery_action()
    assert response.status_code == 400 and plugin.library.get(1)
    request.username = "admin"
    request.json.return_value = {"action": "add", "ids": [1], "tags": "可爱 憋笑"}
    assert (await plugin.gallery_action()).status_code == 200
    assert plugin.library.get(1)["tags"] == ["猫", "可爱", "憋笑"]
    request.query = types.SimpleNamespace(
        get=lambda key, default=None, **kwargs: default
    )
    data = json.loads((await plugin.gallery_list()).body)
    assert data["items"][0]["thumbnail"].startswith("data:image/jpeg;base64,")
    assert "owner" not in data["items"][0]


@pytest.mark.asyncio
async def test_media_download_limits_and_reused_session():
    from aiohttp import web

    app = web.Application()

    async def handler(request):
        if request.path == "/large":
            return web.Response(body=b"x" * 10001)
        if request.path == "/bad":
            return web.Response(text="expired URL")
        return web.Response(body=picture(), content_type="image/png")

    app.router.add_get("/{path}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    loader = MediaLoader(10000, trust_env=False)
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    try:
        assert await loader.read(base + "/ok") == picture()
        session = loader.session
        assert await loader.read(base + "/ok") == picture()
        assert loader.session is session
        with pytest.raises(ValueError):
            await loader.read(base + "/large")
        with pytest.raises(ValueError):
            await loader.read(base + "/bad")
    finally:
        await loader.close()
        await runner.cleanup()
    assert session.closed


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


@pytest.mark.asyncio
async def test_concurrent_random_replies_do_not_repeat(plugin):
    for color in ("red", "blue", "green"):
        plugin.library.add(picture(color), ["猫"])
    a, b = Event(user="a"), Event(user="b")
    await asyncio.gather(
        plugin.send_images(a, plugin.library.search()),
        plugin.send_images(b, plugin.library.search()),
    )
    assert a.images()[0] != b.images()[0]


@pytest.mark.asyncio
async def test_quick_reply_requires_exact_tag_and_honors_wake(plugin):
    item, _ = plugin.library.add(picture(), ["猫"])
    plugin.library.annotate(item["id"], "可爱小动物", "哈哈")
    casual = Event("哈哈")
    await plugin.quick_reply(casual)
    assert casual.sent == []
    unwoken = Event("猫")
    unwoken.is_at_or_wake_command = False
    await plugin.quick_reply(unwoken)
    assert unwoken.sent == []
    actual = Event("猫")
    await plugin.quick_reply(actual)
    assert len(actual.images()) == 1 and actual.stopped


@pytest.mark.asyncio
async def test_ai_search_rejects_hallucinated_ids_and_pages(plugin):
    plugin.config.update(ai_enabled=True, ai_provider_id="test")
    item, _ = plugin.library.add(picture(), ["猫"])
    plugin.context.llm_generate.return_value = types.SimpleNamespace(
        completion_text=f'[{item["id"]},99999,{item["id"]},"wrong"]'
    )
    event = Event("AI搜图 一只猫")
    await plugin.command(event)
    assert plugin.pages[plugin.user_key(event)]["ids"] == [item["id"]]
    assert len(event.images()) == 1


@pytest.mark.asyncio
async def test_web_upload_export_and_restore(plugin, monkeypatch):
    upload = types.SimpleNamespace(
        filename="test.gif", read=AsyncMock(return_value=picture(animated=True))
    )
    request = types.SimpleNamespace(
        username="admin", files=AsyncMock(return_value={"file": upload})
    )
    monkeypatch.setattr(web_api, "request", request)
    response = await plugin.gallery_upload()
    assert response.status_code == 200
    item = plugin.library.get(1)
    assert item["filename"].endswith(".gif") and item["tags"] == ["待整理"]
    assert (await plugin.gallery_export()).path.exists()
    request.json = AsyncMock(return_value={"action": "delete", "ids": [1]})
    await plugin.gallery_action()
    request.json.return_value["action"] = "restore"
    await plugin.gallery_action()
    assert plugin.library.get(1)


def test_command_registration_with_real_astrbot_parser(plugin):
    from astrbot.core.star.star_handler import star_handlers_registry

    handler = next(
        h
        for h in star_handlers_registry
        if h.handler_name == "command" and "memeharbor_test" in h.handler_module_path
    )
    command_filter = handler.event_filters[0]
    event = Event("搜图 猫 憋笑")
    event.get_message_str = lambda: event.message_str
    event.set_extra = lambda key, value: None
    assert command_filter.filter(event, {})


def test_group_history_ignores_user_session_isolation_but_separates_bots(plugin):
    a, b = Event(user="a"), Event(user="b")
    a.unified_msg_origin += ":a"
    b.unified_msg_origin += ":b"
    assert plugin.scope(a) == plugin.scope(b)
    assert plugin.user_key(a) != plugin.user_key(b)
    a.get_platform_id = lambda: "bot-one"
    b.get_platform_id = lambda: "bot-two"
    assert plugin.scope(a) != plugin.scope(b)
    assert plugin.owner(a).startswith("bot-one:")


@pytest.mark.asyncio
async def test_memeharbor_entrypoint_and_identity(plugin):
    assert module.PLUGIN == "astrbot_plugin_memeharbor"
    assert module.VERSION == "1.0.0"
    event = Event(
        "/memeharbor 收集 猫",
        chain=[Image.fromBase64(base64.b64encode(picture()).decode())],
    )
    event.is_at_or_wake_command = False
    await plugin.quick_reply(event)
    assert plugin.library.get(1)["tags"] == ["猫"]
    help_event = Event("/memeharbor")
    await plugin.quick_reply(help_event)
    assert "MemeHarbor 1.0" in help_event.text()


def test_legacy_settings_import_preserves_new_custom_values(tmp_path, monkeypatch):
    base = tmp_path / "plugin_data"
    legacy = Library(base / "astrbot_plugin_image_replay")
    legacy.add(picture(), ["old"])
    configuration = tmp_path / "config"
    configuration.mkdir()
    (configuration / "astrbot_plugin_image_replay_config.json").write_text(
        json.dumps({"require_wake": False, "max_send_count": 2}), encoding="utf-8"
    )
    monkeypatch.setattr(module.StarTools, "get_data_dir", lambda name: base / name)
    config = {
        k: v["default"]
        for k, v in json.loads(
            (SOURCE / "_conf_schema.json").read_text(encoding="utf-8")
        ).items()
    }
    config["max_send_count"] = 1
    context = types.SimpleNamespace(register_web_api=lambda *args: None)
    instance = module.MemeHarborPlugin(context, config)
    assert instance.config["require_wake"] is False
    assert instance.config["max_send_count"] == 1
    assert instance.library.root.name == "astrbot_plugin_memeharbor"
    assert instance.library.get(1)["tags"] == ["old"]
    assert legacy.get(1)["tags"] == ["old"]


def test_web_routes_use_new_identity(plugin):
    routes = []
    context = types.SimpleNamespace(
        register_web_api=lambda route, *args: routes.append(route)
    )
    plugin.register_gallery(context)
    assert len(routes) == 5
    assert all(route.startswith("/astrbot_plugin_memeharbor/") for route in routes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,woken,require_wake",
    [
        ("收集 猫", False, False),
        ("/收集 猫", False, False),
        ("/收集 猫", False, True),
        ("/收集 猫", True, True),
        ("／收集 猫", False, True),
        ("收集 猫", True, True),
        ("/添加图片 猫", False, False),
        ("/斗图 收集 猫", False, True),
    ],
)
async def test_collect_routes_with_empty_framework_wake_prefix(
    plugin, text, woken, require_wake
):
    from astrbot.core.star.star_handler import star_handlers_registry

    handler = star_handlers_registry.get_handler_by_full_name(
        "memeharbor_test.main_command"
    )
    plugin.config["require_wake"] = require_wake
    image = Image.fromBase64(base64.b64encode(picture()).decode())
    event = Event(text, chain=[image])
    event.is_at_or_wake_command = woken
    matched = all(f.filter(event, {"wake_prefix": []}) for f in handler.event_filters)
    if matched:
        await plugin.command(event)
    if not event.stopped:
        await plugin.quick_reply(event)
    assert len(plugin.library.search()) == 1
    assert plugin.library.get(1)["tags"] == ["猫"]
    assert "收集成功" in event.text()


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_same_event_not_processed_twice(plugin, reverse):
    plugin.config["require_wake"] = False
    event = Event(
        "收集 猫", chain=[Image.fromBase64(base64.b64encode(picture()).decode())]
    )
    handlers = [plugin.command, plugin.quick_reply]
    if reverse:
        handlers.reverse()
    for handler in handlers:
        await handler(event)
    assert len(event.sent) == 1
    assert "已有图片" not in event.text()


@pytest.mark.asyncio
async def test_unwoken_bare_command_still_respects_require_wake(plugin):
    plugin.config["require_wake"] = True
    event = Event("收集 猫")
    event.is_at_or_wake_command = False
    await plugin.quick_reply(event)
    assert event.sent == [] and not plugin.pending


@pytest.mark.asyncio
async def test_compat_route_respects_disabled_handler_and_permissions(
    plugin, monkeypatch
):
    from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
    from astrbot.core.star.star_handler import star_handlers_registry

    handler = star_handlers_registry.get_handler_by_full_name(
        "memeharbor_test.main_command"
    )
    event = Event("/收集 猫", admin=False)
    event.is_at_or_wake_command = False
    monkeypatch.setattr(handler, "enabled", False)
    await plugin.quick_reply(event)
    assert not plugin.pending
    monkeypatch.setattr(handler, "enabled", True)
    monkeypatch.setattr(
        handler,
        "event_filters",
        handler.event_filters + [PermissionTypeFilter(PermissionType.ADMIN)],
    )
    await plugin.quick_reply(event)
    assert not plugin.pending and event.sent == []
    assert event.message_str == "/收集 猫" and not event.is_at_or_wake_command


@pytest.mark.asyncio
async def test_compat_route_respects_removed_alias(plugin, monkeypatch):
    from astrbot.core.star.star_handler import star_handlers_registry

    handler = star_handlers_registry.get_handler_by_full_name(
        "memeharbor_test.main_command"
    )
    command_filter = handler.event_filters[0]
    monkeypatch.setattr(command_filter, "alias", command_filter.alias - {"收集"})
    monkeypatch.setattr(command_filter, "_cmpl_cmd_names", None)
    event = Event("/收集 猫")
    await plugin.quick_reply(event)
    assert event.sent == []
