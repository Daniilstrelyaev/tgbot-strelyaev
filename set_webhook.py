"""
После деплоя запусти:
  BOT_TOKEN=xxx WEBHOOK_URL=https://your-app.onrender.com WEBHOOK_SECRET=yyy python3 set_webhook.py
"""
import os, ssl, json, urllib.request

TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"].rstrip("/")
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def tg(method, data):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    for _ in range(8):
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
                return json.loads(r.read())
        except Exception:
            pass
    return {}


# Удаляем старый webhook и очищаем очередь обновлений
tg("deleteWebhook", {"drop_pending_updates": True})

# Ставим новый webhook с secret_token (без токена в URL)
url = f"{WEBHOOK_URL}/webhook"
result = tg("setWebhook", {
    "url": url,
    "secret_token": WEBHOOK_SECRET,
    "allowed_updates": ["message"],
    "drop_pending_updates": True,
})
print("setWebhook:", result)

info = tg("getWebhookInfo", {})
print("Webhook URL:", info.get("result", {}).get("url"))
print("Pending:", info.get("result", {}).get("pending_update_count"))
