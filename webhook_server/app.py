#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Discord message webhook receiver.

Stores incoming messages in SQLite, exposes a small web UI and JSON API.
Only Python standard library modules are used so the service is easy to run.
"""

import html
import json
import os
import base64
import hashlib
import hmac
import queue
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("WEBHOOK_DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.getenv("WEBHOOK_DB_PATH", DATA_DIR / "messages.sqlite3"))
HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()
FORWARD_ROUTES_JSON = os.getenv("FORWARD_ROUTES_JSON", "").strip()
FORWARD_WORKERS = int(os.getenv("FORWARD_WORKERS", "2"))
FORWARD_TIMEOUT = int(os.getenv("FORWARD_TIMEOUT", "30"))
FORWARD_QUEUE: "queue.Queue[int]" = queue.Queue()
FORWARD_ROUTES: List[Dict[str, Any]] = []
WEBHOOK_TIMEZONE = os.getenv("WEBHOOK_TIMEZONE", "Asia/Shanghai")


def load_display_timezone():
    if ZoneInfo:
        try:
            return ZoneInfo(WEBHOOK_TIMEZONE)
        except Exception:
            print(f"Invalid WEBHOOK_TIMEZONE={WEBHOOK_TIMEZONE!r}, fallback to Asia/Shanghai", file=sys.stderr)
    return timezone(timedelta(hours=8))


DISPLAY_TIMEZONE = load_display_timezone()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DISPLAY_TIMEZONE)
    return parsed


def format_display_time(value: Any) -> str:
    parsed = parse_datetime(value)
    if not parsed:
        return str(value or "")
    return parsed.astimezone(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def message_time(message: Dict[str, Any]) -> str:
    return (
        message.get("timestamp_local")
        or format_display_time(message.get("timestamp"))
        or message.get("timestamp")
        or message.get("created_at_local")
        or format_display_time(message.get("created_at"))
        or message.get("created_at")
        or ""
    )


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_message_id TEXT NOT NULL,
                username TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT,
                channel_url TEXT NOT NULL,
                channel_name TEXT,
                attachments_json TEXT NOT NULL DEFAULT '[]',
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(discord_message_id, channel_url)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel_url ON messages(channel_url)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_username ON messages(username)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS forward_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                route_index INTEGER NOT NULL,
                target_index INTEGER NOT NULL,
                target_type TEXT NOT NULL,
                target_name TEXT NOT NULL,
                target_config_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                response_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT,
                UNIQUE(message_id, route_index, target_index)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_forward_message_id ON forward_deliveries(message_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_forward_status ON forward_deliveries(status)")


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_message(payload: Dict[str, Any]) -> Dict[str, Any]:
    attachments = payload.get("attachments") or []
    if not isinstance(attachments, list):
        attachments = []

    message_id = str(payload.get("id") or payload.get("discord_message_id") or "").strip()
    channel_url = str(payload.get("channel_url") or "").strip()

    if not message_id:
        raise ValueError("missing field: id")
    if not channel_url:
        raise ValueError("missing field: channel_url")

    return {
        "discord_message_id": message_id,
        "username": str(payload.get("username") or "未知用户"),
        "content": str(payload.get("content") or ""),
        "timestamp": str(payload.get("timestamp") or ""),
        "channel_url": channel_url,
        "channel_name": str(payload.get("channel_name") or ""),
        "attachments_json": json.dumps(attachments, ensure_ascii=False),
        "raw_json": json.dumps(payload, ensure_ascii=False),
        "created_at": utc_now(),
    }


def insert_message(payload: Dict[str, Any]) -> Tuple[int, bool]:
    data = normalize_message(payload)
    with db_connect() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO messages (
                discord_message_id, username, content, timestamp,
                channel_url, channel_name, attachments_json, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["discord_message_id"],
                data["username"],
                data["content"],
                data["timestamp"],
                data["channel_url"],
                data["channel_name"],
                data["attachments_json"],
                data["raw_json"],
                data["created_at"],
            ),
        )

        inserted = cursor.rowcount > 0
        if inserted:
            return int(cursor.lastrowid), True

        existing = conn.execute(
            """
            SELECT id FROM messages
            WHERE discord_message_id = ? AND channel_url = ?
            """,
            (data["discord_message_id"], data["channel_url"]),
        ).fetchone()
        return int(existing["id"]), False


def load_forward_routes() -> List[Dict[str, Any]]:
    if not FORWARD_ROUTES_JSON:
        return []

    try:
        routes = json.loads(FORWARD_ROUTES_JSON)
    except json.JSONDecodeError as e:
        print(f"Invalid FORWARD_ROUTES_JSON: {e}", file=sys.stderr)
        return []

    if not isinstance(routes, list):
        print("Invalid FORWARD_ROUTES_JSON: root value must be a list", file=sys.stderr)
        return []

    normalized_routes = []
    for index, route in enumerate(routes):
        if not isinstance(route, dict):
            print(f"Ignore invalid forward route #{index}: not an object", file=sys.stderr)
            continue

        targets = route.get("targets") or []
        if not isinstance(targets, list) or not targets:
            print(f"Ignore invalid forward route #{index}: missing targets", file=sys.stderr)
            continue

        normalized_routes.append(route)
    return normalized_routes


def create_forward_deliveries(message_id: int, message: Dict[str, Any]) -> List[int]:
    delivery_ids = []
    now = utc_now()

    for route_index, route in enumerate(FORWARD_ROUTES):
        if not route_matches_message(route, message):
            continue

        targets = route.get("targets") or []
        with db_connect() as conn:
            for target_index, target in enumerate(targets):
                if not isinstance(target, dict):
                    continue

                target_type = str(target.get("type") or target.get("sender_type") or "").strip()
                if target_type not in ["feishu", "enterprise_wechat"]:
                    continue

                target_name = str(target.get("name") or f"{target_type}:{route_index}:{target_index}")
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO forward_deliveries (
                        message_id, route_index, target_index, target_type, target_name,
                        target_config_json, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        message_id,
                        route_index,
                        target_index,
                        target_type,
                        target_name,
                        json.dumps(target, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                if cursor.rowcount > 0:
                    delivery_ids.append(int(cursor.lastrowid))

    for delivery_id in delivery_ids:
        FORWARD_QUEUE.put(delivery_id)

    return delivery_ids


def route_matches_message(route: Dict[str, Any], message: Dict[str, Any]) -> bool:
    channels = route.get("channels")
    if isinstance(channels, str):
        channels = [channels]
    if channels is None and route.get("channel"):
        channels = [route.get("channel")]

    if not channels:
        return True

    message_channel = normalize_channel(message.get("channel_url") or "")
    return any(normalize_channel(channel) == message_channel for channel in channels)


def normalize_channel(channel_url: str) -> str:
    return str(channel_url or "").rstrip("/")


def start_forward_workers() -> None:
    if not FORWARD_ROUTES:
        return

    for index in range(max(1, FORWARD_WORKERS)):
        thread = threading.Thread(target=forward_worker_loop, name=f"forward-worker-{index + 1}", daemon=True)
        thread.start()

    enqueue_pending_deliveries()
    print(f"Forward routes enabled: {len(FORWARD_ROUTES)}, workers={max(1, FORWARD_WORKERS)}")


def enqueue_pending_deliveries() -> None:
    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT id FROM forward_deliveries
            WHERE status IN ('pending', 'failed')
            ORDER BY id ASC
            LIMIT 500
            """
        ).fetchall()

    for row in rows:
        FORWARD_QUEUE.put(int(row["id"]))


