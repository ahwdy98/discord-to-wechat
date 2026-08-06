# 消息发送方式配置
# 可选值:
#   "wechat"             - 微信个人号（需要小号扫码登录，发送给大号）
#   "enterprise_wechat"  - 企业微信机器人（使用Webhook，发送到企业微信群）
#   "feishu"             - 飞书自定义机器人（使用Webhook，发送到飞书群）
#   "webhook_server"     - 自建Webhook服务端（写入SQLite，提供Web/API查看）
SENDER_TYPE = "enterprise_wechat"  # 可改为 wechat / enterprise_wechat / feishu / webhook_server

# Discord配置
# 可以配置多个频道URL，程序会轮询监听所有频道
DISCORD_CHANNEL_URLS = [
    # 在这里添加更多频道，例如：
    # "https://discord.com/channels/服务器ID/频道ID",
]

# 微信个人号配置（当 SENDER_TYPE = "wechat" 时使用）
WECHAT_RECEIVER_NAME = ""  # 你大号在小号中的备注名或昵称


# 企业微信机器人配置（当 SENDER_TYPE = "enterprise_wechat" 时使用）
# 获取方式：
# 1. 在企业微信群中，点击群设置 -> 群机器人 -> 添加机器人
# 2. 复制机器人的 Webhook 地址到下方

# 旧版配置（单个Webhook，所有频道消息都发到这里）
# ENTERPRISE_WECHAT_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcde"

# 新版配置（多Webhook映射，不同频道发到不同群）
# 格式: [{'hook': 'Webhook地址', 'channel': 'Discord频道URL'}]
ENTERPRISE_WECHAT_WEBHOOK_LIST = [
    # {
    #     'hook': 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxx',
    #     'channel': 'https://discord.com/channels/123456789/987654321'
    # },
    # {
    #     'hook': 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=yyyyy',
    #     'channel': 'https://discord.com/channels/123456789/123456789'
    # }
]


# 飞书自定义机器人配置（当 SENDER_TYPE = "feishu" 时使用）
# 获取方式：
# 1. 在飞书群中，点击群设置 -> 群机器人 -> 添加机器人 -> 自定义机器人
# 2. 复制机器人的 Webhook 地址到下方
# 3. 如果机器人开启了“签名校验”，填写 FEISHU_SECRET

# 单个Webhook，所有频道消息都发到这里
FEISHU_WEBHOOK = ""
FEISHU_SECRET = ""

# 多Webhook映射，不同Discord频道发到不同飞书群
# 格式: [{'hook': 'Webhook地址', 'channel': 'Discord频道URL', 'secret': '签名密钥，可选'}]
FEISHU_WEBHOOK_LIST = [
    # {
    #     'hook': 'https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx',
    #     'channel': 'https://discord.com/channels/123456789/987654321',
    #     'secret': ''
    # },
    # {
    #     'hook': 'https://open.feishu.cn/open-apis/bot/v2/hook/yyyyy',
    #     'channel': 'https://discord.com/channels/123456789/123456789',
    #     'secret': '对应机器人的签名密钥'
    # }
]


# 自建Webhook服务端配置（当 SENDER_TYPE = "webhook_server" 时使用）
# Docker Compose 内部访问地址：
# WEBHOOK_SERVER_URL = "http://webhook-server:8080/webhook/messages"
# 本机直接运行服务端，或 Linux host 网络模式时可使用：
# WEBHOOK_SERVER_URL = "http://127.0.0.1:8080/webhook/messages"
WEBHOOK_SERVER_URL = ""
WEBHOOK_SERVER_TOKEN = ""  # 如果 webhook_server 设置了 WEBHOOK_TOKEN，这里填同一个值


# 运行配置
# 监控间隔（秒）
CHECK_INTERVAL = 30

# Chrome配置
HEADLESS_MODE = False  # 设为True则无头模式（不显示浏览器窗口）
