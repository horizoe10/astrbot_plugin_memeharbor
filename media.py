"""Bounded loading of AstrBot image components with adapter-specific fallbacks."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import aiohttp

from .library import inspect_image
from .records import event_sources, expand_records, onebot_action


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
        return event_sources(event).images

    @staticmethod
    def has_media(event):
        sources = event_sources(event)
        return bool(
            sources.images
            or sources.forwards
            or sources.replies
            or sources.is_record
            or MediaLoader.raw_urls(event)
        )

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

    async def read_segment(self, event, segment):
        candidates = list(
            dict.fromkeys(
                source
                for source in (
                    getattr(segment, "url", None),
                    getattr(segment, "file", None),
                    getattr(segment, "path", None),
                )
                if isinstance(source, str) and source
            )
        )
        last_error = None
        for source in candidates:
            try:
                return await self.read(source)
            except (TimeoutError, OSError, ValueError, aiohttp.ClientError) as exc:
                last_error = exc
        file_id = getattr(segment, "file", None)
        if (
            event.get_platform_name() == "aiocqhttp"
            and file_id
            and not file_id.startswith(
                ("http:", "https:", "//", "base64:", "data:", "file:")
            )
        ):
            try:
                detail = await onebot_action(
                    event, "get_image", [{"file_id": file_id}], self.timeout
                )
                for source in dict.fromkeys((detail.get("url"), detail.get("file"))):
                    if source:
                        try:
                            return await self.read(source)
                        except (
                            TimeoutError,
                            OSError,
                            ValueError,
                            aiohttp.ClientError,
                        ) as exc:
                            last_error = exc
            except Exception as exc:
                last_error = exc
        converter = getattr(segment, "convert_to_base64", None)
        if converter and not candidates:
            try:
                converted = converter()
                if asyncio.iscoroutine(converted):
                    converted = await asyncio.wait_for(converted, self.timeout)
                if isinstance(converted, bytes):
                    await asyncio.to_thread(inspect_image, converted, self.max_bytes)
                    return converted
                return await self.read(
                    converted
                    if converted.startswith("base64://")
                    else "base64://" + converted
                )
            except Exception as exc:
                last_error = exc
        raise ValueError(
            "图片读取失败："
            + (type(last_error).__name__ if last_error else "缺少图片地址")
        ) from last_error

    async def load(self, event):
        sources = event_sources(event)
        segments, errors = await expand_records(event, sources, self.timeout)
        result = []
        for segment in segments:
            try:
                result.append(await self.read_segment(event, segment))
            except Exception as exc:
                errors.append(str(exc))
        if not segments and not sources.is_record:
            for url in self.raw_urls(event):
                try:
                    result.append(await self.read(url))
                except (TimeoutError, OSError, ValueError, aiohttp.ClientError) as exc:
                    errors.append(type(exc).__name__)
        return result, errors
