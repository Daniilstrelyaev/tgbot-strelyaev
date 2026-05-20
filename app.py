import atexit, hmac, json, logging, os, queue, threading, time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlparse

import certifi, urllib3
from flask import Flask, abort, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

TOKEN = os.environ["BOT_TOKEN"].strip()
WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"].strip()

START_TEXT = (
    "Привет 👋\n\n"
    "Держи ссылку на канал — там выходит весь контент:\n"
    "https://t.me/strelyae_v\n\n"
    "Подписывайся, чтобы не пропустить 🤍"
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024  # 256 KB — защита от DoS

# ── Telegram connection pool ──────────────────────────────────────────────────
tg_pool = urllib3.HTTPSConnectionPool(
    "api.telegram.org", port=443,
    maxsize=8, block=True,
    cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(),
    retries=urllib3.Retry(connect=2, read=0, status=0, backoff_factor=0.2),
)


def _redact_token(value: str) -> str:
    """Убирает токен из строк для безопасного логирования."""
    return value.replace(TOKEN, "<BOT_TOKEN>")


# ── Дедупликация update_id ────────────────────────────────────────────────────
_seen_ids: deque = deque()
_seen_ids_set: set = set()
_seen_lock = threading.Lock()
_DEDUP_MAX = 500


def _remember_update(update_id: int) -> bool:
    """Возвращает True если update_id новый (не дубликат)."""
    with _seen_lock:
        if update_id in _seen_ids_set:
            return False
        _seen_ids.append(update_id)
        _seen_ids_set.add(update_id)
        if len(_seen_ids) > _DEDUP_MAX:
            old = _seen_ids.popleft()
            _seen_ids_set.discard(old)
        return True


def _is_start_command(text: str) -> bool:
    """Корректная проверка /start — нет ложных срабатываний на /start123."""
    if not text:
        return False
    cmd = text.strip().split(maxsplit=1)[0].split("@", 1)[0]
    return cmd == "/start"


# ── Фоновая очередь отправки ──────────────────────────────────────────────────
@dataclass(slots=True)
class SendTask:
    chat_id: int
    username: str
    enqueued_at: float


send_queue: queue.Queue = queue.Queue(maxsize=1000)
stop_event = threading.Event()


def send_message(chat_id: int, text: str) -> bool:
    """Отправка сообщения с полным retry (flood control, 5xx, таймауты)."""
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

            err_code = data.get("error_code", 0)
            description = data.get("description", "")
            log.warning(f"sendMessage attempt {attempt+1} failed: {err_code} {description}")

            if err_code == 429:
                # Flood control: ждём столько, сколько сказал Telegram
                retry_after = data.get("parameters", {}).get("retry_after", 5)
                log.warning(f"Flood control: retry after {retry_after}s")
                time.sleep(retry_after)
            elif err_code in (400, 403):
                # Постоянная ошибка (неверный chat_id / бот заблокирован) — не ретраить
                return False
            else:
                time.sleep(1 * (attempt + 1))

        except urllib3.exceptions.ReadTimeoutError:
            log.warning(f"sendMessage timeout (attempt {attempt+1})")
            time.sleep(0.5 * (attempt + 1))
        except Exception as e:
            log.error(f"sendMessage error (attempt {attempt+1}): {_redact_token(str(e))}")
            time.sleep(0.5 * (attempt + 1))

    log.error(f"sendMessage: все 3 попытки исчерпаны для chat_id={chat_id}")
    return False


def _sender_worker() -> None:
    """Фоновый поток, который опустошает очередь send_queue."""
    log.info("[sender] worker started")
    while not stop_event.is_set():
        try:
            task: SendTask = send_queue.get(timeout=1)
        except queue.Empty:
            continue

        wait_ms = (time.time() - task.enqueued_at) * 1000
        t0 = time.time()
        ok = send_message(task.chat_id, START_TEXT)
        elapsed_ms = (time.time() - t0) * 1000
        log.info(
            f"/start @{task.username} ({task.chat_id}) "
            f"wait={wait_ms:.0f}ms send={elapsed_ms:.0f}ms → {'ok' if ok else 'FAIL'}"
        )
        send_queue.task_done()
    log.info("[sender] worker stopped")


# ── Webhook endpoint ──────────────────────────────────────────────────────────
@app.post("/webhook")
def webhook():
    # Проверяем секретный заголовок — timing-safe сравнение
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(secret, WEBHOOK_SECRET):
        abort(403)

    update = request.get_json(silent=True)
    if not update:
        abort(400)

    # Дедупликация (Telegram может прислать один update дважды)
    update_id = update.get("update_id")
    if update_id is not None and not _remember_update(update_id):
        log.info(f"Duplicate update_id={update_id} ignored")
        return "ok", 200

    msg = update.get("message", {})
    text = msg.get("text", "")
    chat_id = msg.get("chat", {}).get("id")
    username = msg.get("from", {}).get("username") or "—"

    if _is_start_command(text) and chat_id:
        try:
            send_queue.put_nowait(SendTask(
                chat_id=chat_id,
                username=username,
                enqueued_at=time.time(),
            ))
        except queue.Full:
            log.error("send_queue is full — dropping /start message")

    return "ok", 200


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "queue_size": send_queue.qsize()}, 200


# ── Keepalive: не даём Render free tier уснуть ────────────────────────────────
def _keepalive() -> None:
    service_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not service_url:
        log.warning("[keepalive] RENDER_EXTERNAL_URL not set — self-ping disabled")
        return

    parsed = urlparse(service_url)
    host = parsed.hostname

    if parsed.scheme == "https":
        pool = urllib3.HTTPSConnectionPool(
            host, port=443, maxsize=1,
            cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(),
            retries=urllib3.Retry(connect=2, read=False, backoff_factor=1),
        )
    else:
        pool = urllib3.HTTPConnectionPool(
            host, port=80, maxsize=1,
            retries=urllib3.Retry(connect=2, read=False, backoff_factor=1),
        )

    log.info(f"[keepalive] started → pinging {host}/health every 4 min")
    time.sleep(5)  # первый пинг через 5 секунд после старта
    cycle = 0
    while not stop_event.is_set():
        try:
            r = pool.urlopen("GET", "/health", timeout=urllib3.Timeout(connect=10, read=20))
            log.info(f"[keepalive] ping #{cycle} → {r.status}")
        except Exception as e:
            log.warning(f"[keepalive] ping #{cycle} failed: {e}")
        cycle += 1
        stop_event.wait(timeout=240)  # 4 минуты, прерывается при stop_event


# ── Запуск/остановка фоновых потоков ─────────────────────────────────────────
def _start_background_threads() -> None:
    threading.Thread(target=_sender_worker, daemon=True, name="sender").start()
    threading.Thread(target=_keepalive, daemon=True, name="keepalive").start()


def _shutdown_background_threads() -> None:
    log.info("Shutting down background threads...")
    stop_event.set()


_start_background_threads()
atexit.register(_shutdown_background_threads)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
