#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Async message sender wrapper.

The listener can enqueue messages quickly while worker threads do the actual
delivery. By default send_message waits for the worker result, so callers can
mark a Discord message as processed only after real delivery succeeds.
"""

import queue
import threading
import time
from typing import Any, Optional

from .base import MessageSender
from src.core.models import DiscordMessage
from src.utils.logger import get_logger

logger = get_logger(__name__)


class AsyncMessageSender(MessageSender):
    """Run the real sender in background worker threads."""

    def __init__(
        self,
        sender: MessageSender,
        workers: int = 1,
        queue_size: int = 1000,
        confirm_timeout: float = 30.0,
    ):
        super().__init__()
        self.sender = sender
        self.workers = max(1, int(workers or 1))
        self.queue_size = max(1, int(queue_size or 1000))
        self.confirm_timeout = max(0.0, float(confirm_timeout or 0.0))
        self.queue: "queue.Queue[Optional[Any]]" = queue.Queue(maxsize=self.queue_size)
        self.threads = []
        self.started = False

    def login(self) -> bool:
        if not self.sender.login():
            return False

        self._start_workers()
        self.is_ready = True
        logger.info(
            "Async sender queue enabled: "
            f"workers={self.workers}, queue_size={self.queue_size}, "
            f"confirm_timeout={self.confirm_timeout}s"
        )
        return True

    def send_message(self, message: DiscordMessage) -> bool:
        if not self.is_ready:
            logger.warning("Async sender is not ready, skipping message")
            return False

        try:
            if self.confirm_timeout <= 0:
                self.queue.put_nowait(message)
                return True

            ack = threading.Event()
            result = {"ok": False}
            self.queue.put_nowait((message, ack, result))
            if not ack.wait(self.confirm_timeout):
                logger.error(f"Async sender delivery confirmation timed out: message_id={message.id}")
                return False
            return bool(result.get("ok"))
        except queue.Full:
            logger.error("Async sender queue is full, dropping message")
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
            item = self.queue.get()
            ack = None
            result = None
            try:
                if item is None:
                    return

                if isinstance(item, tuple) and len(item) == 3:
                    message, ack, result = item
                else:
                    message = item

                sent = False
                max_attempts = 3
                for attempt in range(1, max_attempts + 1):
                    try:
                        if self.sender.send_message(message):
                            sent = True
                            break
                    except Exception as e:
                        logger.error(f"Async sender worker error: {e}", exc_info=True)

                    if attempt < max_attempts:
                        time.sleep(min(5, attempt))

                if not sent:
                    logger.error(
                        "Async sender failed after retries: "
                        f"message_id={getattr(message, 'id', '')}"
                    )

                if result is not None:
                    result["ok"] = sent
            except Exception as e:
                logger.error(f"Async sender worker loop error: {e}", exc_info=True)
            finally:
                if ack is not None:
                    ack.set()
                self.queue.task_done()
