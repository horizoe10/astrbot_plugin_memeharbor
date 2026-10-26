"""Read image components and references from explicitly supplied chat records."""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque

from astrbot.api.message_components import Forward, Image, Json, Node, Nodes, Reply


def field(value, name, default=None):
    return (
        value.get(name, default)
        if isinstance(value, dict)
        else getattr(value, name, default)
    )


def cq_unescape(value):
    for before, after in (
        ("&#91;", "["),
        ("&#93;", "]"),
        ("&#44;", ","),
        ("&amp;", "&"),
    ):
        value = value.replace(before, after)
    return value


class ImageSources:
    def __init__(self):
        self.images = []
        self.forwards = []
        self.replies = []
        self.is_record = False

    def scan(self, payload):
        # An explicit stack supports deep records without a recursion/count cap.
        pending, seen = [payload], {}
        while pending:
            value = pending.pop()
            if value is None:
                continue
            if isinstance(value, str):
                if value.lstrip().startswith(("[", "{")):
                    try:
                        decoded = json.loads(value)
                    except ValueError:
                        decoded = None
                    if isinstance(decoded, (dict, list)):
                        pending.append(decoded)
                        continue
                # Strings in OneBot message/content fields may be CQ messages.
                for match in re.finditer(
                    r"\[CQ:(image|forward|reply),([^\]]+)\]", value
                ):
                    data = dict(
                        part.split("=", 1)
                        for part in match[2].split(",")
                        if "=" in part
                    )
                    pending.append(
                        {
                            "type": match[1],
                            "data": {k: cq_unescape(v) for k, v in data.items()},
                        }
                    )
                continue
            identity = id(value)
            if identity in seen:
                continue
            # Keep references alive: Python may otherwise reuse IDs of parsed CQ/JSON dicts.
            seen[identity] = value
            if isinstance(value, (list, tuple)):
                pending.extend(reversed(value))
                continue
            if isinstance(value, Image):
                self.images.append(value)
            elif isinstance(value, Reply):
                if value.id is not None and str(value.id).strip():
                    self.replies.append(str(value.id))
                pending.append(value.chain)
            elif isinstance(value, Node):
                self.is_record = True
                pending.append(value.content)
            elif isinstance(value, Nodes):
                self.is_record = True
                pending.append(value.nodes)
            elif isinstance(value, Forward):
                self.is_record = True
                if value.id:
                    self.forwards.append(str(value.id))
            elif isinstance(value, Json):
                self.card(value.data)
            elif isinstance(value, dict):
                kind = str(value.get("type", "")).lower()
                data = value.get("data", {})
                data = data if isinstance(data, dict) else {}
                if kind == "image":
                    fields = {
                        key: data.get(key) if isinstance(data.get(key), str) else ""
                        for key in ("url", "file", "path")
                    }
                    self.images.append(
                        Image(
                            file=fields["file"] or fields["url"],
                            url=fields["url"],
                            path=fields["path"],
                        )
                    )
                elif kind in {"forward", "forward_msg"}:
                    self.is_record = True
                    reference = data.get("id") or data.get("message_id")
                    if reference is not None and str(reference).strip():
                        self.forwards.append(str(reference))
                    pending.append(data.get("content"))
                elif kind == "reply":
                    reference = data.get("id")
                    if reference is not None and str(reference).strip():
                        self.replies.append(str(reference))
                    pending.append(data.get("chain"))
                elif kind in {"node", "nodes"}:
                    self.is_record = True
                    pending.append(
                        data.get("content")
                        or data.get("nodes")
                        or value.get("content")
                        or value.get("nodes")
                    )
                elif kind == "json":
                    self.card(data.get("data"))
                elif not kind:
                    # API envelopes / nodes have these specific fields. Never
                    # interpret arbitrary text or JSON card previews as images.
                    for key in ("messages", "message", "content", "nodes"):
                        if value.get(key) is not None:
                            pending.append(value[key])
                    if isinstance(value.get("data"), (dict, list)):
                        pending.append(value["data"])
        return self

    def card(self, value):
        if isinstance(value, str):
            try:
                value = json.loads(cq_unescape(value))
            except ValueError:
                return
        if not isinstance(value, dict) or value.get("app") != "com.tencent.multimsg":
            return
        self.is_record = True
        detail = field(field(value, "meta", {}), "detail", {})
        reference = field(detail, "resid") or field(detail, "m_resid")
        if isinstance(reference, (str, int)) and str(reference).strip():
            self.forwards.append(str(reference))


def event_sources(event):
    chain = event.get_messages() or []
    quoted = [s for s in chain if isinstance(s, Reply)]
    if quoted:
        sources = ImageSources().scan(quoted)
        if sources.images or sources.forwards or sources.is_record:
            return sources
    return ImageSources().scan(chain)


async def onebot_action(event, action, params_list, timeout):
    bot = getattr(event, "bot", None)
    call = getattr(getattr(bot, "api", None), "call_action", None)
    if not callable(call):
        call = getattr(bot, "call_action", None)
    if not callable(call):
        raise ValueError("适配器未提供消息读取接口")
    raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
    self_id = field(raw, "self_id")
    last_error = None
    for params in params_list:
        try:
            routing = {"self_id": self_id} if self_id is not None else {}
            response = await asyncio.wait_for(
                call(action, **params, **routing), timeout
            )
            if not isinstance(response, dict):
                raise ValueError("消息接口没有返回有效数据")
            if response.get("status") == "failed" or response.get("retcode", 0) not in (
                0,
                None,
            ):
                raise ValueError("消息接口读取失败")
            data = response.get("data")
            payload = data if isinstance(data, (dict, list)) else response
            keys = (
                ("file", "url")
                if action == "get_image"
                else ("messages", "message", "content", "nodes")
            )
            if not isinstance(payload, list) and not any(
                key in payload for key in keys
            ):
                raise ValueError("消息接口返回内容不完整")
            return payload
        except Exception as exc:
            last_error = exc
    raise ValueError(
        f"{action} 读取失败，请检查协议端是否支持该接口、记录是否已过期"
    ) from last_error


async def expand_records(event, sources, timeout):
    errors = []
    if event.get_platform_name() != "aiocqhttp":
        if sources.forwards or (sources.is_record and not sources.images):
            errors.append(
                "当前平台未提供这条聊天记录的图片内容，请展开记录后发送图片或导入图片 ZIP"
            )
        return sources.images, errors
    images = list(sources.images)
    pending = deque(("forward", ref) for ref in sources.forwards)
    if not images and not pending:
        pending.extend(("reply", ref) for ref in sources.replies)
    seen = set()
    while pending:
        kind, reference = pending.popleft()
        key = kind, reference
        if key in seen:
            continue
        seen.add(key)
        action = "get_forward_msg" if kind == "forward" else "get_msg"
        names = ("id", "message_id") if kind == "forward" else ("message_id",)
        values = [reference, int(reference)] if reference.isdigit() else [reference]
        params = [{name: value} for value in values for name in names]
        try:
            payload = await onebot_action(event, action, params, timeout)
            nested = ImageSources().scan(payload)
            images.extend(nested.images)
            pending.extend(("forward", ref) for ref in nested.forwards)
            if not nested.images and not nested.forwards:
                pending.extend(("reply", ref) for ref in nested.replies)
        except Exception as exc:
            errors.append(str(exc))
    if sources.is_record and not images and not errors:
        errors.append(
            "聊天记录中没有可读取的图片，请确认记录内容及协议端的转发读取能力"
        )
    return images, errors