def forward_worker_loop() -> None:
    while True:
        delivery_id = FORWARD_QUEUE.get()
        try:
            process_forward_delivery(delivery_id)
        except Exception as e:
            print(f"Forward worker error for delivery {delivery_id}: {e}", file=sys.stderr)
        finally:
            FORWARD_QUEUE.task_done()


def process_forward_delivery(delivery_id: int) -> None:
    delivery, message = get_forward_delivery_context(delivery_id)
    if not delivery or not message:
        return

    if delivery["status"] == "sent":
        return

    target_config = json.loads(delivery["target_config_json"] or "{}")
    mark_delivery_status(delivery_id, "sending")

    try:
        if delivery["target_type"] == "feishu":
            response = send_to_feishu(target_config, message)
        elif delivery["target_type"] == "enterprise_wechat":
            response = send_to_enterprise_wechat(target_config, message)
        else:
            raise ValueError(f"unsupported target type: {delivery['target_type']}")

        mark_delivery_status(delivery_id, "sent", response=response, sent=True)
    except Exception as e:
        mark_delivery_status(delivery_id, "failed", error=str(e))


def get_forward_delivery_context(delivery_id: int) -> Tuple[Optional[sqlite3.Row], Optional[Dict[str, Any]]]:
    with db_connect() as conn:
        delivery = conn.execute("SELECT * FROM forward_deliveries WHERE id = ?", (delivery_id,)).fetchone()
        if not delivery:
            return None, None
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (delivery["message_id"],)).fetchone()

    return delivery, row_to_message(row) if row else None


