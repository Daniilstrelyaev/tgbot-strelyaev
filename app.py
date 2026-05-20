import os, json, urllib3, threading, time, logging
from flask import Flask, request, abort

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

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

# ── Keepalive: ping self every 4 min so Render never sleeps ──────────────────
def _keepalive():
    # Render injects the public URL of this service
    service_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not service_url:
        log.info("[keepalive] RENDER_EXTERNAL_URL not set, skipping self-ping")
        return
    log.info(f"[keepalive] will ping {service_url}/health every 4 min")
    # Parse host from URL
    host = service_url.replace("https://", "").replace("http://", "").split("/")[0]
    self_pool = urllib3.HTTPSConnectionPool(
        host, port=443, maxsize=1, cert_reqs="CERT_NONE",
        retries=urllib3.Retry(connect=3, read=False, backoff_factor=1),
    )
    while True:
        time.sleep(240)  # 4 minutes
        try:
            r = self_pool.urlopen("GET", "/health",
                timeout=urllib3.Timeout(connect=10, read=15))
            log.info(f"[keepalive] ping → {r.status}")
        except Exception as e:
            log.warning(f"[keepalive] ping failed: {e}")

threading.Thread(target=_keepalive, daemon=True, name="keepalive").start()
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
