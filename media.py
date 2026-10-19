"""Bounded loading of AstrBot image components with adapter-specific fallbacks."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import aiohttp

from astrbot.api.message_components import Image, Reply

from .library import inspect_image


class MediaLoader:
    def __init__(self, max_bytes, timeout=20, trust_env=True):
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.trust_env = trust_env
        self.session = None

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def read(self, source):
        if not isinstance(source, str) or not source:
            raise ValueError("图片没有可用地址")
        if source.startswith(("base64://", "data:image/")):
            encoded = (
                source.split(",", 1)[1] if source.startswith("data:") else source[9:]
            )
            if len(encoded) > (self.max_bytes * 4 // 3 + 16):
                raise ValueError("图片超过大小限制")
            data = base64.b64decode(encoded, validate=True)
        elif source.startswith(("http://", "https://", "//")):
            if source.startswith("//"):
                source = "https:" + source
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession(
                    trust_env=self.trust_env,
                    timeout=aiohttp.ClientTimeout(
                        total=self.timeout, connect=min(8, self.timeout)
                    ),
                )
            async with self.session.get(source) as response:
                response.raise_for_status()
                if response.content_length and response.content_length > self.max_bytes:
                    raise ValueError("图片超过大小限制")
                content = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    content.extend(chunk)
                    if len(content) > self.max_bytes:
                        raise ValueError("图片超过大小限制")
                data = bytes(content)
        else:
            parsed = urlparse(source)
            if parsed.scheme == "file":
                path_text = url2pathname(parsed.path)
                if parsed.netloc and parsed.netloc != "localhost":
                    path_text = "//" + parsed.netloc + path_text
                path = Path(path_text)
            else:
                path = Path(source)

            def read_local():
                with path.open("rb") as stream:
                    return stream.read(self.max_bytes + 1)

            data = await asyncio.to_thread(read_local)
        await asyncio.to_thread(inspect_image, data, self.max_bytes)
        return data

    @staticmethod
    def image_segments(event):
        chain = event.get_messages() or []

        def walk(items):
            result = []
            for segment in items:
                if isinstance(segment, Image):
                    result.append(segment)
                elif isinstance(segment, Reply):
                    result.extend(walk(getattr(segment, "chain", None) or []))
            return result

        quoted = []
        for segment in chain:
            if isinstance(segment, Reply):
                quoted.extend(walk(getattr(segment, "chain", None) or []))
        return quoted or walk(chain)

    @staticmethod
    def raw_urls(event):
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)

        def field(value, name, default=None):
            return (
                value.get(name, default)
                if isinstance(value, dict)
                else getattr(value, name, default)
            )

        attachments = []
        if str(field(raw, "message_type", "")) == "103":
            for element in field(raw, "msg_elements", []) or []:
                attachments.extend(field(element, "attachments", []) or [])
        if not attachments:
            attachments = field(raw, "attachments", []) or []
        result = []
        for attachment in attachments:
            content_type = str(
                field(attachment, "content_type", "")
                or field(attachment, "contentType", "")
            )
            url = field(attachment, "url")
            if isinstance(url, str) and (
                content_type.startswith("image/")
                or Path(urlparse(url).path).suffix.lower()
                in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
            ):
                result.append(url)
        return result

    async def load(self, event):
        result, errors = [], []
        segments = self.image_segments(event)
        for segment in segments:
            candidates = list(
                dict.fromkeys(
                    s
                    for s in (
                        getattr(segment, "url", None),
                        getattr(segment, "file", None),
                    )
                    if s
                )
            )
            last_error = None
            for source in candidates:
                try:
                    result.append(await self.read(source))
                    break
                except (TimeoutError, OSError, ValueError, aiohttp.ClientError) as exc:
                    last_error = exc
            else:
                converter = getattr(segment, "convert_to_base64", None)
                if converter and not candidates:
                    try:
                        converted = converter()
                        if asyncio.iscoroutine(converted):
                            converted = await asyncio.wait_for(converted, self.timeout)
                        if isinstance(converted, bytes):
                            await asyncio.to_thread(
                                inspect_image, converted, self.max_bytes
                            )
                            result.append(converted)
                        else:
                            result.append(
                                await self.read(
                                    converted
                                    if converted.startswith("base64://")
                                    else "base64://" + converted
                                )
                            )
                        continue
                    except Exception as exc:
                        last_error = exc
                errors.append(
                    type(last_error).__name__ if last_error else "缺少图片地址"
                )
        if not segments:
            for url in self.raw_urls(event):
                try:
                    result.append(await self.read(url))
                except (TimeoutError, OSError, ValueError, aiohttp.ClientError) as exc:
                    errors.append(type(exc).__name__)
        # Only OneBot exposes get_msg/get_image; never call those on official QQ.
        if not result and event.get_platform_name() == "aiocqhttp":
            reply = next(
                (s for s in event.get_messages() if isinstance(s, Reply)), None
            )
            if reply and getattr(reply, "id", None):
                try:
                    payload = await asyncio.wait_for(
                        event.bot.api.call_action("get_msg", message_id=str(reply.id)),
                        self.timeout,
                    )
                    for node in payload.get("message", []):
                        if node.get("type") != "image":
                            continue
                        source = node.get("data", {})
                        if source.get("url"):
                            result.append(await self.read(source["url"]))
                        elif source.get("file"):
                            detail = await asyncio.wait_for(
                                event.bot.api.call_action(
                                    "get_image", file_id=source["file"]
                                ),
                                self.timeout,
                            )
                            result.append(
                                await self.read(detail.get("file") or detail.get("url"))
                            )
                except Exception as exc:
                    errors.append(type(exc).__name__)
        return result, errors