def mark_delivery_status(
    delivery_id: int,
    status: str,
    error: str = "",
    response: Optional[Dict[str, Any]] = None,
    sent: bool = False,
) -> None:
    now = utc_now()
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE forward_deliveries
            SET status = ?,
                attempts = attempts + CASE WHEN ? IN ('sent', 'failed') THEN 1 ELSE 0 END,
                last_error = ?,
                response_json = ?,
                updated_at = ?,
                sent_at = CASE WHEN ? THEN ? ELSE sent_at END
            WHERE id = ?
            """,
            (
                status,
                status,
                error or None,
                json.dumps(response, ensure_ascii=False) if response is not None else None,
                now,
                1 if sent else 0,
                now,
                delivery_id,
            ),
        )


def send_to_feishu(target: Dict[str, Any], message: Dict[str, Any]) -> Dict[str, Any]:
    webhook = str(target.get("webhook") or target.get("hook") or "").strip()
    if not webhook:
        raise ValueError("missing feishu webhook")

    payload = build_feishu_payload(message)
    secret = str(target.get("secret") or "").strip()
    if secret:
        payload.update(build_feishu_signature(secret))

    return post_json(webhook, payload)


def build_feishu_payload(message: Dict[str, Any]) -> Dict[str, Any]:
    rows = [
        [{"tag": "text", "text": f"来自 {message.get('username') or '未知用户'} 的消息"}],
    ]
    if message.get("channel_name"):
        rows.append([{"tag": "text", "text": f"频道: {message.get('channel_name')}"}])
    display_time = message_time(message)
    if display_time:
        rows.append([{"tag": "text", "text": f"时间: {display_time}"}])
    rows.append([{"tag": "text", "text": "----------------"}])

    for line in split_content_lines(message.get("content") or "", len(message.get("attachments") or [])):
        rows.append([{"tag": "text", "text": line}])

    attachments = message.get("attachments") or []
    if attachments:
        rows.append([{"tag": "text", "text": f"附件({len(attachments)}):"}])
        for index, attachment in enumerate(attachments[:5], 1):
            rows.append(
                [
                    {"tag": "text", "text": f"{index}. "},
                    {"tag": "a", "text": attachment_label(str(attachment), index), "href": str(attachment)},
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


def build_feishu_signature(secret: str) -> Dict[str, str]:
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return {
        "timestamp": timestamp,
        "sign": base64.b64encode(digest).decode("utf-8"),
    }


def send_to_enterprise_wechat(target: Dict[str, Any], message: Dict[str, Any]) -> Dict[str, Any]:
    webhook = str(target.get("webhook") or target.get("hook") or "").strip()
    if not webhook:
        raise ValueError("missing enterprise_wechat webhook")

    content = build_enterprise_wechat_markdown(message)
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "content": content,
        },
    }
    return post_json(webhook, payload)


def build_enterprise_wechat_markdown(message: Dict[str, Any]) -> str:
    content = f"来自 **{message.get('username') or '未知用户'}** 的消息\n"
    if message.get("channel_name"):
        content += f"> 频道: {message.get('channel_name')}\n"
    display_time = message_time(message)
    if display_time:
        content += f"> 时间: {display_time}\n\n"

    lines = split_content_lines(message.get("content") or "", len(message.get("attachments") or []))
    content += "\n".join(lines) if lines else ""

    attachments = message.get("attachments") or []
    if attachments:
        content += f"\n\n**附件({len(attachments)}):**\n"
        for index, attachment in enumerate(attachments[:5], 1):
            label = attachment_label(str(attachment), index)
            content += f"{index}. [{label}]({attachment})\n"

    return content


def split_content_lines(content: str, attachment_count: int = 0) -> List[str]:
    placeholder = f"[附件 {attachment_count} 个]"
    if attachment_count and (content or "").strip() == placeholder:
        return []

    lines = [line.strip() for line in (content or "").splitlines()]
    lines = [line for line in lines if line]
    return lines or ([] if attachment_count else ["[空消息]"])


def post_json(url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=FORWARD_TIMEOUT) as response:
            response_body = response.read().decode("utf-8")
    except HTTPError as e:
        response_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {response_body[:300]}") from e
    except URLError as e:
        raise RuntimeError(f"network error: {e}") from e

    try:
        result = json.loads(response_body or "{}")
    except json.JSONDecodeError:
        result = {"raw": response_body}

    if not is_success_response(result):
        raise RuntimeError(f"target returned failure: {result}")

    return result


def is_success_response(result: Dict[str, Any]) -> bool:
    return result.get("code") == 0 or result.get("StatusCode") == 0 or result.get("errcode") == 0


def row_to_message(row: sqlite3.Row) -> Dict[str, Any]:
    message = dict(row)
    try:
        message["attachments"] = json.loads(message.pop("attachments_json") or "[]")
    except json.JSONDecodeError:
        message["attachments"] = []
    try:
        message["raw"] = json.loads(message.pop("raw_json") or "{}")
    except json.JSONDecodeError:
        message["raw"] = {}
    message["timestamp_local"] = format_display_time(message.get("timestamp"))
    message["created_at_local"] = format_display_time(message.get("created_at"))
    return message


def query_messages(params: Dict[str, List[str]]) -> Dict[str, Any]:
    limit = clamp_int(first_param(params, "limit", "50"), 1, 200, 50)
    offset = clamp_int(first_param(params, "offset", "0"), 0, 1000000, 0)
    q = first_param(params, "q", "").strip()
    username = first_param(params, "username", "").strip()
    channel_url = first_param(params, "channel_url", "").strip()
    after_id = clamp_int(first_param(params, "after_id", "0"), 0, 1000000000, 0)

    where = []
    values: List[Any] = []
    if q:
        where.append("(content LIKE ? OR username LIKE ? OR channel_name LIKE ?)")
        like = f"%{q}%"
        values.extend([like, like, like])
    if username:
        where.append("username = ?")
        values.append(username)
    if channel_url:
        where.append("channel_url = ?")
        values.append(channel_url)
    if after_id:
        where.append("id > ?")
        values.append(after_id)

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    with db_connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM messages {where_sql}", values).fetchone()["c"]
        rows = conn.execute(
            f"""
            SELECT * FROM messages
            {where_sql}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            [*values, limit, offset],
        ).fetchall()

    messages = [row_to_message(row) for row in rows]
    attach_forward_statuses(messages)

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "messages": messages,
    }


