# Discord Webhook Server

独立的 Discord 消息接收服务，提供：

- `POST /webhook/messages` 接收消息并写入 SQLite
- `GET /messages` Web 页面查看消息，支持搜索和频道筛选
- `GET /api/messages` JSON 查询消息
- `GET /api/messages/{id}` 查询单条消息
- `GET /api/health` 健康检查
- 可选：按频道转发到飞书/企业微信，并记录转发状态

## 本地启动

```bash
python app.py
```

默认监听 `http://0.0.0.0:8080`，数据库保存到 `webhook_server/data/messages.sqlite3`。

## Docker 启动

```bash
docker build -t discord-webhook-server webhook_server
docker run -d --name discord-webhook-server -p 8080:8080 -v ./webhook_server/data:/data discord-webhook-server
```

## 可选鉴权

设置 `WEBHOOK_TOKEN` 后，Web 页面、查询 API 和写入 API 都需要 token。

```bash
WEBHOOK_TOKEN=your-token python app.py
```

请求时使用其中一种方式：

```bash
curl -H "Authorization: Bearer your-token" http://localhost:8080/api/messages
curl http://localhost:8080/messages?token=your-token
```

## 写入示例

```bash
curl -X POST http://localhost:8080/webhook/messages \
  -H "Content-Type: application/json" \
  -d '{
    "id": "discord-message-id",
    "username": "alice",
    "content": "hello",
    "timestamp": "2026-08-06T10:00:00+08:00",
    "channel_url": "https://discord.com/channels/server/channel",
    "channel_name": "频道channel",
    "attachments": []
  }'
```

## 查询示例

```bash
curl "http://localhost:8080/api/messages?limit=20&offset=0&q=hello"
curl "http://localhost:8080/api/messages?channel_url=https://discord.com/channels/server/channel"
curl "http://localhost:8080/api/messages/1"
```

## 服务端转发

推荐让主监听程序统一配置为：

```python
SENDER_TYPE = "webhook_server"
WEBHOOK_SERVER_URL = "http://webhook-server:8080/webhook/messages"
```

然后在 `webhook_server` 里决定要转发到哪些目标。配置方式是环境变量 `FORWARD_ROUTES_JSON`：

```bash
FORWARD_ROUTES_JSON='[
  {
    "channels": ["https://discord.com/channels/服务器ID/频道ID"],
    "targets": [
      {
        "type": "feishu",
        "name": "飞书群",
        "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx",
        "secret": ""
      },
      {
        "type": "enterprise_wechat",
        "name": "企业微信群",
        "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxx"
      }
    ]
  }
]'
```

规则说明：

- `channels` 为空或不填时匹配所有频道。
- 同一条消息可以匹配多个 target，但只会监听和入库一次。
- 转发任务写入 `forward_deliveries` 表，Web 页面和 `/api/messages` 会展示 `forwards` 状态。
- 目前支持 `feishu` 和 `enterprise_wechat`。
