
import logging
import os
import shutil
import subprocess
import time
from typing import Optional
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from src.services.listener.cdp import execute_cdp_command

logger = logging.getLogger(__name__)

class BrowserManager:
    """浏览器管理器：负责 Chrome 的初始化、启动和关闭"""

    def __init__(
        self,
        headless_mode: bool = False,
        load_images: bool = True,
        disable_notifications: bool = True,
        mute_audio: bool = True,
        performance_logging: bool = False
    ):
        self.headless_mode = headless_mode
        self.load_images = load_images
        self.disable_notifications = disable_notifications
        self.mute_audio = mute_audio
        self.performance_logging = performance_logging
        self.driver: Optional[webdriver.Chrome] = None

    def init_chrome(self) -> webdriver.Chrome:
        """初始化并返回 Chrome 驱动"""
        logger.info("⏳ 正在配置Chrome浏览器...")
        
        chrome_options = Options()
        if self.performance_logging:
            chrome_options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
        try:
            chrome_options.page_load_strategy = 'eager'
        except Exception:
            pass
        
        # 远程 Selenium 支持
        remote_url = os.getenv('SELENIUM_REMOTE_URL')
        if remote_url:
            return self._init_remote_chrome(remote_url, chrome_options)
        
        # 本地 Chrome 配置
        self._configure_local_options(chrome_options)
        
        # 启动驱动
        try:
            self.driver = webdriver.Chrome(options=chrome_options)
        except Exception as e:
            logger.error(f"   通过 Selenium Manager 启动失败: {e}")
            logger.info("   回退到系统 chromedriver...")
            self.driver = self._init_system_chromedriver(chrome_options)
            
        logger.info("✅ Chrome浏览器已成功启动")
        self._enable_network_monitoring()
        return self.driver

    def _init_remote_chrome(self, remote_url: str, options: Options) -> webdriver.Remote:
        if self.headless_mode:
            options.add_argument('--headless=new')
            logger.info("   使用无头模式运行，noVNC 中不会显示浏览器窗口")
        self._configure_common_chrome_options(options)
        options.add_argument(f'--window-size={self._chrome_window_size()}')
        options.add_argument('--user-data-dir=/home/seluser/discord-chrome-data')
        options.add_argument('--profile-directory=Default')

        # 代理支持：
        # - 仅设置 HTTP(S)_PROXY 环境变量通常不足以让 Chrome 自动走代理
        # - 这里读取 CHROME_PROXY（优先）或 HTTPS_PROXY/HTTP_PROXY，并注入 --proxy-server
        proxy = (os.getenv("CHROME_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or "").strip()
        if proxy:
            # Chrome 支持的示例：
            # - http://host:port
            # - socks5://host:port
            options.add_argument(f"--proxy-server={proxy}")
            logger.info(f"   Chrome 已启用代理: {proxy}")
        
        try:
            logger.info(f"   使用远程 Selenium: {remote_url}")
            self.driver = webdriver.Remote(command_executor=remote_url, options=options)
            self._remote_start_attempt = 1
            self._enable_network_monitoring()
            logger.info("✅ Chrome浏览器已成功启动(远程)")
            return self.driver
        except Exception as e:
            logger.error(f"   连接远程 Selenium 失败: {e}")
            error_text = str(e)
            if "Chrome instance exited" in error_text or "session not created" in error_text:
                logger.error("   提示：Chrome 可能因 selenium_data 权限、锁文件或残留会话启动失败")
                logger.error("   可尝试执行: bash bash/init_selenium.sh && docker compose restart")
            attempt = getattr(self, "_remote_start_attempt", 1)
            retries = self._chrome_start_retries()
            if attempt < retries:
                delay = min(30, 5 * attempt)
                logger.warning(f"   Chrome start failed, retrying in {delay}s ({attempt}/{retries})...")
                self._remote_start_attempt = attempt + 1
                time.sleep(delay)
                return self._init_remote_chrome(remote_url, options)

            self._remote_start_attempt = 1
            raise

    def _configure_local_options(self, options: Options):
        if self.headless_mode:
            options.add_argument('--headless=new')
            logger.info("   使用无头模式运行")

        self._configure_common_chrome_options(options)
        options.add_argument(f'--window-size={self._chrome_window_size()}')
        options.add_argument('--user-data-dir=./chrome_data')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option('excludeSwitches', ['enable-automation'])
        options.add_experimental_option('useAutomationExtension', False)

    def _configure_common_chrome_options(self, options: Options):
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--disable-software-rasterizer')
        options.add_argument('--disable-extensions')
        options.add_argument('--no-zygote')
        options.add_argument('--disable-background-networking')
        options.add_argument('--disable-client-side-phishing-detection')
        options.add_argument('--disable-component-update')
        options.add_argument('--disable-default-apps')
        options.add_argument('--disable-domain-reliability')
        options.add_argument('--disable-hang-monitor')
        options.add_argument('--disable-prompt-on-repost')
        options.add_argument('--disable-sync')
        options.add_argument('--disable-translate')
        options.add_argument('--disable-background-timer-throttling')
        options.add_argument('--disable-backgrounding-occluded-windows')
        options.add_argument('--disable-renderer-backgrounding')
        options.add_argument('--metrics-recording-only')
        options.add_argument('--process-per-site')
        options.add_argument(
            '--disable-features='
            'VizDisplayCompositor,UseOzonePlatform,MediaRouter,Translate,'
            'OptimizationHints,InterestFeedContentSuggestions,AutofillServerCommunication'
        )
        options.add_argument('--remote-debugging-port=0')

        renderer_limit = os.getenv("CHROME_RENDERER_PROCESS_LIMIT", "4").strip()
        if renderer_limit and renderer_limit != "0":
            options.add_argument(f"--renderer-process-limit={renderer_limit}")

        if self.disable_notifications:
            options.add_argument('--disable-notifications')
        if self.mute_audio:
            options.add_argument('--mute-audio')
        if not self.load_images:
            options.add_argument('--blink-settings=imagesEnabled=false')
            logger.info("   Chrome 已禁用图片加载以降低资源占用")

        prefs = {}
        if not self.load_images:
            prefs["profile.managed_default_content_settings.images"] = 2
        if self.disable_notifications:
            prefs["profile.default_content_setting_values.notifications"] = 2
        if prefs:
            options.add_experimental_option("prefs", prefs)

    def _init_system_chromedriver(self, options: Options) -> webdriver.Chrome:
        try:
            chromedriver_path = shutil.which('chromedriver') or '/usr/bin/chromedriver'
            return webdriver.Chrome(service=Service(executable_path=chromedriver_path), options=options)
        except Exception as e:
            logger.error(f"   使用系统 chromedriver 启动失败: {e}")
            raise

    def _enable_network_monitoring(self):
        if not self.performance_logging or not self.driver:
            return

        try:
            execute_cdp_command(self.driver, "Network.enable", {})
            logger.info("   Chrome performance log 已启用，用于监听 WebSocket 消息")
        except Exception as e:
            logger.warning(f"   启用 Chrome Network 监控失败，将尝试继续读取 performance log: {e}")

    @staticmethod
    def _chrome_start_retries() -> int:
        try:
            return max(1, int(os.getenv("CHROME_START_RETRIES", "3")))
        except ValueError:
            return 3

    @staticmethod
    def _chrome_window_size() -> str:
        value = os.getenv("CHROME_WINDOW_SIZE", "1280,720").strip()
        if "x" in value:
            value = value.replace("x", ",", 1)
        parts = value.split(",", 1)
        if len(parts) == 2 and all(part.strip().isdigit() for part in parts):
            return f"{int(parts[0])},{int(parts[1])}"
        return "1280,720"

    def cleanup(self):
        """清理浏览器资源"""
        if self.driver:
            try:
                self.driver.quit()
                logger.info("   ✅ Chrome浏览器已关闭")
            except Exception as e:
                logger.debug(f"   关闭Chrome失败: {e}")
                self._force_kill_chromedriver()

    def _force_kill_chromedriver(self):
        # 尝试强制终止残留进程
        try:
            subprocess.run(["pkill", "-f", "chromedriver"], check=False)
        except Exception:
            pass

