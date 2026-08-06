#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Discord消息监听器
使用Selenium监听Discord频道的新消息
"""

import time
import os
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from src.core.models import DiscordMessage
from src.utils.logger import get_logger
from src.services.listener.browser import BrowserManager

logger = get_logger(__name__)


class DiscordListener:
    """Discord消息监听器"""
    
    def __init__(
        self,
        channel_urls: List[str],
        on_new_message: Callable[[DiscordMessage], None],
        check_interval: int = 3,
        headless_mode: bool = False,
        chrome_load_images: bool = True,
        chrome_disable_notifications: bool = True,
        chrome_mute_audio: bool = True
    ):
        """
        初始化Discord监听器
        :param channel_urls: Discord频道URL列表
        :param on_new_message: 新消息回调函数，参数为 (message: DiscordMessage)
        :param check_interval: 检查间隔（秒）
        :param headless_mode: 是否使用无头模式
        :param chrome_load_images: 是否加载图片资源
        :param chrome_disable_notifications: 是否禁用浏览器通知
        :param chrome_mute_audio: 是否静音
        """
        self.channel_urls = channel_urls if isinstance(channel_urls, list) else [channel_urls]
        self.on_new_message = on_new_message
        self.check_interval = check_interval
        
        # 浏览器管理器
        self.browser_manager = BrowserManager(
            headless_mode=headless_mode,
            load_images=chrome_load_images,
            disable_notifications=chrome_disable_notifications,
            mute_audio=chrome_mute_audio
        )
        self.driver = None
        
        # 为每个频道维护独立的最后消息ID
        self.last_message_ids = {url: None for url in self.channel_urls}
        self.seen_message_keys = set()
        self.dom_forward_after_utc = None
        self.dom_startup_quarantine_until = 0
        self.dom_ignored_old_message_ids = set()
        self.channel_names = {}
        # 为每个频道维护独立的浏览器标签页句柄（window handle）
        self.channel_handles = {}
        self._last_tab_reconcile_at = 0.0
        self._last_switch_error = None
    
    def init_chrome(self):
        """初始化Chrome浏览器"""
        self.driver = self.browser_manager.init_chrome()
    
    def login_discord(self):
        """登录Discord（首次需要手动登录）"""
        logger.info("⏳ 正在打开Discord...")
        self.driver.get('https://discord.com/login')
        
        # 检查是否已经登录
        time.sleep(3)
        current_url = self.driver.current_url
        
        if 'login' in current_url:
            logger.info("⚠️  请在浏览器中登录Discord...")
            logger.info("   提示：登录后会自动保存登录状态，下次不用再登录")
            logger.info("   🌐 如果使用Docker，请访问 http://localhost:7900 在noVNC中登录")
            logger.info("   🔑 noVNC默认密码: secret")
            
            # 等待用户登录完成
            while 'login' in self.driver.current_url:
                time.sleep(2)
            
            logger.info("✅ Discord登录成功！")
            logger.info("⏳ 正在保存登录状态，请稍候...")
            # 登录成功后多等待几秒，确保Chrome有足够时间将会话数据写入磁盘
            time.sleep(8)
            logger.info("✅ 登录状态已保存")
        else:
            logger.info("✅ Discord已经登录，跳过登录步骤")
        
        # 等待几秒让页面完全加载
        time.sleep(3)
    
    def restart_browser(self):
        """重启浏览器并重新登录"""
        logger.info("♻️ 正在重启浏览器...")
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
            
        self.channel_handles = {}
        self._last_switch_error = None
        self.init_chrome()
        self.login_discord()
        logger.info("✅ 浏览器重启完成")

    @staticmethod
    def _is_browser_session_lost(error) -> bool:
        if not error:
            return False
        text = str(error).lower()
        return any(
            phrase in text
            for phrase in (
                "unable to find session",
                "invalid session id",
                "no such session",
                "session timed out due to inactivity",
                "session was removed",
            )
        )

    def _recover_browser_session(self, reason: str) -> None:
        logger.error(f"检测到 Selenium 浏览器会话已失效，正在自动重建: {reason}")
        self.restart_browser()
        self.navigate_to_channel()
        self._last_tab_reconcile_at = 0.0
        logger.info("✅ Selenium 浏览器会话已重建，继续监控")

    @staticmethod
    def _channel_id_from_url(url: str) -> str:
        """从 Discord 频道 URL 中提取频道 ID。"""
        cleaned = str(url or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
        parts = [part for part in cleaned.split("/") if part]
        if len(parts) >= 3 and parts[-3] == "channels":
            return parts[-1]
        return ""

    def _current_url_matches_channel(self, current_url: str, channel_url: str) -> bool:
        expected_channel_id = self._channel_id_from_url(channel_url)
        actual_channel_id = self._channel_id_from_url(current_url)
        return bool(expected_channel_id and actual_channel_id == expected_channel_id)

    def _wait_for_channel_url(self, channel_url: str, timeout: float = 8.0, stable_seconds: float = 1.2) -> bool:
        deadline = time.monotonic() + max(0.5, timeout)
        matched_since = None
        while time.monotonic() < deadline:
            try:
                if self._current_url_matches_channel(self.driver.current_url or "", channel_url):
                    if matched_since is None:
                        matched_since = time.monotonic()
                    if time.monotonic() - matched_since >= stable_seconds:
                        return True
                else:
                    matched_since = None
            except Exception:
                matched_since = None
            time.sleep(0.25)
        return False

    def _find_handle_for_channel(self, channel_url: str, skip_handles: Optional[set] = None) -> Optional[str]:
        skip_handles = skip_handles or set()
        original_handle = None
        try:
            original_handle = self.driver.current_window_handle
        except Exception:
            pass

        for handle in list(self.driver.window_handles):
            if handle in skip_handles:
                continue
            try:
                self.driver.switch_to.window(handle)
                if self._current_url_matches_channel(self.driver.current_url or "", channel_url):
                    return handle
            except Exception:
                continue
        try:
            if original_handle and original_handle in self.driver.window_handles:
                self.driver.switch_to.window(original_handle)
        except Exception:
            pass
        return None

    def _navigate_current_tab_to_channel(self, channel_url: str) -> bool:
        self.driver.get(channel_url)
        if not self._wait_for_channel_url(channel_url):
            logger.warning(
                "频道标签页导航后仍未落到目标频道: "
                f"expected={channel_url}, current={self.driver.current_url}"
            )
            return False
        self._install_dom_observer(channel_url)
        self._refresh_channel_name(channel_url)
        return True

    def _reconcile_channel_tabs(self, force: bool = False) -> None:
        """重新核对所有频道和标签页的绑定，修复重复/空白/串频道标签页。"""
        try:
            interval = max(5.0, float(os.getenv("DISCORD_TAB_RECONCILE_INTERVAL", "30")))
        except ValueError:
            interval = 30.0
        now = time.monotonic()
        if not force and now - self._last_tab_reconcile_at < interval:
            return
        self._last_tab_reconcile_at = now

        handles = list(self.driver.window_handles)
        if not handles:
            return

        configured_pairs = [
            (self._channel_id_from_url(url), url)
            for url in self.channel_urls
            if self._channel_id_from_url(url)
        ]
        configured_ids = {channel_id for channel_id, _url in configured_pairs}
        snapshots = []
        original_handle = None
        try:
            original_handle = self.driver.current_window_handle
        except Exception:
            pass

        for handle in handles:
            try:
                self.driver.switch_to.window(handle)
                current_url = self.driver.current_url or ""
                snapshots.append({
                    "handle": handle,
                    "url": current_url,
                    "channel_id": self._channel_id_from_url(current_url),
                })
            except Exception:
                snapshots.append({"handle": handle, "url": "", "channel_id": ""})

        unused_handles = []
        selected_handles = set()
        satisfied_urls = set()
        repaired = []

        for channel_id, channel_url in configured_pairs:
            matches = [
                item for item in snapshots
                if item["handle"] not in selected_handles and item["channel_id"] == channel_id
            ]
            if not matches:
                continue

            cached = self.channel_handles.get(channel_url)
            chosen = next((item for item in matches if item["handle"] == cached), matches[0])
            self.channel_handles[channel_url] = chosen["handle"]
            selected_handles.add(chosen["handle"])
            satisfied_urls.add(channel_url)
            if chosen["handle"] != cached:
                repaired.append(f"{channel_id}:reuse")
            for extra in matches:
                if extra["handle"] != chosen["handle"]:
                    unused_handles.append(extra["handle"])

        for item in snapshots:
            if item["handle"] in selected_handles or item["handle"] in unused_handles:
                continue
            if item["channel_id"] not in configured_ids:
                unused_handles.append(item["handle"])

        for channel_id, channel_url in configured_pairs:
            if channel_url in satisfied_urls:
                continue

            target_handle = unused_handles.pop(0) if unused_handles else None

            if not target_handle:
                try:
                    self.driver.switch_to.new_window("tab")
                    target_handle = self.driver.current_window_handle
                    handles.append(target_handle)
                    snapshots.append({"handle": target_handle, "url": "", "channel_id": ""})
                except Exception as e:
                    logger.warning(f"新建频道标签页失败: {channel_url}, error={e}")
                    continue

            try:
                self.driver.switch_to.window(target_handle)
                if self._navigate_current_tab_to_channel(channel_url):
                    self.channel_handles[channel_url] = target_handle
                    selected_handles.add(target_handle)
                    satisfied_urls.add(channel_url)
                    repaired.append(f"{channel_id}:navigate")
            except Exception as e:
                logger.warning(f"修复频道标签页失败: {channel_url}, error={e}")

        try:
            if original_handle and original_handle in self.driver.window_handles:
                self.driver.switch_to.window(original_handle)
        except Exception:
            pass

        if repaired:
            logger.info(f"频道标签页绑定已校准: {', '.join(repaired)}")

    def navigate_to_channel(self, channel_url: Optional[str] = None):
        """打开/切换到指定频道"""
        if channel_url:
            self.switch_to_channel(channel_url)
        else:
            # 初始化打开所有频道
            logger.info(f"⏳ 正在打开 {len(self.channel_urls)} 个频道...")
            for idx, url in enumerate(self.channel_urls, 1):
                logger.info(f"   [{idx}/{len(self.channel_urls)}] {url}")
                self.switch_to_channel(url)
                # 稍微等待，避免操作过快
                time.sleep(2)

            # 切回第一个频道
            if self.channel_urls:
                self.switch_to_channel(self.channel_urls[0])
            self._reconcile_channel_tabs(force=True)
            logger.info("✅ 频道已成功打开")

    def switch_to_channel(self, channel_url: str) -> bool:
        """切换到指定频道对应的标签页"""
        self._last_switch_error = None
        try:
            # 1. 尝试直接使用缓存的句柄
            handle = self.channel_handles.get(channel_url)
            # 句柄存在且有效
            if handle and handle in self.driver.window_handles:
                # 获取当前窗口句柄，如果当前窗口已关闭，设为 None
                try:
                    current_handle = self.driver.current_window_handle
                except Exception:
                    current_handle = None

                if current_handle != handle:
                    logger.debug("⏳ 正在切换到频道标签页...")
                    # logger.info(f"   URL: {channel_url}")
                    self.driver.switch_to.window(handle)
                    time.sleep(0.1)

                current_url = self.driver.current_url or ""
                if not self._current_url_matches_channel(current_url, channel_url):
                    logger.warning(
                        "频道标签页 URL 已偏离，重新导航: "
                        f"expected={channel_url}, current={current_url}"
                    )
                    existing_handle = self._find_handle_for_channel(channel_url, {handle})
                    if existing_handle:
                        self.channel_handles[channel_url] = existing_handle
                        self.driver.switch_to.window(existing_handle)
                        self._install_dom_observer(channel_url)
                        self._refresh_channel_name(channel_url)
                        return True
                    if not self._navigate_current_tab_to_channel(channel_url):
                        return False
                return True

            existing_handle = self._find_handle_for_channel(channel_url)
            if existing_handle:
                self.channel_handles[channel_url] = existing_handle
                self.driver.switch_to.window(existing_handle)
                self._install_dom_observer(channel_url)
                self._refresh_channel_name(channel_url)
                return True

            # 3. 未找到则需要打开
            # 如果是第一个初始化的频道（还没有任何句柄记录），则复用当前页面（如登录后的页面）
            if not self.channel_handles:
                logger.info(f"⏳ 初始化频道，覆盖当前页面: {channel_url}")
                self.channel_handles[channel_url] = self.driver.current_window_handle
                return self._navigate_current_tab_to_channel(channel_url)

            # 否则新建标签页
            logger.info("⏳ 未找到频道标签页，正在新建...")
            logger.info(f"   URL: {channel_url}")
            
            # 确保在打开新窗口前有一个有效的上下文
            # 如果当前窗口已关闭（例如用户手动关闭了标签页），switch_to.new_window 可能会失败
            try:
                self.driver.current_window_handle
            except Exception:
                # 当前窗口句柄失效，尝试切换到任意存在的窗口
                try:
                    if self.driver.window_handles:
                        self.driver.switch_to.window(self.driver.window_handles[0])
                except Exception:
                    pass

            # 遍历所有句柄查找未被记录的
            # === 使用 Selenium 4 新 API ===
            self.driver.switch_to.new_window('tab')
            self.channel_handles[channel_url] = self.driver.current_window_handle
            return self._navigate_current_tab_to_channel(channel_url)
        except Exception as e:
            self._last_switch_error = e
            logger.error(f"切换频道标签页失败: {e}")
            return False
    
    def get_channel_name(self, channel_url: str) -> str:
        """从URL中提取频道标识"""
        cached_name = self.channel_names.get(channel_url)
        if cached_name:
            return cached_name

        try:
            parts = channel_url.rstrip('/').split('/')
            if len(parts) >= 2:
                return f"频道{parts[-1]}"
            return "未知频道"
        except:
            return "未知频道"

    def _refresh_channel_name(self, channel_url: str) -> str:
        try:
            channel_name = self.driver.execute_script(
                """
                function clean(value) {
                  return String(value || "")
                    .trim()
                    .replace(/^#/, "")
                    .replace(/^["']|["']$/g, "")
                    .trim();
                }

                function fromHeader() {
                  const header = document.querySelector('h1[class*="title"], div[class*="titleWrapper"] h1');
                  if (!header) return "";
                  const lines = (header.innerText || header.textContent || "")
                    .split("\\n")
                    .map(clean)
                    .filter(Boolean);
                  if (lines.length >= 2) {
                    const server = lines[0].replace(/:$/, "").trim();
                    return `${server}: ${lines.slice(1).join(" ")}`;
                  }
                  return lines[0] || "";
                }

                const headerName = fromHeader();
                if (headerName) return headerName;

                const titleParts = String(document.title || "").split("|").map(part => clean(part));
                if (titleParts.length >= 2) {
                  const channel = titleParts[1];
                  const server = titleParts[2] || "";
                  if (channel) return server ? `${server}: ${channel}` : channel;
                }
                return "";
                """
            )
            channel_name = str(channel_name or "").strip()
            if channel_name:
                previous = self.channel_names.get(channel_url)
                self.channel_names[channel_url] = channel_name
                if previous != channel_name:
                    logger.info(f"Resolved Discord channel name: {channel_name} ({channel_url})")
                return channel_name
        except Exception as e:
            logger.debug(f"Failed to refresh Discord channel name: {e}")

        return self.get_channel_name(channel_url)
    
    def _dom_observer_script(self) -> str:
        return r"""
(function () {
  if (window.__discordDomBridgeInstalled) return true;
  window.__discordDomBridgeInstalled = true;
  window.__discordDomBridgeQueue = [];
  window.__discordDomBridgeSeen = window.__discordDomBridgeSeen || {};
  window.__discordDomBridgeBaselineUntil = Date.now() + 6000;
  window.__discordDomBridgeStats = { queued: 0, ignored: 0, errors: 0, startedAt: Date.now() };

  if (!document.getElementById("__discordDomBridgeReduceMotion")) {
    const style = document.createElement("style");
    style.id = "__discordDomBridgeReduceMotion";
    style.textContent = `
      *, *::before, *::after {
        animation-duration: 0.001s !important;
        animation-iteration-count: 1 !important;
        scroll-behavior: auto !important;
        transition-duration: 0.001s !important;
      }
    `;
    document.documentElement.appendChild(style);
  }

  function messageRoot(node) {
    let el = node && node.nodeType === Node.ELEMENT_NODE ? node : node && node.parentElement;
    while (el && el !== document.body) {
      if (el.matches && el.matches('li[id^="chat-messages-"]')) return el;
      el = el.parentElement;
    }
    return null;
  }

  function textAll(root, selectors) {
    const parts = [];
    for (const selector of selectors) {
      for (const el of root.querySelectorAll(selector)) {
        const text = (el.innerText || el.textContent || "").trim();
        if (text) parts.push(text);
      }
    }
    return parts;
  }

  function firstText(root, selectors) {
    for (const selector of selectors) {
      const el = root.querySelector(selector);
      const text = el && (el.innerText || el.textContent || "").trim();
      if (text) return text;
    }
    return "";
  }

  function parseMessage(root, eventKind) {
    const id = root.getAttribute("id") || "";
    if (!id) return null;

    const username = firstText(root, [
      'h3[class*="header"] span[class*="username"]',
      'span[class*="username"]'
    ]) || ((root.getAttribute("aria-label") || "").split(",")[0].trim()) || "Unknown user";

    const contentParts = textAll(root, [
      'div[id^="message-content-"]',
      'div[class*="messageContent"]',
      'div[class*="embedTitle"]',
      'div[class*="embedDescription"]',
      'div[class*="embedFieldValue"]'
    ]);

    const attachments = [];
    for (const a of root.querySelectorAll('a[href]')) {
      const href = (a.getAttribute("href") || "").trim();
      const lower = href.toLowerCase();
      if (!href) continue;
      if ((lower.includes("cdn.discordapp.com") || lower.includes("media.discordapp.net")) &&
          (lower.includes("/attachments/") || /\.(png|jpe?g|gif|webp|mp4|mov|webm)(\?|$)/.test(lower))) {
        attachments.push(href);
      }
    }
    for (const img of root.querySelectorAll('img[src]')) {
      const src = (img.getAttribute("src") || "").trim();
      const lower = src.toLowerCase();
      if (src && (lower.includes("cdn.discordapp.com") || lower.includes("media.discordapp.net")) &&
          lower.includes("/attachments/")) {
        attachments.push(src);
      }
    }

    const uniqueContent = Array.from(new Set(contentParts)).join("\n\n").trim();
    const uniqueAttachments = Array.from(new Set(attachments));
    const timeEl = root.querySelector("time");
    return {
      id: id,
      eventKind: eventKind || "added",
      username: username,
      content: uniqueContent || (uniqueAttachments.length ? `[Attachment count: ${uniqueAttachments.length}]` : "[No text content]"),
      timestamp: (timeEl && timeEl.getAttribute("datetime")) || new Date().toISOString(),
      attachments: uniqueAttachments
    };
  }

  function signature(message) {
    return JSON.stringify({ content: message.content, attachments: message.attachments });
  }

  function remember(root) {
    const message = parseMessage(root, "baseline");
    if (message) window.__discordDomBridgeSeen[message.id] = signature(message);
  }

  window.__discordDomBridgeCollectVisibleMessages = function (limit) {
    const roots = Array.from(document.querySelectorAll('li[id^="chat-messages-"]'));
    return roots.slice(-Math.max(1, limit || 50))
      .map(function (root) { return parseMessage(root, "recovery"); })
      .filter(Boolean);
  };

  function enqueue(root, eventKind) {
    try {
      const message = parseMessage(root, eventKind);
      if (!message) return;
      const sig = signature(message);
      if (Date.now() < window.__discordDomBridgeBaselineUntil) {
        window.__discordDomBridgeSeen[message.id] = sig;
        window.__discordDomBridgeStats.ignored += 1;
        return;
      }
      if (window.__discordDomBridgeSeen[message.id] === sig) {
        window.__discordDomBridgeStats.ignored += 1;
        return;
      }
      window.__discordDomBridgeSeen[message.id] = sig;
      window.__discordDomBridgeQueue.push(message);
      if (window.__discordDomBridgeQueue.length > 500) {
        window.__discordDomBridgeQueue.splice(0, window.__discordDomBridgeQueue.length - 500);
      }
      window.__discordDomBridgeStats.queued += 1;
    } catch (e) {
      window.__discordDomBridgeStats.errors += 1;
    }
  }

  Array.from(document.querySelectorAll('li[id^="chat-messages-"]')).forEach(remember);

  const pending = [];
  let timer = null;
  function schedule(root, eventKind) {
    if (!root) return;
    pending.push({ root: root, eventKind: eventKind || "added" });
    if (timer) return;
    timer = setTimeout(function () {
      const items = pending.splice(0, pending.length);
      timer = null;
      const latestById = {};
      for (const item of items) {
        const id = item.root.getAttribute("id") || "";
        latestById[id] = item;
      }
      Object.values(latestById).forEach(function (item) {
        enqueue(item.root, item.eventKind);
      });
    }, 120);
  }

  const observer = new MutationObserver(function (mutations) {
    for (const mutation of mutations) {
      if (mutation.type === "childList") {
        for (const node of mutation.addedNodes) {
          const root = messageRoot(node);
          if (root) schedule(root, "added");
          if (node.querySelectorAll) {
            for (const item of node.querySelectorAll('li[id^="chat-messages-"]')) {
              schedule(item, "added");
            }
          }
        }
      } else if (mutation.type === "characterData") {
        schedule(messageRoot(mutation.target), "updated");
      }
    }
  });

  observer.observe(document.body, { childList: true, subtree: true, characterData: true });
  window.__discordDomBridgeObserver = observer;
  return true;
})();
"""

    def _install_dom_observer(self, channel_url: str) -> bool:
        try:
            installed = self.driver.execute_script(self._dom_observer_script())
            if installed:
                logger.info(f"Discord DOM event observer installed: {channel_url}")
                return True
        except Exception as e:
            logger.warning(f"Failed to install Discord DOM event observer: {e}")
        return False

    def _drain_dom_events(self, channel_url: str) -> List[DiscordMessage]:
        try:
            result = self.driver.execute_script(
                """
                if (!window.__discordDomBridgeInstalled) return null;

                function clean(value) {
                  return String(value || "")
                    .trim()
                    .replace(/^#/, "")
                    .replace(/^["']|["']$/g, "")
                    .trim();
                }

                function channelName() {
                  function fromHeader() {
                    const header = document.querySelector('h1[class*="title"], div[class*="titleWrapper"] h1');
                    if (!header) return "";
                    const lines = (header.innerText || header.textContent || "")
                      .split("\\n")
                      .map(clean)
                      .filter(Boolean);
                    if (lines.length >= 2) {
                      const server = lines[0].replace(/:$/, "").trim();
                      return `${server}: ${lines.slice(1).join(" ")}`;
                    }
                    return lines[0] || "";
                  }

                  const headerName = fromHeader();
                  if (headerName) return headerName;

                  const titleParts = String(document.title || "").split("|").map(part => clean(part));
                  if (titleParts.length >= 2) {
                    const channel = titleParts[1];
                    const server = titleParts[2] || "";
                    if (channel) return server ? `${server}: ${channel}` : channel;
                  }
                  return "";
                }

                function findMessageScroller() {
                  const candidates = Array.from(document.querySelectorAll('[class*="scroller"], main, [data-list-id]'))
                    .filter(el => el && el.scrollHeight > el.clientHeight + 50);
                  candidates.sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                  return candidates[0] || document.scrollingElement || document.documentElement;
                }

                function keepAtLatest() {
                  const labels = ["Jump to Present", "跳至最新", "跳到最新", "转到最新"];
                  for (const button of document.querySelectorAll('button,[role="button"]')) {
                    const text = [
                      button.getAttribute("aria-label"),
                      button.getAttribute("title"),
                      button.innerText,
                      button.textContent
                    ].join(" ");
                    if (labels.some(label => text && text.includes(label))) {
                      button.click();
                    }
                  }

                  const scroller = findMessageScroller();
                  if (!scroller) return false;
                  const distance = scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop;
                  if (distance > 240) {
                    scroller.scrollTop = scroller.scrollHeight;
                    return true;
                  }
                  return false;
                }

                const scrolledBeforeCollect = keepAtLatest();
                const queue = window.__discordDomBridgeQueue || [];
                window.__discordDomBridgeQueue = [];
                const visibleMessages = window.__discordDomBridgeCollectVisibleMessages
                  ? window.__discordDomBridgeCollectVisibleMessages(50)
                  : [];
                return {
                  messages: queue.concat(visibleMessages),
                  channelName: channelName(),
                  scrolled: scrolledBeforeCollect || keepAtLatest(),
                  bridgeStats: window.__discordDomBridgeStats || null
                };
                """
            )
        except Exception as e:
            if self._is_browser_session_lost(e):
                raise
            logger.warning(f"Failed to drain Discord DOM event queue: {e}")
            return []

        if result is None:
            self._install_dom_observer(channel_url)
            self._refresh_channel_name(channel_url)
            return []

        if isinstance(result, dict):
            raw_messages = result.get("messages") or []
            channel_name = str(result.get("channelName") or "").strip()
        else:
            raw_messages = result or []
            channel_name = ""

        if channel_name:
            previous = self.channel_names.get(channel_url)
            self.channel_names[channel_url] = channel_name
            if previous != channel_name:
                logger.info(f"Resolved Discord channel name: {channel_name} ({channel_url})")
        else:
            channel_name = self._refresh_channel_name(channel_url)

        messages = []
        batch_seen_keys = set()
        for raw in raw_messages or []:
            msg = self._message_from_dom_event(raw, channel_url, channel_name)
            if msg:
                seen_key = self._message_seen_key(msg)
                if seen_key in batch_seen_keys:
                    continue
                batch_seen_keys.add(seen_key)
                messages.append(msg)
        return messages

    @staticmethod
    def _message_seen_key(message: DiscordMessage) -> str:
        payload = "\0".join([message.content or "", *(message.attachments or [])])
        digest = hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()
        return f"{message.channel_url}:{message.id}:{digest}"

    def _mark_message_seen(self, message: DiscordMessage) -> None:
        self.seen_message_keys.add(self._message_seen_key(message))

    def _dispatch_dom_message(self, message: DiscordMessage) -> bool:
        result = self.on_new_message(message)
        if result is False:
            logger.error(
                "DOM event message send failed, keeping it eligible for visible-message retry: "
                f"channel={message.channel_url}, id={message.id}"
            )
            return False

        self._mark_message_seen(message)
        return True

    def _message_from_dom_event(self, raw: Dict, channel_url: str, channel_name: str) -> Optional[DiscordMessage]:
        message_id = str(raw.get("id") or "")
        if not message_id:
            return None
        message_id = message_id.rsplit("-", 1)[-1]

        attachments = [str(url) for url in raw.get("attachments") or [] if url]
        content = str(raw.get("content") or "").strip()
        message_content = content or (f"[Attachment count: {len(attachments)}]" if attachments else "[No text content]")
        probe_message = DiscordMessage(
            id=message_id,
            username=str(raw.get("username") or "Unknown user"),
            content=message_content,
            timestamp=datetime.now(timezone.utc),
            channel_url=channel_url,
            attachments=attachments,
            channel_name=channel_name,
        )
        if self._message_seen_key(probe_message) in self.seen_message_keys:
            return None

        timestamp = probe_message.timestamp
        timestamp_raw = raw.get("timestamp")
        if timestamp_raw:
            try:
                timestamp = datetime.fromisoformat(str(timestamp_raw).replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        if self.dom_forward_after_utc and timestamp_raw and timestamp < self.dom_forward_after_utc:
            event_kind = str(raw.get("eventKind") or "added")
            in_quarantine = time.monotonic() < self.dom_startup_quarantine_until
            if event_kind != "updated" or in_quarantine:
                self.dom_ignored_old_message_ids.add(message_id)
                return None

        return DiscordMessage(
            id=message_id,
            username=probe_message.username,
            content=message_content,
            timestamp=timestamp,
            channel_url=channel_url,
            attachments=attachments,
            channel_name=channel_name,
        )

    def _monitor_messages_dom_queue(self):
        channel_errors = {url: 0 for url in self.channel_urls}
        max_errors = 5
        if self.dom_forward_after_utc is None:
            grace_seconds = max(2, float(self.check_interval or 1) * 2)
            recovery_seconds = max(grace_seconds, self._dom_recovery_lookback_seconds())
            self.dom_forward_after_utc = datetime.now(timezone.utc) - timedelta(seconds=recovery_seconds)
            self.dom_startup_quarantine_until = time.monotonic() + 60
            logger.info(
                "DOM event monitor will recover recent messages and ignore older added messages before "
                f"{self.dom_forward_after_utc.isoformat()}"
            )

        while True:
            try:
                self._reconcile_channel_tabs()
            except Exception as e:
                if self._is_browser_session_lost(e):
                    self._recover_browser_session(str(e).splitlines()[0])
                    channel_errors = {url: 0 for url in self.channel_urls}
                    continue
                logger.warning(f"频道标签页巡检失败: {e}")

            for channel_idx, channel_url in enumerate(self.channel_urls):
                try:
                    if not self.switch_to_channel(channel_url):
                        if self._is_browser_session_lost(self._last_switch_error):
                            self._recover_browser_session(str(self._last_switch_error).splitlines()[0])
                            channel_errors = {url: 0 for url in self.channel_urls}
                            break
                        raise Exception("Unable to switch to channel tab")

                    new_messages = self._drain_dom_events(channel_url)
                    if new_messages:
                        logger.info(f"DOM event queue channel [{channel_idx + 1}/{len(self.channel_urls)}] found {len(new_messages)} messages")
                        for idx, msg_obj in enumerate(new_messages, 1):
                            logger.info(f"\nDOM event message [{idx}/{len(new_messages)}]:")
                            logger.info(f"   User: {msg_obj.username}")
                            logger.info(f"   Content: {msg_obj.content[:50]}...")
                            if self._dispatch_dom_message(msg_obj):
                                self.last_message_ids[channel_url] = msg_obj.id

                    channel_errors[channel_url] = 0
                except Exception as e:
                    if self._is_browser_session_lost(e):
                        self._recover_browser_session(str(e).splitlines()[0])
                        channel_errors = {url: 0 for url in self.channel_urls}
                        break
                    channel_errors[channel_url] += 1
                    logger.error(
                        f"DOM event monitor channel [{channel_idx + 1}] error "
                        f"({channel_errors[channel_url]}/{max_errors}): {e}"
                    )
                    if channel_errors[channel_url] >= max_errors:
                        try:
                            self.driver.refresh()
                            time.sleep(5)
                            self._install_dom_observer(channel_url)
                            channel_errors[channel_url] = 0
                        except Exception:
                            if channel_url in self.channel_handles:
                                del self.channel_handles[channel_url]
                            channel_errors[channel_url] = 0

            time.sleep(max(0.1, float(self.check_interval or 0.5)))

    @staticmethod
    def _dom_recovery_lookback_seconds() -> float:
        try:
            return max(0.0, float(os.getenv("DISCORD_DOM_RECOVERY_LOOKBACK_SECONDS", "7200")))
        except ValueError:
            return 7200.0

    def monitor_messages(self):
        logger.info("Using Discord DOM event observer queues for browser-tabs mode")
        self._monitor_messages_dom_queue()
        return
        """监控Discord消息"""
        logger.info("✅ 所有准备工作已完成，开始监控消息...")
        logger.info(f"💡 正在监控 {len(self.channel_urls)} 个频道")
        
        # 为每个频道维护独立的错误计数器
        channel_errors = {url: 0 for url in self.channel_urls}
        max_errors = 5
        
        while True:
            for channel_idx, channel_url in enumerate(self.channel_urls):
                try:
                    if not self.switch_to_channel(channel_url):
                        # 主动抛出异常，以便触发下方的错误计数和恢复逻辑
                        raise Exception("无法切换到频道标签页 (Switch failed)")
                    
                    WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, 'li[id^="chat-messages-"]'))
                    )
                    
                    messages = self.driver.find_elements(
                        By.CSS_SELECTOR,
                        'li[id^="chat-messages-"]'
                    )
                    
                    if messages:
                        new_messages = []
                        found_last = False
                        last_message_id = self.last_message_ids[channel_url]
                        
                        if last_message_id is None:
                            new_messages = [messages[-1]]
                            logger.info(f"🎬 频道 [{channel_idx + 1}/{len(self.channel_urls)}] 首次运行，从最新消息开始监控")
                        else:
                            for message in messages:
                                msg_id = message.get_attribute('id')
                                if msg_id == last_message_id:
                                    found_last = True
                                    continue
                                if found_last:
                                    new_messages.append(message)
                            
                            if not found_last and len(new_messages) == 0:
                                last_msg_id = messages[-1].get_attribute('id')
                                if last_msg_id != last_message_id:
                                    new_messages = [messages[-1]]
                                    logger.info(f"⚠️  频道 [{channel_idx + 1}] 未找到上次消息记录，可能页面已刷新")
                        
                        if new_messages:
                            if len(new_messages) > 1:
                                logger.info(f"📬 频道 [{channel_idx + 1}] 发现 {len(new_messages)} 条新消息，依次处理中...")
                            
                            for idx, message_element in enumerate(new_messages, 1):
                                # 确保元素可见以便提取信息（Parser内部不再处理滚动，交由Parser调用前确保可见？
                                # 或者保留滚动逻辑在这里，或者在Parser里做。
                                # 最佳实践：Listener负责交互(滚动)，Parser负责提取。
                                try:
                                    self.driver.execute_script(
                                        "arguments[0].scrollIntoView({block: 'nearest'});",
                                        message_element
                                    )
                                    time.sleep(0.05)
                                except Exception:
                                    pass

                                # 提取消息并构建 DiscordMessage 对象
                                channel_name = self.get_channel_name(channel_url)
                                msg_obj = DiscordParser.parse_message(message_element, channel_url, channel_name)
                                
                                if msg_obj:
                                    if len(new_messages) > 1:
                                        logger.info(f"\n📨 频道 [{channel_idx + 1}] 新消息 [{idx}/{len(new_messages)}]:")
                                    else:
                                        logger.info(f"\n📨 频道 [{channel_idx + 1}] 新消息:")
                                    logger.info(f"   用户: {msg_obj.username}")
                                    logger.info(f"   内容: {msg_obj.content[:50]}...")
                                    
                                    # 回调
                                    if self._dispatch_dom_message(msg_obj):
                                        self.last_message_ids[channel_url] = msg_obj.id
                                    
                                    if len(new_messages) > 1 and idx < len(new_messages):
                                        time.sleep(0.5)
                    
                    # 成功执行，重置该频道的错误计数
                    channel_errors[channel_url] = 0
                    
                except Exception as e:
                    channel_errors[channel_url] += 1
                    current_errors = channel_errors[channel_url]
                    logger.error(f"⚠️  频道 [{channel_idx + 1}] 监控错误 ({current_errors}/{max_errors}): {e}")
                    
                    if current_errors >= max_errors:
                        logger.warning(f"❌ 频道 [{channel_idx + 1}] 错误次数过多，尝试重新加载页面...")
                        try:
                            self.driver.refresh()
                            time.sleep(5)
                            channel_errors[channel_url] = 0
                        except Exception as refresh_error:
                            logger.error(f"页面刷新失败，可能是标签页崩溃: {refresh_error}")
                            
                            # 检查浏览器是否完全崩溃/关闭
                            is_fatal = False
                            try:
                                if not self.driver.window_handles:
                                    is_fatal = True
                            except Exception:
                                is_fatal = True
                            
                            if is_fatal:
                                logger.error("🔥 检测到浏览器已关闭或崩溃，正在重启...")
                                self.restart_browser()
                                break # 跳出 for 循环，重新开始 while 循环

                            logger.info("♻️ 尝试移除失效句柄，下次将重新打开该频道...")
                            
                            # 移除失效句柄，触发重新打开逻辑
                            if channel_url in self.channel_handles:
                                del self.channel_handles[channel_url]
                            
                            # 尝试关闭崩溃的标签页
                            try:
                                self.driver.close()
                            except:
                                pass
                                
                            # 重置错误计数
                            channel_errors[channel_url] = 0
                            
                            # 尝试切回第一个可用窗口
                            try:
                                if len(self.driver.window_handles) > 0:
                                    self.driver.switch_to.window(self.driver.window_handles[0])
                            except:
                                pass
                    
                    time.sleep(5)
            
            time.sleep(self.check_interval)
    
    def cleanup(self):
        """清理资源"""
        if self.browser_manager:
            self.browser_manager.cleanup()