def get_message(message_id: int) -> Optional[Dict[str, Any]]:
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if not row:
        return None

    message = row_to_message(row)
    attach_forward_statuses([message])
    return message


def attach_forward_statuses(messages: List[Dict[str, Any]]) -> None:
    if not messages:
        return

    message_ids = [message["id"] for message in messages]
    placeholders = ",".join(["?"] * len(message_ids))
    with db_connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, message_id, target_type, target_name, status, attempts,
                   last_error, response_json, created_at, updated_at, sent_at
            FROM forward_deliveries
            WHERE message_id IN ({placeholders})
            ORDER BY route_index ASC, target_index ASC
            """,
            message_ids,
        ).fetchall()

    by_message: Dict[int, List[Dict[str, Any]]] = {message_id: [] for message_id in message_ids}
    for row in rows:
        delivery = dict(row)
        try:
            delivery["response"] = json.loads(delivery.pop("response_json") or "{}")
        except json.JSONDecodeError:
            delivery["response"] = {}
        delivery["created_at_local"] = format_display_time(delivery.get("created_at"))
        delivery["updated_at_local"] = format_display_time(delivery.get("updated_at"))
        delivery["sent_at_local"] = format_display_time(delivery.get("sent_at"))
        by_message.setdefault(delivery["message_id"], []).append(delivery)

    for message in messages:
        message["forwards"] = by_message.get(message["id"], [])


def first_param(params: Dict[str, List[str]], key: str, default: str) -> str:
    values = params.get(key)
    return values[0] if values else default


def clamp_int(value: str, minimum: int, maximum: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "DiscordWebhookServer/1.0"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/health" or parsed.path == "/api/health":
            self.send_json({"ok": True, "db_path": str(DB_PATH), "forward_routes": len(FORWARD_ROUTES)})
            return

        if not self.authorized(params):
            self.send_error_json(HTTPStatus.UNAUTHORIZED, "unauthorized")
            return

        if parsed.path == "/" or parsed.path == "/messages":
            self.send_html(render_messages_page(query_messages(params), params))
            return

        if parsed.path == "/api/messages":
            self.send_json(query_messages(params))
            return

        if parsed.path.startswith("/api/messages/"):
            try:
                message_id = int(parsed.path.rsplit("/", 1)[-1])
            except ValueError:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid message id")
                return

            message = get_message(message_id)
            if not message:
                self.send_error_json(HTTPStatus.NOT_FOUND, "message not found")
                return
            self.send_json(message)
            return

        self.send_error_json(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path not in ["/webhook/messages", "/api/messages"]:
            self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return

        if not self.authorized(params):
            self.send_error_json(HTTPStatus.UNAUTHORIZED, "unauthorized")
            return

        try:
            payload = self.read_json_body()
            message_id, inserted = insert_message(payload)
            forward_ids = []
            if inserted:
                message = get_message(message_id)
                if message:
                    forward_ids = create_forward_deliveries(message_id, message)
        except ValueError as e:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(e))
            return
        except Exception as e:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))
            return

        status = HTTPStatus.CREATED if inserted else HTTPStatus.OK
        self.send_json(
            {
                "ok": True,
                "id": message_id,
                "inserted": inserted,
                "forward_count": len(forward_ids),
                "forward_ids": forward_ids,
            },
            status=status,
        )

    def read_json_body(self) -> Dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid json: {e}") from e
        if not isinstance(payload, dict):
            raise ValueError("json body must be an object")
        return payload

    def authorized(self, params: Dict[str, List[str]]) -> bool:
        if not TOKEN:
            return True

        auth_header = self.headers.get("Authorization", "")
        if auth_header == f"Bearer {TOKEN}":
            return True
        if self.headers.get("X-Webhook-Token", "") == TOKEN:
            return True
        if first_param(params, "token", "") == TOKEN:
            return True
        return False

    def send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"ok": False, "error": message}, status=status)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def render_messages_page(result: Dict[str, Any], params: Dict[str, List[str]]) -> str:
    q = html.escape(first_param(params, "q", ""))
    token = html.escape(first_param(params, "token", ""))
    token_input = f'<input type="hidden" name="token" value="{token}">' if token else ""
    cards = "\n".join(render_message_card(message) for message in result["messages"])
    if not cards:
        cards = '<div class="empty">暂无消息</div>'

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Discord Webhook Messages</title>
  <style>
    :root {{ color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: #f6f7f9; color: #20242a; }}
    header {{ position: sticky; top: 0; background: #ffffff; border-bottom: 1px solid #dde1e7; padding: 14px 22px; z-index: 1; }}
    h1 {{ margin: 0 0 10px; font-size: 20px; }}
    form {{ display: flex; gap: 8px; max-width: 760px; }}
    input {{ flex: 1; padding: 9px 11px; border: 1px solid #c8ced8; border-radius: 6px; font-size: 14px; }}
    button {{ padding: 9px 14px; border: 1px solid #1f6feb; background: #1f6feb; color: white; border-radius: 6px; cursor: pointer; }}
    main {{ max-width: 980px; margin: 18px auto; padding: 0 16px 36px; }}
    .summary {{ color: #5a6472; margin-bottom: 12px; }}
    .message {{ background: #ffffff; border: 1px solid #dde1e7; border-radius: 8px; padding: 14px 16px; margin-bottom: 12px; }}
    .meta {{ color: #5a6472; font-size: 13px; margin-bottom: 8px; display: flex; gap: 10px; flex-wrap: wrap; }}
    .content {{ white-space: pre-wrap; line-height: 1.55; }}
    .attachments {{ margin-top: 10px; padding-top: 10px; border-top: 1px solid #edf0f4; }}
    .attachments a {{ display: inline-block; margin: 3px 8px 3px 0; color: #1f6feb; }}
    .forwards {{ margin-top: 10px; padding-top: 10px; border-top: 1px solid #edf0f4; font-size: 13px; }}
    .forward {{ display: inline-block; margin: 3px 8px 3px 0; padding: 3px 7px; border-radius: 999px; background: #edf0f4; color: #3d4652; }}
    .forward.sent {{ background: #e6f4ea; color: #1f7a3f; }}
    .forward.failed {{ background: #fde8e8; color: #b42318; }}
    .forward.pending, .forward.sending {{ background: #fff4cc; color: #8a5a00; }}
    .empty {{ background: #ffffff; border: 1px dashed #b7c0cc; border-radius: 8px; padding: 30px; text-align: center; color: #697386; }}
  </style>
</head>
<body>
  <header>
    <h1>Discord Webhook Messages</h1>
    <form method="get" action="/messages">
      {token_input}
      <input name="q" value="{q}" placeholder="搜索内容、用户或频道">
      <button type="submit">搜索</button>
    </form>
  </header>
  <main>
    <div class="summary">共 {result["total"]} 条，当前显示 {len(result["messages"])} 条</div>
    {cards}
  </main>
</body>
</html>"""


