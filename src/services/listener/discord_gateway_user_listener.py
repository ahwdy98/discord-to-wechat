#!/usr/bin/env python3
"""Low-resource Discord Gateway listener for a user account."""

import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional

from src.core.models import DiscordMessage
from src.utils.logger import get_logger


logger = get_logger(__name__)


class DiscordGatewayUserListener:
    """Receive configured Discord channels through discord.py-self."""

    def __init__(
        self,
        channel_urls: List[str],
        on_new_message: Callable[[DiscordMessage], bool],
        token: str,
        recovery_max_messages: int = 10,
        recovery_lookback_seconds: float = 86400.0,
        max_seen_messages: int = 10000,
    ):
        self.channel_urls = channel_urls if isinstance(channel_urls, list) else [channel_urls]
        self.on_new_message = on_new_message
        self.token = str(token or "").strip()
        self.recovery_max_messages = max(1, int(recovery_max_messages or 10))
        self.recovery_lookback_seconds = max(0.0, float(recovery_lookback_seconds or 0))
        self.max_seen_messages = max(100, int(max_seen_messages or 10000))
        self.channel_by_id = self._build_channel_map(self.channel_urls)
        self.seen_message_keys = OrderedDict()
        self.client = None
        self.loop = None
        self._recovery_lock = None
        self._recovery_runs = 0

    def init_chrome(self):
        """Compatibility hook for the bridge lifecycle; no browser is started."""
        if not self.token:
            raise ValueError("DISCORD_USER_TOKEN is required for gateway_user mode")
        logger.info("Discord Gateway user listener does not require Chrome or Selenium")

    def login_discord(self):
        """Authentication occurs when the Gateway client starts."""
        logger.info("Discord user token is configured; authentication will occur in memory")

    def navigate_to_channel(self):
        """Compatibility hook; Gateway receives all configured channels on one session."""
        logger.info(f"Gateway user listener configured for {len(self.channel_by_id)} channels")

    def monitor_messages(self):
        logger.info("Starting Discord user Gateway listener")
        asyncio.run(self._run())

    async def _run(self):
        import discord

        self.loop = asyncio.get_running_loop()
        self._recovery_lock = asyncio.Lock()
        self.client = discord.Client(max_messages=100)

        @self.client.event
        async def on_ready():
            await self._on_ready()

        @self.client.event
        async def on_message(message):
            await self._on_message(message)

        @self.client.event
        async def on_message_edit(before, after):
            await self._on_message(after, edited=True)

        @self.client.event
        async def on_disconnect():
            logger.warning("Discord user Gateway disconnected; waiting for automatic reconnect")

        @self.client.event
        async def on_resumed():
            logger.info("Discord user Gateway session resumed")

        try:
            await self.client.start(self.token)
        finally:
            self.client = None
            self.loop = None

    async def _on_ready(self):
        cached = sum(self.client.get_channel(channel_id) is not None for channel_id in self.channel_by_id)
        logger.info(
            "Discord user Gateway is ready: "
            f"configured_channels={len(self.channel_by_id)}, cached_channels={cached}, "
            f"guilds={len(self.client.guilds)}"
        )
        await self._recover_recent_messages()

    async def _on_message(self, message, edited: bool = False):
        channel_id = int(getattr(getattr(message, "channel", None), "id", 0) or 0)
        if channel_id not in self.channel_by_id:
            return

        key = self._message_key(message, edited=edited)
        if not key or not self._remember_message(key):
            return

        parsed = self._message_from_discord(message)
        event_name = "edited message" if edited else "new message"
        logger.info(
            f"Gateway user {event_name}: channel={parsed.channel_name or parsed.channel_url}, "
            f"message_id={parsed.id}"
        )
        if not await asyncio.to_thread(self.on_new_message, parsed):
            self.seen_message_keys.pop(key, None)
            logger.error(f"Gateway user message delivery failed: message_id={parsed.id}")

    async def _recover_recent_messages(self):
        if self._recovery_lock is None:
            return

        async with self._recovery_lock:
            self._recovery_runs += 1
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.recovery_lookback_seconds)
            recovered = 0
            unavailable = []

            for channel_id in self.channel_by_id:
                channel = self.client.get_channel(channel_id)
                if channel is None:
                    unavailable.append(str(channel_id))
                    continue

                try:
                    history = [
                        message
                        async for message in channel.history(limit=self.recovery_max_messages)
                    ]
                except Exception as error:
                    unavailable.append(str(channel_id))
                    logger.warning(
                        f"Gateway history recovery failed for channel_id={channel_id}: "
                        f"{type(error).__name__}"
                    )
                    continue

                for message in self._select_recovery_messages(history, cutoff):
                    key = self._message_key(message)
                    if not key or not self._remember_message(key):
                        continue
                    parsed = self._message_from_discord(message)
                    if await asyncio.to_thread(self.on_new_message, parsed):
                        recovered += 1
                    else:
                        self.seen_message_keys.pop(key, None)
                        logger.error(
                            f"Gateway history message delivery failed: message_id={parsed.id}"
                        )

            logger.info(
                f"Gateway history recovery complete: run={self._recovery_runs}, "
                f"recovered={recovered}, unavailable={len(unavailable)}"
            )
            if unavailable:
                logger.warning(f"Gateway history unavailable channel IDs: {', '.join(unavailable)}")

    def cleanup(self):
        client = self.client
        loop = self.loop
        if not client or not loop or loop.is_closed() or client.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(client.close(), loop).result(timeout=10)
        except Exception as error:
            logger.warning(f"Failed to close Discord user Gateway cleanly: {error}")

    def _message_from_discord(self, message) -> DiscordMessage:
        channel_id = int(message.channel.id)
        channel_url = self.channel_by_id[channel_id]
        username = (
            getattr(message.author, "display_name", None)
            or getattr(message.author, "global_name", None)
            or getattr(message.author, "name", None)
            or "Unknown user"
        )
        content = self._extract_content(message)
        attachments = self._extract_attachments(message)
        if not content:
            content = f"[Attachment count: {len(attachments)}]" if attachments else "[No text content]"

        return DiscordMessage(
            id=str(message.id),
            content=content,
            username=str(username),
            timestamp=self._as_aware_datetime(getattr(message, "created_at", None)),
            channel_url=channel_url,
            attachments=attachments,
            channel_name=self._channel_name(message.channel),
        )

    @staticmethod
    def _select_recovery_messages(messages: Iterable, cutoff: datetime) -> List:
        history = list(messages)
        recent = [
            message
            for message in history
            if DiscordGatewayUserListener._as_aware_datetime(
                getattr(message, "created_at", None)
            ) >= cutoff
        ]
        selected = recent if recent else history[:1]
        return list(reversed(selected))

    @staticmethod
    def _extract_content(message) -> str:
        parts = []
        content = str(getattr(message, "content", "") or "").strip()
        if content:
            parts.append(content)

        for embed in getattr(message, "embeds", []) or []:
            data = embed.to_dict() if hasattr(embed, "to_dict") else {}
            for key in ("title", "description"):
                value = str(data.get(key) or "").strip()
                if value:
                    parts.append(value)
            for field in data.get("fields") or []:
                name = str(field.get("name") or "").strip()
                value = str(field.get("value") or "").strip()
                if name and value:
                    parts.append(f"{name}: {value}")
                elif value:
                    parts.append(value)
            footer = data.get("footer") or {}
            footer_text = str(footer.get("text") or "").strip()
            if footer_text:
                parts.append(footer_text)

        return "\n\n".join(dict.fromkeys(parts))

    @staticmethod
    def _extract_attachments(message) -> List[str]:
        urls = []
        for attachment in getattr(message, "attachments", []) or []:
            url = getattr(attachment, "url", None) or getattr(attachment, "proxy_url", None)
            if url:
                urls.append(str(url))

        for sticker in getattr(message, "stickers", []) or []:
            url = getattr(sticker, "url", None)
            if url:
                urls.append(str(url))

        for embed in getattr(message, "embeds", []) or []:
            data = embed.to_dict() if hasattr(embed, "to_dict") else {}
            for key in ("image", "thumbnail", "video"):
                media = data.get(key) or {}
                url = media.get("url") or media.get("proxy_url")
                if url:
                    urls.append(str(url))

        return list(dict.fromkeys(urls))

    @staticmethod
    def _channel_name(channel) -> str:
        channel_name = str(getattr(channel, "name", "") or "")
        guild_name = str(getattr(getattr(channel, "guild", None), "name", "") or "")
        if guild_name and channel_name:
            return f"{guild_name}: {channel_name}"
        return channel_name or str(getattr(channel, "id", ""))

    @staticmethod
    def _message_key(message, edited: bool = False) -> str:
        message_id = str(getattr(message, "id", "") or "")
        if not message_id:
            return ""
        if not edited:
            return message_id
        edited_at = getattr(message, "edited_at", None)
        return f"{message_id}:update:{edited_at or ''}"

    def _remember_message(self, key: str) -> bool:
        if key in self.seen_message_keys:
            return False
        self.seen_message_keys[key] = None
        while len(self.seen_message_keys) > self.max_seen_messages:
            self.seen_message_keys.popitem(last=False)
        return True

    @staticmethod
    def _as_aware_datetime(value) -> datetime:
        if not isinstance(value, datetime):
            return datetime.now(timezone.utc)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _build_channel_map(channel_urls: List[str]) -> Dict[int, str]:
        result = {}
        for url in channel_urls:
            normalized = str(url or "").rstrip("/")
            try:
                channel_id = int(normalized.rsplit("/", 1)[-1])
            except (TypeError, ValueError):
                continue
            result[channel_id] = normalized
        return result
