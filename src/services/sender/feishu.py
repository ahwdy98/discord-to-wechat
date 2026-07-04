#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞书自定义机器人消息发送器。

支持单个 Webhook，也支持按 Discord 频道 URL 映射到不同飞书群机器人。
"""

import base64
import hashlib
import hmac
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests
from dateutil import parser
from zoneinfo import ZoneInfo

from .base import MessageSender
from src.core.models import DiscordMessage
from src.utils.logger import get_logger

logger = get_logger(__name__)


class FeishuSender(MessageSender):
    """飞书自定义机器人发送器"""

    def __init__(
        self,
        webhook_url: str = "",
        secret: str = "",
        webhook_configs: Optional[List[Dict[str, str]]] = None,
    ):
        """
        初始化飞书发送器。

        :param webhook_url: 默认 Webhook 地址，未命中频道映射时使用
        :param secret: 默认签名密钥，飞书机器人启用签名校验时填写
        :param webhook_configs: Webhook 映射列表，格式为
            [{'hook': '...', 'channel': '...', 'secret': '...'}]
        """
        super().__init__()
        self.webhook_url = webhook_url
        self.secret = secret
        self.webhook_configs = webhook_configs or []
        self.webhook_map: Dict[str, Dict[str, str]] = {}

        for config in self.webhook_configs:
            hook = config.get("hook", "")
            channel = config.get("channel", "")
            config_secret = config.get("secret", self.secret)
            if hook and channel:
                self.webhook_map[channel.rstrip("/")] = {
                    "hook": hook,
                    "secret": config_secret,
                }

    def login(self) -> bool:
        """
        验证飞书 Webhook 是否可用。
        """
        logger.info("\n" + "=" * 50)
        logger.info("正在初始化飞书自定义机器人...")
        logger.info("=" * 50)

        targets_to_test = []
        if self._is_configured_hook(self.webhook_url):
            targets_to_test.append({"hook": self.webhook_url, "secret": self.secret})

        for config in self.webhook_configs:
            hook = config.get("hook", "")
            if self._is_configured_hook(hook):
                targets_to_test.append(
                    {
                        "hook": hook,
                        "secret": config.get("secret", self.secret),
                    }
                )

        unique_targets = []
        seen = set()
        for target in targets_to_test:
            marker = (target["hook"], target.get("secret", ""))
            if marker not in seen:
                unique_targets.append(target)
                seen.add(marker)

        if not unique_targets:
            logger.error("请先在 config.py 中配置 FEISHU_WEBHOOK 或 FEISHU_WEBHOOK_LIST")
            logger.error("提示：在飞书群中添加自定义机器人后，复制 Webhook 地址")
            return False

        success_count = 0
        total_count = len(unique_targets)
        logger.info(f"正在验证 {total_count} 个飞书 Webhook 地址...")

        for i, target in enumerate(unique_targets, 1):
            content = f"飞书机器人初始化成功 ({i}/{total_count})\nDiscord 消息桥接器已启动"
            if self._post_text(target["hook"], target.get("secret", ""), content):
                logger.info(f"飞书 Webhook {i} 连接成功")
                success_count += 1
            else:
                logger.error(f"飞书 Webhook {i} 连接失败")

        if success_count > 0:
            logger.info(f"成功连接 {success_count}/{total_count} 个飞书机器人 Webhook")
            self.is_ready = True
            return True

        logger.error("所有飞书 Webhook 连接均失败")
        return False

    def get_webhook_for_channel(self, channel_url: str) -> Optional[Dict[str, str]]:
        """根据 Discord 频道 URL 获取对应的飞书 Webhook 配置。"""
        if channel_url:
            normalized_url = channel_url.rstrip("/")
            if normalized_url in self.webhook_map:
                return self.webhook_map[normalized_url]

        if self._is_configured_hook(self.webhook_url):
            return {"hook": self.webhook_url, "secret": self.secret}

        return None

    def send_message(self, message: DiscordMessage) -> bool:
        """
        发送消息到飞书群。
        """
        if not self.is_ready:
            logger.warning("飞书机器人未就绪，跳过发送")
            return False

        target = self.get_webhook_for_channel(message.channel_url)
        if not target:
            logger.warning(f"未找到频道 [{message.channel_name}] 对应的飞书 Webhook 配置，且无默认 Webhook")
            return False

        content = self._format_text_message(message)
        if self._post_text(target["hook"], target.get("secret", ""), content):
            logger.info(f"消息已发送到飞书: {message.content[:30]}...")
            return True

        logger.error("发送飞书消息失败")
        return False

    def keep_alive(self):
        """飞书自定义机器人不需要保持长连接。"""
        pass

    def cleanup(self):
        """清理资源。"""
        logger.info("   飞书发送器已清理")

    def _post_text(self, webhook_url: str, secret: str, content: str) -> bool:
        payload = {
            "msg_type": "text",
            "content": {
                "text": content,
            },
        }

        if secret:
            payload.update(self._build_signature(secret))

        try:
            response = requests.post(webhook_url, json=payload, timeout=30)
            response.raise_for_status()
            result = response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"飞书 Webhook 网络请求失败: {e}")
            return False
        except ValueError:
            logger.error(f"飞书 Webhook 返回了非 JSON 响应: {response.text[:200]}")
            return False

        if isinstance(result, dict) and self._is_success_response(result):
            return True

        logger.error(f"飞书 Webhook 返回失败: {result}")
        return False

    def _format_text_message(self, message: DiscordMessage) -> str:
        bj_time_str = self._format_beijing_time(message.timestamp)

        content = f"来自 {message.username} 的消息\n"
        if message.channel_name:
            content += f"频道: {message.channel_name}\n"
        content += f"时间: {bj_time_str}\n"
        content += "----------------\n"
        content += f"{message.content}\n"

        if message.attachments:
            content += f"\n附件({len(message.attachments)}):\n"
            for i, attachment in enumerate(message.attachments[:3], 1):
                content += f"{i}. {attachment}\n"

        return content

    @staticmethod
    def _format_beijing_time(timestamp) -> str:
        try:
            if isinstance(timestamp, str):
                bj_time = parser.isoparse(timestamp).astimezone(ZoneInfo("Asia/Shanghai"))
            elif isinstance(timestamp, datetime):
                bj_time = timestamp.astimezone(ZoneInfo("Asia/Shanghai"))
            else:
                bj_time = datetime.now(ZoneInfo("Asia/Shanghai"))
        except Exception:
            bj_time = datetime.now(ZoneInfo("Asia/Shanghai"))

        return bj_time.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _build_signature(secret: str) -> Dict[str, str]:
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{secret}"
        hmac_code = hmac.new(
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = base64.b64encode(hmac_code).decode("utf-8")
        return {
            "timestamp": timestamp,
            "sign": sign,
        }

    @staticmethod
    def _is_success_response(result: Dict) -> bool:
        if result.get("code") == 0:
            return True
        if result.get("StatusCode") == 0:
            return True
        if result.get("errcode") == 0:
            return True
        return False

    @staticmethod
    def _is_configured_hook(webhook_url: str) -> bool:
        return bool(webhook_url) and "YOUR_WEBHOOK" not in webhook_url
