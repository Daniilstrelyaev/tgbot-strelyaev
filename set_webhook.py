"""
После деплоя запусти:
  BOT_TOKEN=xxx WEBHOOK_URL=https://your-app.railway.app python3 set_webhook.py
"""
import os, ssl, json, urllib.request

TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"].rstrip("/")

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

# Останавливаем polling-бота (удаляем старый webhook и очищаем очередь)
tg("deleteWebhook", {"drop_pending_updates": True})

# Ставим новый webhook
url = f"{WEBHOOK_URL}/webhook/{TOKEN}"
result = tg("setWebhook", {
    "url": url,
    "allowed_updates": ["message"],
    "drop_pending_updates": True,
})
print("setWebhook:", result)

info = tg("getWebhookInfo", {})
print("Webhook URL:", info.get("result", {}).get("url"))
print("Pending:", info.get("result", {}).get("pending_update_count"))
