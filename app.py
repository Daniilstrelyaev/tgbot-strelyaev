import os, json, urllib3, threading, time, logging
import certifi
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

# FIX #5: используем certifi для проверки SSL-сертификата Telegram
tg_pool = urllib3.HTTPSConnectionPool(
    "api.telegram.org", port=443,
    maxsize=4,
    ca_certs=certifi.where(),
    retries=urllib3.Retry(connect=5, read=False, backoff_factor=0.2),
)


def send_message(chat_id: int, text: str) -> bool:
    """Send message. Retries on network errors AND Telegram API errors (flood/rate limit)."""
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

            # FIX #3 + #6: retry при flood control (429) и других ошибках API
            err_code = data.get("error_code", 0)
            description = data.get("description", "")
            log.warning(f"sendMessage attempt {attempt+1} failed: {err_code} {description}")

            if err_code == 429:
                # Flood control: ждём сколько сказал Telegram
                retry_after = data.get("parameters", {}).get("retry_after", 5)
                log.warning(f"Flood control: retry after {retry_after}s")
                time.sleep(retry_after)
            elif err_code in (400, 403):
                # Bad request или бот заблокирован — не ретраить
                return False
            else:
                time.sleep(1 * (attempt + 1))

        except urllib3.exceptions.ReadTimeoutError:
            log.warning(f"sendMessage timeout (attempt {attempt+1})")
            time.sleep(0.5 * (attempt + 1))
        except Exception as e:
            log.error(f"sendMessage error (attempt {attempt+1}): {e}")
            time.sleep(0.5 * (attempt + 1))

    log.error(f"sendMessage: все {3} попытки исчерпаны для chat_id={chat_id}")
    return False


@app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(silent=True)
    if not update:
        abort(400)

    msg = update.get("message", {})
    text = msg.get("text", "")
    chat_id = msg.get("chat", {}).get("id")
    username = msg.get("from", {}).get("username") or "—"

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
    # FIX #5: CERT_NONE только для self-ping (собственный домен, Cloudflare-сертификат)
    self_pool = urllib3.HTTPSConnectionPool(
        host, port=443, maxsize=1, cert_reqs="CERT_NONE",
        retries=urllib3.Retry(connect=2, read=False, backoff_factor=1),
    )

    log.info(f"[keepalive] started → pinging {host}/health every 4 min")

    # Первый пинг через 5 секунд после старта
    time.sleep(5)
    cycle = 0
    # FIX #1: while True вместо range(9999) — работает вечно, не останавливается через 28 дней
    while True:
        try:
            r = self_pool.urlopen(
                "GET", "/health",
                timeout=urllib3.Timeout(connect=10, read=20),
            )
            log.info(f"[keepalive] ping #{cycle} → {r.status}")
        except Exception as e:
            log.warning(f"[keepalive] ping #{cycle} failed: {e}")
        cycle += 1
        time.sleep(240)  # 4 минуты
# ─────────────────────────────────────────────────────────────────────────────


# FIX #2: убираем бесполезный _keepalive_started флаг — просто запускаем поток
threading.Thread(target=_keepalive, daemon=True, name="keepalive").start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
