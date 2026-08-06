#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地 Webhook Server 发送器。

将 Discord 消息转发到独立的 webhook_server 服务，由该服务写入 SQLite
并提供 Web 页面/API 查询。
"""

from datetime import datetime
from typing import Dict

import requests

from .base import MessageSender
from src.core.models import DiscordMessage
from src.utils.logger import get_logger

logger = get_logger(__name__)


class WebhookServerSender(MessageSender):
    """转发消息到自建 Webhook Server"""

    def __init__(self, endpoint_url: str, token: str = ""):
        super().__init__()
        self.endpoint_url = endpoint_url.rstrip("/")
        self.token = token

    def login(self) -> bool:
        if not self.endpoint_url:
            logger.error("请先在 config.py 中配置 WEBHOOK_SERVER_URL")
            return False

        logger.info("\n" + "=" * 50)
        logger.info("正在初始化 Webhook Server 发送器...")
        logger.info("=" * 50)
        logger.info(f"Webhook Server: {self.endpoint_url}")
        self.is_ready = True
        return True

    def send_message(self, message: DiscordMessage) -> bool:
        if not self.is_ready:
            logger.warning("Webhook Server 发送器未就绪，跳过发送")
            return False

        try:
            response = requests.post(
                self.endpoint_url,
                json=self._message_to_payload(message),
                headers=self._headers(),
                timeout=30,
            )
            response.raise_for_status()
            result = response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"发送到 Webhook Server 失败: {e}")
            return False
        except ValueError:
            logger.error(f"Webhook Server 返回了非 JSON 响应: {response.text[:200]}")
            return False

        if result.get("ok"):
            logger.info(f"消息已写入 Webhook Server: {message.content[:30]}...")
            return True

        logger.error(f"Webhook Server 返回失败: {result}")
        return False

    def keep_alive(self):
        """Webhook Server 不需要保持长连接。"""
        pass

    def cleanup(self):
        logger.info("   Webhook Server 发送器已清理")

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _message_to_payload(message: DiscordMessage) -> Dict:
        timestamp = message.timestamp
        if isinstance(timestamp, datetime):
            timestamp_value = timestamp.isoformat()
        else:
            timestamp_value = str(timestamp or "")

        return {
            "id": message.id,
            "username": message.username,
            "content": message.content,
            "timestamp": timestamp_value,
            "channel_url": message.channel_url,
            "channel_name": message.channel_name,
            "attachments": message.attachments,
        }
