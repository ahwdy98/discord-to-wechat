#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Discord到微信/企业微信消息桥接器
使用浏览器自动化监听Discord消息，通过多种方式转发消息

支持的发送方式：
1. 微信个人号（itchat）- 小号发送给大号
2. 企业微信机器人（Webhook）- 发送到企业微信群
"""

from typing import Dict

# 导入核心模块
from src.core.config_manager import app_config
from src.core.models import DiscordMessage
from src.utils.logger import get_logger, setup_logger

# 导入业务模块
from src.services.listener.discord_listener import DiscordListener
from src.services.listener.discord_websocket_listener import DiscordWebsocketListener
from src.services.sender.base import MessageSender
from src.services.sender.wechat import WechatSender
from src.services.sender.working_wechat import WorkingWechatSender
from src.services.sender.feishu import FeishuSender
from src.services.sender.webhook_server import WebhookServerSender
from src.services.sender.router import ChannelRoutingSender
from src.services.sender.async_sender import AsyncMessageSender

# 初始化日志
logger = setup_logger()

SUPPORTED_SENDER_TYPES = ["wechat", "enterprise_wechat", "feishu", "webhook_server"]
SUPPORTED_LISTENER_MODES = ["browser_tabs", "websocket"]


class DiscordToWechatBridge:
    """Discord到微信/企业微信的消息桥接器"""
    
    def __init__(self):
        """初始化Discord到微信的消息桥"""
        # 从配置管理器加载配置
        self.config = app_config
        
        # 初始化消息发送器
        self.sender = self._create_sender()
        
        # 初始化Discord监听器
        self.listener = self._create_listener()

    def _create_listener(self):
        if self.config.discord_listener_mode == "websocket":
            logger.info("Using Discord WebSocket listener mode (single page, event driven)")
            return DiscordWebsocketListener(
                channel_urls=self.config.discord_channel_urls,
                on_new_message=self._on_new_message,
                check_interval=self.config.websocket_poll_interval,
                headless_mode=self.config.headless_mode,
                chrome_load_images=self.config.chrome_load_images,
                chrome_disable_notifications=self.config.chrome_disable_notifications,
                chrome_mute_audio=self.config.chrome_mute_audio
            )

        logger.info("Using Discord browser-tabs polling listener mode")
        return DiscordListener(
            channel_urls=self.config.discord_channel_urls,
            on_new_message=self._on_new_message,
            check_interval=self.config.check_interval,
            headless_mode=self.config.headless_mode,
            chrome_load_images=self.config.chrome_load_images,
            chrome_disable_notifications=self.config.chrome_disable_notifications,
            chrome_mute_audio=self.config.chrome_mute_audio
        )
    
    def _create_sender(self) -> MessageSender:
        """创建消息发送器"""
        default_sender = self._create_sender_by_type(self.config.sender_type, {})

        if self.config.sender_routes:
            logger.info(f"🔀 已启用频道发送路由: {len(self.config.sender_routes)} 条")
            sender = ChannelRoutingSender(
                default_sender=default_sender,
                routes=self.config.sender_routes,
                sender_factory=self._create_sender_by_type
            )
            return self._wrap_sender(sender)

        return self._wrap_sender(default_sender)

    def _wrap_sender(self, sender: MessageSender) -> MessageSender:
        """根据配置包装发送器，例如启用异步发送队列。"""
        if not self.config.async_send_enabled:
            return sender

        return AsyncMessageSender(
            sender=sender,
            workers=self.config.send_workers,
            queue_size=self.config.send_queue_size
        )

    def _create_sender_by_type(self, sender_type: str, overrides: Dict) -> MessageSender:
        """按类型创建消息发送器，可用 overrides 覆盖默认配置"""
        if sender_type == "wechat":
            logger.info("📱 使用发送方式: 微信个人号")
            receiver_name = overrides.get("wechat_receiver_name") or self.config.wechat_receiver_name
            if not receiver_name or receiver_name == "na":
                logger.error("❌ 请先在 config.py 中配置 WECHAT_RECEIVER_NAME")
                raise ValueError("微信接收者名称未配置")
            return WechatSender(receiver_name=receiver_name)
        
        elif sender_type == "enterprise_wechat":
            logger.info("🤖 使用发送方式: 企业微信机器人")
            
            webhook_url = (
                overrides.get("enterprise_wechat_webhook")
                or overrides.get("webhook")
                or overrides.get("hook")
                or self.config.enterprise_wechat_webhook
            )
            webhook_configs = overrides.get("enterprise_wechat_webhook_list", self.config.enterprise_wechat_webhook_list)
            has_list_config = webhook_configs and len(webhook_configs) > 0
            has_single_config = webhook_url and "YOUR_WEBHOOK_KEY" not in webhook_url
            
            if not has_list_config and not has_single_config:
                logger.error("❌ 请先在 config.py 中配置 ENTERPRISE_WECHAT_WEBHOOK_LIST 或 ENTERPRISE_WECHAT_WEBHOOK")
                raise ValueError("企业微信Webhook未配置")
            
            return WorkingWechatSender(
                webhook_url=webhook_url,
                webhook_configs=webhook_configs
            )

        elif sender_type == "feishu":
            logger.info("🚀 使用发送方式: 飞书自定义机器人")

            webhook_url = (
                overrides.get("feishu_webhook")
                or overrides.get("webhook")
                or overrides.get("hook")
                or self.config.feishu_webhook
            )
            secret = overrides.get("feishu_secret") or overrides.get("secret") or self.config.feishu_secret
            webhook_configs = overrides.get("feishu_webhook_list", self.config.feishu_webhook_list)
            has_list_config = webhook_configs and len(webhook_configs) > 0
            has_single_config = webhook_url and "YOUR_WEBHOOK" not in webhook_url

            if not has_list_config and not has_single_config:
                logger.error("❌ 请先在 config.py 中配置 FEISHU_WEBHOOK_LIST 或 FEISHU_WEBHOOK")
                raise ValueError("飞书Webhook未配置")

            return FeishuSender(
                webhook_url=webhook_url,
                secret=secret,
                webhook_configs=webhook_configs
            )

        elif sender_type == "webhook_server":
            logger.info("🗄️ 使用发送方式: 本地 Webhook Server")

            endpoint_url = overrides.get("webhook_server_url") or overrides.get("url") or self.config.webhook_server_url
            token = overrides.get("webhook_server_token") or overrides.get("token") or self.config.webhook_server_token

            if not endpoint_url:
                logger.error("❌ 请先在 config.py 中配置 WEBHOOK_SERVER_URL")
                raise ValueError("Webhook Server URL 未配置")

            return WebhookServerSender(
                endpoint_url=endpoint_url,
                token=token
            )
        
        else:
            logger.error(f"❌ 不支持的发送器类型: {sender_type}")
            logger.error(f"   支持的类型: {', '.join(SUPPORTED_SENDER_TYPES)}")
            raise ValueError(f"不支持的发送器类型: {sender_type}")
    
    def _on_new_message(self, message: DiscordMessage):
        """
        新消息回调函数
        :param message: Discord消息对象
        """
        # 发送消息
        self.sender.send_message(message)
    
    def run(self):
        """运行主程序"""
        try:
            logger.info("🚀 Discord to WeChat Bridge 启动中...")
            logger.info("=" * 50)
            
            # 步骤 1: 初始化并登录发送器
            logger.info("\n" + "=" * 50)
            logger.info("🔧 步骤 1/4: 初始化消息发送器...")
            logger.info("=" * 50)
            
            if not self.sender.login():
                logger.error("❌ 消息发送器初始化失败，程序退出")
                return
            
            # 启动发送器的保持活跃线程（如果需要）
            self.sender.keep_alive()
            
            # 步骤 2: 初始化浏览器
            logger.info("\n" + "=" * 50)
            logger.info("🔧 步骤 2/4: 初始化Chrome浏览器...")
            logger.info("=" * 50)
            self.listener.init_chrome()
            
            # 步骤 3: 登录Discord
            logger.info("\n" + "=" * 50)
            logger.info("🔐 步骤 3/4: 登录Discord...")
            logger.info("=" * 50)
            self.listener.login_discord()
            
            # 步骤 4: 打开频道并开始监控
            logger.info("\n" + "=" * 50)
            logger.info("📱 步骤 4/4: 打开Discord频道并开始监控...")
            logger.info("=" * 50)
            self.listener.navigate_to_channel()
            
            # 开始监控消息
            self.listener.monitor_messages()
            
        except KeyboardInterrupt:
            logger.info("\n\n⏹️  程序被用户中断")
        except Exception as e:
            logger.error(f"\n❌ 程序异常: {e}", exc_info=True)
        finally:
            self.cleanup()
    
    def cleanup(self):
        """清理资源"""
        logger.info("\n🧹 清理资源...")
        
        # 清理监听器
        if hasattr(self, 'listener') and self.listener:
            self.listener.cleanup()
        
        # 清理发送器
        if hasattr(self, 'sender') and self.sender:
            self.sender.cleanup()
        
        logger.info("👋 程序已退出")


def validate_config():
    """验证配置是否正确"""
    if not app_config.discord_channel_urls:
        logger.error("❌ 请先在 config.py 中配置 DISCORD_CHANNEL_URLS，或在 SENDER_ROUTES 中配置 channel/channels")
        return False
    
    if app_config.sender_type not in SUPPORTED_SENDER_TYPES:
        logger.error(f"❌ SENDER_TYPE 配置错误: {app_config.sender_type}")
        return False

    if app_config.discord_listener_mode not in SUPPORTED_LISTENER_MODES:
        logger.error(f"❌ DISCORD_LISTENER_MODE 配置错误: {app_config.discord_listener_mode}")
        logger.error(f"   支持的监听模式: {', '.join(SUPPORTED_LISTENER_MODES)}")
        return False

    if app_config.websocket_poll_interval <= 0:
        logger.error("❌ WEBSOCKET_POLL_INTERVAL 必须大于 0")
        return False

    if app_config.async_send_enabled:
        if app_config.send_workers < 1:
            logger.error("❌ SEND_WORKERS 必须大于等于 1")
            return False
        if app_config.send_queue_size < 1:
            logger.error("❌ SEND_QUEUE_SIZE 必须大于等于 1")
            return False

    for index, route in enumerate(app_config.sender_routes or [], 1):
        route_sender_type = route.get("sender_type") or route.get("type")
        route_channels = route.get("channels") or ([route.get("channel")] if route.get("channel") else [])
        if isinstance(route_channels, str):
            route_channels = [route_channels]

        if route_sender_type not in SUPPORTED_SENDER_TYPES:
            logger.error(f"❌ SENDER_ROUTES[{index}] sender_type 配置错误: {route_sender_type}")
            return False
        if not route_channels:
            logger.error(f"❌ SENDER_ROUTES[{index}] 请配置 channel 或 channels")
            return False
    
    if app_config.sender_type == "wechat":
        if "你的大号" in app_config.wechat_receiver_name or app_config.wechat_receiver_name == "na":
            logger.error("❌ 请先在 config.py 中配置 WECHAT_RECEIVER_NAME")
            return False
    
    elif app_config.sender_type == "enterprise_wechat":
        valid_list = app_config.enterprise_wechat_webhook_list and len(app_config.enterprise_wechat_webhook_list) > 0
        valid_single = app_config.enterprise_wechat_webhook and "YOUR_WEBHOOK_KEY" not in app_config.enterprise_wechat_webhook
        
        if not valid_list and not valid_single:
            logger.error("❌ 请先在 config.py 中配置 ENTERPRISE_WECHAT_WEBHOOK_LIST 或 ENTERPRISE_WECHAT_WEBHOOK")
            return False

    elif app_config.sender_type == "feishu":
        valid_list = app_config.feishu_webhook_list and len(app_config.feishu_webhook_list) > 0
        valid_single = app_config.feishu_webhook and "YOUR_WEBHOOK" not in app_config.feishu_webhook

        if not valid_list and not valid_single:
            logger.error("❌ 请先在 config.py 中配置 FEISHU_WEBHOOK_LIST 或 FEISHU_WEBHOOK")
            return False

    elif app_config.sender_type == "webhook_server":
        if not app_config.webhook_server_url:
            logger.error("❌ 请先在 config.py 中配置 WEBHOOK_SERVER_URL")
            return False
    
    return True


def print_startup_info():
    """打印启动信息"""
    logger.info("\n" + "=" * 60)
    logger.info("    Discord to WeChat/Enterprise WeChat Bridge")
    logger.info("=" * 60)
    
    # 发送方式信息
    if app_config.sender_type == "wechat":
        logger.info("📱 发送方式: 微信个人号")
        logger.info(f"👤 接收者: {app_config.wechat_receiver_name}")
    elif app_config.sender_type == "enterprise_wechat":
        logger.info("🤖 发送方式: 企业微信机器人")
        
        if app_config.enterprise_wechat_webhook_list:
             logger.info(f"🔗 已配置 {len(app_config.enterprise_wechat_webhook_list)} 个Webhook映射")
        else:
             webhook = app_config.enterprise_wechat_webhook
             logger.info(f"🔗 Webhook: {webhook[:30] if webhook else ''}...")
    elif app_config.sender_type == "feishu":
        logger.info("🚀 发送方式: 飞书自定义机器人")

        if app_config.feishu_webhook_list:
             logger.info(f"🔗 已配置 {len(app_config.feishu_webhook_list)} 个飞书Webhook映射")
        else:
             webhook = app_config.feishu_webhook
             logger.info(f"🔗 Feishu Webhook: {webhook[:30] if webhook else ''}...")
    elif app_config.sender_type == "webhook_server":
        logger.info("🗄️ 发送方式: 本地 Webhook Server")
        logger.info(f"🔗 Webhook Server: {app_config.webhook_server_url}")
    
    # Discord频道信息
    logger.info(f"\n📋 监控 {len(app_config.discord_channel_urls)} 个Discord频道")
    if app_config.sender_routes:
        logger.info(f"🔀 频道发送路由: {len(app_config.sender_routes)} 条")
    
    # 运行配置
    logger.info(f"\n⚙️  运行配置:")
    logger.info(f"   检查间隔: {app_config.check_interval} 秒")
    logger.info(f"   无头模式: {'是' if app_config.headless_mode else '否'}")
    logger.info(f"   加载图片: {'是' if app_config.chrome_load_images else '否'}")
    logger.info(f"   异步发送: {'是' if app_config.async_send_enabled else '否'}")
    if app_config.async_send_enabled:
        logger.info(f"   发送Worker: {app_config.send_workers}, 队列大小: {app_config.send_queue_size}")
    logger.info("=" * 60 + "\n")


def main():
    """主函数"""
    # 验证配置
    if not validate_config():
        return
    
    # 打印启动信息
    print_startup_info()
    
    # 创建并运行桥接器
    try:
        bridge = DiscordToWechatBridge()
        bridge.run()
    
    except ValueError as e:
        logger.error(f"配置错误: {e}")
    except Exception as e:
        logger.error(f"启动失败: {e}", exc_info=True)


if __name__ == "__main__":
    main()
