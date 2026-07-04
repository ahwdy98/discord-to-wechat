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
from urllib.parse import unquote, urlparse

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
        初始化飞书 Webhook 配置。

        飞书自定义机器人没有不发送消息的健康检查接口；启动阶段只做本地配置校验，
        避免容器重启时反复向群里发送“初始化成功”消息。
        """
        logger.info("\n" + "=" * 50)
        logger.info("正在初始化飞书自定义机器人...")
        logger.info("=" * 50)

        unique_targets = self._get_unique_targets()

        if not unique_targets:
            logger.error("请先在 config.py 中配置 FEISHU_WEBHOOK 或 FEISHU_WEBHOOK_LIST")
            logger.error("提示：在飞书群中添加自定义机器人后，复制 Webhook 地址")
            return False

        logger.info(f"已配置 {len(unique_targets)} 个飞书 Webhook")
        logger.info("飞书 Webhook 将在收到 Discord 新消息时发送")
        self.is_ready = True
        return True

    def _get_unique_targets(self) -> List[Dict[str, str]]:
        """返回去重后的飞书 Webhook 配置。保留给后续主动健康检查使用。"""
        targets = []
        if self._is_configured_hook(self.webhook_url):
            targets.append({"hook": self.webhook_url, "secret": self.secret})

        for config in self.webhook_configs:
            hook = config.get("hook", "")
            if self._is_configured_hook(hook):
                targets.append(
                    {
                        "hook": hook,
                        "secret": config.get("secret", self.secret),
                    }
                )

        unique_targets: List[Dict[str, str]] = []
        seen = set()
        for target in targets:
            marker = (target["hook"], target.get("secret", ""))
            if marker not in seen:
                unique_targets.append(target)
                seen.add(marker)

        return unique_targets

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

        payload = self._build_post_payload(message)
        if self._post_payload(target["hook"], target.get("secret", ""), payload):
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

    def _post_payload(self, webhook_url: str, secret: str, payload: Dict) -> bool:
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

    def _build_post_payload(self, message: DiscordMessage) -> Dict:
        bj_time_str = self._format_beijing_time(message.timestamp)

        rows = [
            [{"tag": "text", "text": f"来自 {message.username} 的消息"}],
        ]
        if message.channel_name:
            rows.append([{"tag": "text", "text": f"频道: {message.channel_name}"}])
        rows.append([{"tag": "text", "text": f"时间: {bj_time_str}"}])
        rows.append([{"tag": "text", "text": "----------------"}])

        for line in self._split_text_lines(message.content, len(message.attachments)):
            rows.append([{"tag": "text", "text": line}])

        if message.attachments:
            rows.append([{"tag": "text", "text": f"附件({len(message.attachments)}):"}])
            for i, attachment in enumerate(message.attachments[:3], 1):
                rows.append(
                    [
                        {"tag": "text", "text": f"{i}. "},
                        {
                            "tag": "a",
                            "text": self._format_attachment_label(attachment, i),
                            "href": attachment,
                        },
                    ]
                )

        return {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": "Discord 新消息",
                        "content": rows,
                    }
                }
            },
        }

    @staticmethod
    def _split_text_lines(content: str, attachment_count: int = 0) -> List[str]:
        placeholder = f"[附件 {attachment_count} 个]"
        if attachment_count and (content or "").strip() == placeholder:
            return []

        lines = [line.strip() for line in (content or "").splitlines()]
        lines = [line for line in lines if line]
        return lines or ([] if attachment_count else ["[空消息]"])

    @staticmethod
    def _format_attachment_label(url: str, index: int) -> str:
        parsed = urlparse(url)
        filename = unquote(parsed.path.rsplit("/", 1)[-1])
        lower_filename = filename.lower()

        if any(lower_filename.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"]):
            return f"查看图片 {index}"
        if any(lower_filename.endswith(ext) for ext in [".mp4", ".mov", ".webm"]):
            return f"查看视频 {index}"
        if filename:
            return f"查看附件 {index} ({filename})"
        return f"查看附件 {index}"

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