def render_message_card(message: Dict[str, Any]) -> str:
    attachments = message.get("attachments") or []
    attachment_html = ""
    if attachments:
        links = []
        for index, url in enumerate(attachments[:10], 1):
            label = html.escape(attachment_label(str(url), index))
            safe_url = html.escape(str(url), quote=True)
            links.append(f'<a href="{safe_url}" target="_blank" rel="noreferrer">{label}</a>')
        attachment_html = f'<div class="attachments">{"".join(links)}</div>'

    forward_html = ""
    forwards = message.get("forwards") or []
    if forwards:
        items = []
        for forward in forwards:
            status = html.escape(forward.get("status") or "")
            name = html.escape(forward.get("target_name") or forward.get("target_type") or "")
            title = html.escape(forward.get("last_error") or "")
            items.append(f'<span class="forward {status}" title="{title}">{name}: {status}</span>')
        forward_html = f'<div class="forwards">{"".join(items)}</div>'

    content = html.escape(message.get("content") or "")
    username = html.escape(message.get("username") or "")
    channel = html.escape(message.get("channel_name") or "")
    timestamp = html.escape(message_time(message))
    created_at = html.escape(message.get("created_at_local") or message.get("created_at") or "")
    return f"""
    <article class="message">
      <div class="meta">
        <span>#{message.get("id")}</span>
        <strong>{username}</strong>
        <span>{channel}</span>
        <span>{timestamp or created_at}</span>
      </div>
      <div class="content">{content}</div>
      {attachment_html}
      {forward_html}
    </article>
    """


def attachment_label(url: str, index: int) -> str:
    path = unquote(urlparse(url).path)
    filename = path.rsplit("/", 1)[-1]
    lower = filename.lower()
    if any(lower.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"]):
        return f"图片 {index}"
    if any(lower.endswith(ext) for ext in [".mp4", ".mov", ".webm"]):
        return f"视频 {index}"
    return filename or f"附件 {index}"


def main() -> None:
    global FORWARD_ROUTES
    init_db()
    FORWARD_ROUTES = load_forward_routes()
    start_forward_workers()
    server = ThreadingHTTPServer((HOST, PORT), RequestHandler)
    print(f"Webhook server listening on http://{HOST}:{PORT}")
    print(f"SQLite database: {DB_PATH}")
    print(f"Display timezone: {WEBHOOK_TIMEZONE}")
    server.serve_forever()


if __name__ == "__main__":
    main()
