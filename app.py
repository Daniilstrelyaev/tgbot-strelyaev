import os, json, urllib3, threading, time, logging
from flask import Flask, request, abort

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

TOKEN = os.environ["BOT_TOKEN"]
START_TEXT = (
    "Привет 👋\n\n"
    "Держи ссылку на канал — там выходит весь контент:\n"
    "https://t.me/strelyae_v\n\n"
    "Подписывайся, чтобы не пропустить 🤍"
)

app = Flask(__name__)

# Telegram connection pool
tg_pool = urllib3.HTTPSConnectionPool(
    "api.telegram.org", port=443,
    maxsize=4, cert_reqs="CERT_NONE",
    retries=urllib3.Retry(connect=5, read=False, backoff_factor=0.2),
)


def send_message(chat_id: int, text: str) -> bool:
    """Send message to user. Returns True on success."""
    for attempt in range(3):
        try:
            r = tg_pool.urlopen(
                "POST", f"/bot{TOKEN}/sendMessage",
                body=json.dumps({"chat_id": chat_id, "text": text}).encode(),
                headers={"Content-Type": "application/json"},
                timeout=urllib3.Timeout(connect=5, read=15),
            )
            data = json.loads(r.data)
            if data.get("ok"):
                return True
            log.warning(f"sendMessage non-ok: {data}")
            return False
        except urllib3.exceptions.ReadTimeoutError:
            log.warning(f"sendMessage timeout (attempt {attempt + 1})")
        except Exception as e:
            log.error(f"sendMessage error (attempt {attempt + 1}): {e}")
        time.sleep(0.5 * (attempt + 1))
    return False


@app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(silent=True)
    if not update:
        abort(400)

    msg = update.get("message", {})
    text = msg.get("text", "")
    chat_id = msg.get("chat", {}).get("id")
    username = msg.get("from", {}).get("username", "—")

    if text.startswith("/start") and chat_id:
        t0 = time.time()
        ok = send_message(chat_id, START_TEXT)
        elapsed = (time.time() - t0) * 1000
        log.info(f"/start @{username} ({chat_id}) → {'ok' if ok else 'FAIL'} {elapsed:.0f}ms")

    return "ok", 200


@app.route("/health")
def health():
    return "ok", 200


# ── Keepalive: prevent Render free-tier from sleeping ────────────────────────
def _keepalive():
    service_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not service_url:
        log.warning("[keepalive] RENDER_EXTERNAL_URL not set — self-ping disabled")
        return

    host = service_url.removeprefix("https://").removeprefix("http://").split("/")[0]
    self_pool = urllib3.HTTPSConnectionPool(
        host, port=443, maxsize=1, cert_reqs="CERT_NONE",
        retries=urllib3.Retry(connect=2, read=False, backoff_factor=1),
    )

    log.info(f"[keepalive] started → pinging {host}/health every 4 min")

    # Первый пинг сразу — не ждать 4 минуты
    time.sleep(5)
    for cycle in range(9999):
        try:
            r = self_pool.urlopen(
                "GET", "/health",
                timeout=urllib3.Timeout(connect=10, read=20),
            )
            log.info(f"[keepalive] ping #{cycle} → {r.status}")
        except Exception as e:
            log.warning(f"[keepalive] ping #{cycle} failed: {e}")
        time.sleep(240)  # 4 минуты


# Запускаем только в основном потоке gunicorn (не в preforked workers)
# Проверяем GUNICORN_WORKER_CLASS чтобы не запустить в каждом worker
_keepalive_started = False
if not _keepalive_started:
    _keepalive_started = True
    threading.Thread(target=_keepalive, daemon=True, name="keepalive").start()
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
