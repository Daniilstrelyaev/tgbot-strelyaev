import os, json, urllib3
from flask import Flask, request, abort

TOKEN = os.environ["BOT_TOKEN"]
START_TEXT = (
    "Привет 👋\n\n"
    "Держи ссылку на канал — там выходит весь контент:\n"
    "https://t.me/strelyae_v\n\n"
    "Подписывайся, чтобы не пропустить 🤍"
)

app = Flask(__name__)

pool = urllib3.HTTPSConnectionPool(
    "api.telegram.org", port=443,
    maxsize=4, cert_reqs="CERT_NONE",
    retries=urllib3.Retry(connect=5, read=False, backoff_factor=0),
)

def send_message(chat_id, text):
    pool.urlopen(
        "POST", f"/bot{TOKEN}/sendMessage",
        body=json.dumps({"chat_id": chat_id, "text": text}).encode(),
        headers={"Content-Type": "application/json"},
        timeout=urllib3.Timeout(connect=3, read=10),
    )

@app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(silent=True)
    if not update:
        abort(400)

    msg = update.get("message", {})
    text = msg.get("text", "")
    chat_id = msg.get("chat", {}).get("id")

    if text.startswith("/start") and chat_id:
        send_message(chat_id, START_TEXT)

    return "ok", 200

@app.route("/health")
def health():
    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
