import importlib.util
import os
import sys
from typing import List, Dict, Optional, Any

class Config:
    """配置管理类"""
    
    def __init__(self):
        self.sender_type: str = "wechat"
        self.sender_routes: List[Dict[str, Any]] = []
        self.discord_channel_urls: List[str] = []
        self.wechat_receiver_name: str = ""
        self.enterprise_wechat_webhook: str = ""
        self.enterprise_wechat_webhook_list: List[Dict[str, str]] = []
        self.feishu_webhook: str = ""
        self.feishu_secret: str = ""
        self.feishu_webhook_list: List[Dict[str, str]] = []
        self.webhook_server_url: str = ""
        self.webhook_server_token: str = ""
        self.async_send_enabled: bool = False
        self.send_workers: int = 1
        self.send_queue_size: int = 1000
        self.check_interval: int = 3
        self.headless_mode: bool = False
        self.chrome_load_images: bool = True
        self.chrome_disable_notifications: bool = True
        self.chrome_mute_audio: bool = True
        
        self.load_config()

    def load_config(self):
        """从 config.py 加载配置"""
        try:
            # 动态导入根目录下的 config.py
            config_path = os.path.join(os.getcwd(), 'config.py')
            spec = importlib.util.spec_from_file_location("config", config_path)
            if spec and spec.loader:
                config_module = importlib.util.module_from_spec(spec)
                sys.modules["config"] = config_module
                spec.loader.exec_module(config_module)
                
                # 安全读取配置
                self.sender_type = getattr(config_module, 'SENDER_TYPE', 'wechat')
                self.sender_routes = getattr(config_module, 'SENDER_ROUTES', [])
                self.discord_channel_urls = getattr(config_module, 'DISCORD_CHANNEL_URLS', [])
                self.wechat_receiver_name = getattr(config_module, 'WECHAT_RECEIVER_NAME', '')
                self.enterprise_wechat_webhook = getattr(config_module, 'ENTERPRISE_WECHAT_WEBHOOK', '')
                self.enterprise_wechat_webhook_list = getattr(config_module, 'ENTERPRISE_WECHAT_WEBHOOK_LIST', [])
                self.feishu_webhook = getattr(config_module, 'FEISHU_WEBHOOK', '')
                self.feishu_secret = getattr(config_module, 'FEISHU_SECRET', '')
                self.feishu_webhook_list = getattr(config_module, 'FEISHU_WEBHOOK_LIST', [])
                self.webhook_server_url = getattr(config_module, 'WEBHOOK_SERVER_URL', '')
                if not self.webhook_server_url and self.sender_type == 'webhook_server':
                    self.webhook_server_url = os.getenv(
                        'WEBHOOK_SERVER_URL',
                        'http://webhook-server:8080/webhook/messages'
                    )
                self.webhook_server_token = getattr(config_module, 'WEBHOOK_SERVER_TOKEN', '')
                self.async_send_enabled = getattr(config_module, 'ASYNC_SEND_ENABLED', False)
                self.send_workers = getattr(config_module, 'SEND_WORKERS', 1)
                self.send_queue_size = getattr(config_module, 'SEND_QUEUE_SIZE', 1000)
                self.check_interval = getattr(config_module, 'CHECK_INTERVAL', 3)
                self.headless_mode = getattr(config_module, 'HEADLESS_MODE', False)
                self.chrome_load_images = getattr(config_module, 'CHROME_LOAD_IMAGES', True)
                self.chrome_disable_notifications = getattr(config_module, 'CHROME_DISABLE_NOTIFICATIONS', True)
                self.chrome_mute_audio = getattr(config_module, 'CHROME_MUTE_AUDIO', True)
                self._derive_discord_channels_from_routes()
        except Exception as e:
            print(f"加载配置文件失败: {e}")
            # 可以抛出异常或者使用默认值

    def _derive_discord_channels_from_routes(self):
        """当未显式配置 DISCORD_CHANNEL_URLS 时，从 SENDER_ROUTES 推导监听频道。"""
        if self.discord_channel_urls:
            return

        derived_channels = []
        seen = set()
        for route in self.sender_routes or []:
            channels = route.get('channels')
            if isinstance(channels, str):
                channels = [channels]
            if channels is None and route.get('channel'):
                channels = [route.get('channel')]

            for channel in channels or []:
                normalized = str(channel or '').rstrip('/')
                if normalized and normalized not in seen:
                    derived_channels.append(normalized)
                    seen.add(normalized)

        self.discord_channel_urls = derived_channels

# 全局单例
app_config = Config()

