#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Discord WebSocket listener.

This listener keeps one Discord Web page open and reads Chrome performance logs
for Gateway MESSAGE_CREATE frames. It avoids opening one tab per channel.
"""

import json
import time
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional

from src.core.models import DiscordMessage
from src.services.listener.browser import BrowserManager
from src.utils.logger import get_logger

logger = get_logger(__name__)


class DiscordWebsocketListener:
    """Listen to Discord Gateway messages from a logged-in browser session."""

    def __init__(
        self,
        channel_urls: List[str],
        on_new_message: Callable[[DiscordMessage], None],
        check_interval: float = 0.2,
        headless_mode: bool = False,
        chrome_load_images: bool = True,
        chrome_disable_notifications: bool = True,
        chrome_mute_audio: bool = True,
    ):
        self.channel_urls = channel_urls if isinstance(channel_urls, list) else [channel_urls]
        self.on_new_message = on_new_message
        self.check_interval = max(0.05, float(check_interval or 0.2))
        self.channel_by_id = self._build_channel_map(self.channel_urls)
        self.seen_message_ids = set()
        self.seen_message_order: List[str] = []
        self.max_seen_messages = 5000
        self.last_frame_seen_at: Optional[float] = None
        self.last_parse_warning_at = 0.0

        self.browser_manager = BrowserManager(
            headless_mode=headless_mode,
            load_images=chrome_load_images,
            disable_notifications=chrome_disable_notifications,
            mute_audio=chrome_mute_audio,
            performance_logging=True,
        )
        self.driver = None

    def init_chrome(self):
        self.driver = self.browser_manager.init_chrome()

    def login_discord(self):
        logger.info("Opening Discord...")
        self.driver.get("https://discord.com/login")
        time.sleep(3)

        if "login" in (self.driver.current_url or ""):
            logger.info("Please log in to Discord in the browser.")
            logger.info("   Docker noVNC: http://localhost:7900, default password: secret")
            while "login" in (self.driver.current_url or ""):
                time.sleep(2)

            logger.info("Discord login succeeded, waiting for session persistence...")
            time.sleep(8)
        else:
            logger.info("Discord is already logged in, skipping login")

        time.sleep(2)

    def navigate_to_channel(self, channel_url: Optional[str] = None):
        target_url = channel_url or (self.channel_urls[0] if self.channel_urls else "https://discord.com/channels/@me")
        logger.info(f"WebSocket mode opens one Discord page only: {target_url}")
        self.driver.get(target_url)
        time.sleep(5)
        self._drain_performance_logs()
        logger.info("WebSocket listener page is ready")

    def monitor_messages(self):
        logger.info("Discord WebSocket listener started")
        logger.info(f"Filtering {len(self.channel_by_id)} Discord channels")

        while True:
            try:
                events = self._read_gateway_events()
                for event in events:
                    message = self._message_from_gateway_event(event)
                    if not message:
                        continue

                    self._remember_message(message.id)
                    logger.info("")
                    logger.info("WebSocket new message:")
                    logger.info(f"   User: {message.username}")
                    logger.info(f"   Channel: {message.channel_name or message.channel_url}")
                    logger.info(f"   Content: {message.content[:50]}...")
                    self.on_new_message(message)

                self._warn_if_no_frames()
            except Exception as e:
                logger.error(f"WebSocket listener error: {e}", exc_info=True)
                time.sleep(3)

            time.sleep(self.check_interval)

    def cleanup(self):
        if self.browser_manager:
            self.browser_manager.cleanup()

    def _read_gateway_events(self) -> Iterable[Dict]:
        events = []
        for entry in self._get_performance_logs():
            message = self._parse_performance_entry(entry)
            if not message:
                continue

            method = message.get("method")
            if method != "Network.webSocketFrameReceived":
                continue

            payload_data = (
                message.get("params", {})
                .get("response", {})
                .get("payloadData", "")
            )
            payload = self._decode_payload(payload_data)
            if not payload:
                continue

            self.last_frame_seen_at = time.time()
            for gateway_event in self._extract_gateway_events(payload):
                if gateway_event.get("t") == "MESSAGE_CREATE":
                    events.append(gateway_event)

        return events

    def _get_performance_logs(self) -> List[Dict]:
        try:
            return self.driver.get_log("performance")
        except Exception as e:
            now = time.time()
            if now - self.last_parse_warning_at > 60:
                logger.warning(f"Failed to read Chrome performance log: {e}")
                self.last_parse_warning_at = now
            return []

    @staticmethod
    def _parse_performance_entry(entry: Dict) -> Optional[Dict]:
        try:
            return json.loads(entry.get("message", "{}")).get("message")
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _decode_payload(payload_data: str) -> Optional[Dict]:
        if not payload_data:
            return None

        payload_data = payload_data.strip()
        if not payload_data.startswith(("{", "[")):
            return None

        try:
            return json.loads(payload_data)
        except ValueError:
            return None

    @staticmethod
    def _extract_gateway_events(payload: Dict) -> Iterable[Dict]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            return [payload]
        return []

    def _message_from_gateway_event(self, event: Dict) -> Optional[DiscordMessage]:
        data = event.get("d") or {}
        if not isinstance(data, dict):
            return None

        channel_id = str(data.get("channel_id") or "")
        channel_url = self.channel_by_id.get(channel_id)
        if not channel_url:
            return None

        message_id = str(data.get("id") or "")
        if not message_id or message_id in self.seen_message_ids:
            return None

        author = data.get("author") or {}
        member = data.get("member") or {}
        username = (
            member.get("nick")
            or author.get("global_name")
            or author.get("username")
            or "Unknown user"
        )
        content = str(data.get("content") or "").strip()
        attachments = self._extract_attachments(data)
        if not content:
            content = f"[Attachment count: {len(attachments)}]" if attachments else "[No text content]"

        return DiscordMessage(
            id=message_id,
            content=content,
            username=username,
            timestamp=self._parse_timestamp(data.get("timestamp")),
            channel_url=channel_url,
            attachments=attachments,
            channel_name=self._get_channel_name(channel_url),
        )

    @staticmethod
    def _extract_attachments(data: Dict) -> List[str]:
        urls = []
        for attachment in data.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            url = attachment.get("url") or attachment.get("proxy_url")
            if url:
                urls.append(url)
        return urls

    @staticmethod
    def _parse_timestamp(timestamp: Optional[str]) -> datetime:
        if not timestamp:
            return datetime.now()

        try:
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now()

    @staticmethod
    def _build_channel_map(channel_urls: List[str]) -> Dict[str, str]:
        channel_by_id = {}
        for url in channel_urls:
            channel_id = DiscordWebsocketListener._extract_channel_id(url)
            if channel_id:
                channel_by_id[channel_id] = url.rstrip("/")
        return channel_by_id

    @staticmethod
    def _extract_channel_id(channel_url: str) -> Optional[str]:
        parts = str(channel_url or "").rstrip("/").split("/")
        if len(parts) < 2:
            return None
        channel_id = parts[-1]
        return channel_id if channel_id.isdigit() else None

    @staticmethod
    def _get_channel_name(channel_url: str) -> str:
        channel_id = DiscordWebsocketListener._extract_channel_id(channel_url)
        return f"Channel {channel_id}" if channel_id else "Unknown channel"

    def _remember_message(self, message_id: str):
        self.seen_message_ids.add(message_id)
        self.seen_message_order.append(message_id)
        if len(self.seen_message_order) <= self.max_seen_messages:
            return

        expired = self.seen_message_order[:-self.max_seen_messages]
        self.seen_message_order = self.seen_message_order[-self.max_seen_messages:]
        for old_message_id in expired:
            self.seen_message_ids.discard(old_message_id)

    def _drain_performance_logs(self):
        try:
            self.driver.get_log("performance")
        except Exception:
            pass

    def _warn_if_no_frames(self):
        if self.last_frame_seen_at:
            return

        now = time.time()
        if now - self.last_parse_warning_at < 60:
            return

        logger.warning(
            "No parseable Discord WebSocket JSON frames have been read from Chrome performance log yet. "
            "If this continues, temporarily switch back to DISCORD_LISTENER_MODE = 'browser_tabs'."
        )
        self.last_parse_warning_at = now
