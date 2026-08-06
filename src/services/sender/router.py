#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按 Discord 频道路由到不同消息发送器。
"""

from typing import Callable, Dict, List

from .base import MessageSender
from src.core.models import DiscordMessage
from src.utils.logger import get_logger

logger = get_logger(__name__)


class ChannelRoutingSender(MessageSender):
    """根据 Discord 频道 URL 选择不同的 MessageSender"""

    def __init__(
        self,
        default_sender: MessageSender,
        routes: List[Dict],
        sender_factory: Callable[[str, Dict], MessageSender],
    ):
        super().__init__()
        self.default_sender = default_sender
        self.routes = routes or []
        self.sender_factory = sender_factory
        self.route_sender_map: Dict[str, MessageSender] = {}
        self.route_sender_keys: Dict[str, str] = {}
        self.sender_cache: Dict[str, MessageSender] = {"default": default_sender}

        self._build_routes()

    def login(self) -> bool:
        success = True
        for key, sender in self.sender_cache.items():
            logger.info(f"初始化路由发送器: {key}")
            if not sender.login():
                logger.error(f"路由发送器初始化失败: {key}")
                success = False

        self.is_ready = success
        return success

    def send_message(self, message: DiscordMessage) -> bool:
        sender = self._get_sender_for_channel(message.channel_url)
        return sender.send_message(message)

    def keep_alive(self):
        for sender in self.sender_cache.values():
            sender.keep_alive()

    def cleanup(self):
        cleaned = set()
        for sender in self.sender_cache.values():
            sender_id = id(sender)
            if sender_id in cleaned:
                continue
            sender.cleanup()
            cleaned.add(sender_id)

    def _build_routes(self):
        for index, route in enumerate(self.routes, 1):
            channel_urls = self._get_route_channels(route)
            sender_type = route.get("sender_type") or route.get("type")

            if not channel_urls or not sender_type:
                logger.warning(f"忽略无效 SENDER_ROUTES 配置 #{index}: {route}")
                continue

            sender_key = self._sender_key(sender_type, route)
            if sender_key not in self.sender_cache:
                self.sender_cache[sender_key] = self.sender_factory(sender_type, route)

            for channel_url in channel_urls:
                normalized_url = self._normalize_channel(channel_url)
                self.route_sender_map[normalized_url] = self.sender_cache[sender_key]
                self.route_sender_keys[normalized_url] = sender_key

        if self.route_sender_map:
            logger.info(f"已配置 {len(self.route_sender_map)} 个频道发送路由")

    def _get_sender_for_channel(self, channel_url: str) -> MessageSender:
        normalized_url = self._normalize_channel(channel_url)
        sender = self.route_sender_map.get(normalized_url)
        if sender:
            logger.info(f"频道命中发送路由: {self.route_sender_keys.get(normalized_url)}")
            return sender
        return self.default_sender

    @staticmethod
    def _get_route_channels(route: Dict) -> List[str]:
        if route.get("channels"):
            channels = route.get("channels") or []
            if isinstance(channels, str):
                return [channels]
            return channels
        if route.get("channel"):
            return [route.get("channel")]
        return []

    @staticmethod
    def _normalize_channel(channel_url: str) -> str:
        return (channel_url or "").rstrip("/")

    @staticmethod
    def _sender_key(sender_type: str, route: Dict) -> str:
        if sender_type == "wechat":
            return f"wechat:{route.get('wechat_receiver_name', '')}"
        if sender_type == "enterprise_wechat":
            return f"enterprise_wechat:{route.get('enterprise_wechat_webhook') or route.get('webhook') or route.get('hook') or ''}"
        if sender_type == "feishu":
            return f"feishu:{route.get('feishu_webhook') or route.get('webhook') or route.get('hook') or ''}:{route.get('feishu_secret') or route.get('secret') or ''}"
        if sender_type == "webhook_server":
            return f"webhook_server:{route.get('webhook_server_url') or route.get('url') or ''}:{route.get('webhook_server_token') or route.get('token') or ''}"
        return f"{sender_type}:{id(route)}"
