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
import sqlite3
import sys
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("WEBHOOK_DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.getenv("WEBHOOK_DB_PATH", DATA_DIR / "messages.sqlite3"))
HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "messages": [row_to_message(row) for row in rows],
    }


def get_message(message_id: int) -> Optional[Dict[str, Any]]:
    with db_connect() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    return row_to_message(row) if row else None


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
            self.send_json({"ok": True, "db_path": str(DB_PATH)})
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
        except ValueError as e:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(e))
            return
        except Exception as e:
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(e))
            return

        status = HTTPStatus.CREATED if inserted else HTTPStatus.OK
        self.send_json({"ok": True, "id": message_id, "inserted": inserted}, status=status)

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

    content = html.escape(message.get("content") or "")
    username = html.escape(message.get("username") or "")
    channel = html.escape(message.get("channel_name") or "")
    timestamp = html.escape(message.get("timestamp") or "")
    created_at = html.escape(message.get("created_at") or "")
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
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), RequestHandler)
    print(f"Webhook server listening on http://{HOST}:{PORT}")
    print(f"SQLite database: {DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
