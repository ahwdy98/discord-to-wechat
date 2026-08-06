#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
异步消息发送器包装器。

监听线程只负责把消息放入队列，后台 worker 负责实际发送，避免 HTTP/机器人
发送耗时阻塞 Discord 频道轮询。
"""

import queue
import threading
import time
from typing import Optional

from .base import MessageSender
from src.core.models import DiscordMessage
from src.utils.logger import get_logger

logger = get_logger(__name__)


class AsyncMessageSender(MessageSender):
    """用后台线程异步执行真实发送器"""

    def __init__(self, sender: MessageSender, workers: int = 1, queue_size: int = 1000):
        super().__init__()
        self.sender = sender
        self.workers = max(1, int(workers or 1))
        self.queue_size = max(1, int(queue_size or 1000))
        self.queue: "queue.Queue[Optional[DiscordMessage]]" = queue.Queue(maxsize=self.queue_size)
        self.threads = []
        self.started = False

    def login(self) -> bool:
        if not self.sender.login():
            return False

        self._start_workers()
        self.is_ready = True
        logger.info(f"异步发送队列已启用: workers={self.workers}, queue_size={self.queue_size}")
        return True

    def send_message(self, message: DiscordMessage) -> bool:
        if not self.is_ready:
            logger.warning("异步发送器未就绪，跳过发送")
            return False

        try:
            self.queue.put_nowait(message)
            return True
        except queue.Full:
            logger.error("异步发送队列已满，丢弃消息")
            return False

    def keep_alive(self):
        self.sender.keep_alive()

    def cleanup(self):
        if self.started:
            for _ in self.threads:
                try:
                    self.queue.put_nowait(None)
                except queue.Full:
                    pass

            for thread in self.threads:
                thread.join(timeout=5)

        self.sender.cleanup()

    def _start_workers(self):
        if self.started:
            return

        for index in range(self.workers):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"async-sender-{index + 1}",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)

        self.started = True

    def _worker_loop(self):
        while True:
            message = self.queue.get()
            try:
                if message is None:
                    return

                max_attempts = 3
                for attempt in range(1, max_attempts + 1):
                    try:
                        if self.sender.send_message(message):
                            break
                    except Exception as e:
                        logger.error(f"寮傛鍙戦€佹秷鎭紓甯? {e}", exc_info=True)

                    if attempt < max_attempts:
                        time.sleep(min(5, attempt))
                    else:
                        logger.error(
                            "寮傛鍙戦€佹秷鎭け璐ワ紝宸茶揪鏈€澶ч噸璇曟鏁? "
                            f"message_id={getattr(message, 'id', '')}"
                        )
            except Exception as e:
                logger.error(f"异步发送消息异常: {e}", exc_info=True)
            finally:
                self.queue.task_done()
