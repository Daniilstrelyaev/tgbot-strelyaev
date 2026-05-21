import atexit
import hmac
import json
import logging
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import certifi
import urllib3
from flask import Flask, abort, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

TOKEN = os.environ["BOT_TOKEN"].strip()
WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"].strip()
PORT = int(os.environ.get("PORT", "8080"))
KEEPALIVE_ENABLED = os.environ.get("KEEPALIVE_ENABLED", "true").lower() in {"1", "true", "yes"}
KEEPALIVE_INTERVAL_SECONDS = int(os.environ.get("KEEPALIVE_INTERVAL_SECONDS", "240"))
SEND_QUEUE_MAX_SIZE = int(os.environ.get("SEND_QUEUE_MAX_SIZE", "1000"))
MAX_SEEN_UPDATE_IDS = int(os.environ.get("MAX_SEEN_UPDATE_IDS", "10000"))
MAX_SEND_ATTEMPTS = 3

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is empty")
if not WEBHOOK_SECRET:
    raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is empty")

START_TEXT = (
    "Привет 👋\n\n"
    "Держи ссылку на канал — там выходит весь контент:\n"
    "https://t.me/strelyae_v\n\n"
    "Подписывайся, чтобы не пропустить 🤍"
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024

tg_pool = urllib3.HTTPSConnectionPool(
    "api.telegram.org",
    port=443,
    maxsize=8,
    block=True,
    cert_reqs="CERT_REQUIRED",
    ca_certs=certifi.where(),
    retries=urllib3.Retry(connect=2, read=0, status=0, backoff_factor=0.2),
)


@dataclass(slots=True)
class SendTask:
    chat_id: int
    username: str
    enqueued_at: float


send_queue: queue.Queue[SendTask] = queue.Queue(maxsize=SEND_QUEUE_MAX_SIZE)
stop_event = threading.Event()

_updates_lock = threading.Lock()
_seen_update_ids: deque[int] = deque()
_seen_update_ids_set: set[int] = set()
_inflight_update_ids: set[int] = set()

_threads_started = False
_threads_lock = threading.Lock()


def _redact_secret(value: str) -> str:
    return value.replace(TOKEN, "<BOT_TOKEN>")


def _remember_update_locked(update_id: int) -> None:
    _seen_update_ids.append(update_id)
    _seen_update_ids_set.add(update_id)
    if len(_seen_update_ids) > MAX_SEEN_UPDATE_IDS:
        oldest = _seen_update_ids.popleft()
        _seen_update_ids_set.discard(oldest)


def _remember_update(update_id: int) -> None:
    with _updates_lock:
        _remember_update_locked(update_id)


def _claim_update(update_id: int) -> bool:
    with _updates_lock:
        if update_id in _seen_update_ids_set or update_id in _inflight_update_ids:
            return False
        _inflight_update_ids.add(update_id)
        return True


def _commit_update(update_id: int) -> None:
    with _updates_lock:
        _inflight_update_ids.discard(update_id)
        _remember_update_locked(update_id)


def _release_update(update_id: int) -> None:
    with _updates_lock:
        _inflight_update_ids.discard(update_id)


def _is_start_command(text: str) -> bool:
    if not text:
        return False
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return False
    token = parts[0]
    command = token.split("@", 1)[0]
    return command == "/start"


def send_message(chat_id: int, text: str) -> bool:
    payload = json.dumps({"chat_id": chat_id, "text": text}, ensure_ascii=False).encode("utf-8")

    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            response = tg_pool.urlopen(
                "POST",
                f"/bot{TOKEN}/sendMessage",
                body=payload,
                headers={"Content-Type": "application/json"},
                timeout=urllib3.Timeout(connect=5, read=15),
            )
        except urllib3.exceptions.HTTPError as exc:
            log.warning("sendMessage transport error attempt %s: %s", attempt, _redact_secret(str(exc)))
            time.sleep(0.5 * attempt)
            continue
        except Exception as exc:
            log.warning("sendMessage unexpected error attempt %s: %s", attempt, _redact_secret(str(exc)))
            time.sleep(0.5 * attempt)
            continue

        status = response.status
        body_text = response.data.decode("utf-8", errors="replace")
        parsed: dict[str, Any] = {}
        try:
            candidate = json.loads(body_text)
            if isinstance(candidate, dict):
                parsed = candidate
        except json.JSONDecodeError:
            parsed = {}

        if status == 429 or parsed.get("error_code") == 429:
            retry_after = 1
            retry_header = response.headers.get("Retry-After")
            if retry_header and retry_header.isdigit():
                retry_after = max(1, int(retry_header))
            parameters = parsed.get("parameters")
            if isinstance(parameters, dict):
                maybe_retry = parameters.get("retry_after")
                if isinstance(maybe_retry, int):
                    retry_after = max(1, maybe_retry)
            retry_after = min(retry_after, 30)
            log.warning("sendMessage rate-limited. attempt=%s retry_after=%ss", attempt, retry_after)
            time.sleep(retry_after)
            continue

        if 500 <= status < 600:
            log.warning("sendMessage upstream 5xx status=%s attempt=%s", status, attempt)
            time.sleep(attempt)
            continue

        if status >= 400:
            log.warning(
                "sendMessage permanent HTTP error status=%s error_code=%s description=%s",
                status,
                parsed.get("error_code"),
                parsed.get("description"),
            )
            return False

        if parsed.get("ok") is True:
            return True

        err_code = parsed.get("error_code")
        if err_code in (400, 403):
            log.info("sendMessage rejected error_code=%s description=%s", err_code, parsed.get("description"))
            return False

        log.warning("sendMessage unexpected body attempt=%s status=%s body=%s", attempt, status, body_text[:500])
        time.sleep(attempt)

    log.error("sendMessage failed after %s attempts chat_id=%s", MAX_SEND_ATTEMPTS, chat_id)
    return False


def _sender_worker() -> None:
    while not stop_event.is_set():
        try:
            task = send_queue.get(timeout=1)
        except queue.Empty:
            continue

        started_at = time.monotonic()
        try:
            ok = send_message(task.chat_id, START_TEXT)
        finally:
            send_queue.task_done()

        send_duration_ms = (time.monotonic() - started_at) * 1000
        queue_duration_ms = (started_at - task.enqueued_at) * 1000
        log.info(
            "/start @%s (%s) -> %s send=%.0fms queue=%.0fms queue_size=%s",
            task.username,
            task.chat_id,
            "ok" if ok else "FAIL",
            send_duration_ms,
            queue_duration_ms,
            send_queue.qsize(),
        )


def _build_keepalive_target(service_url: str) -> tuple[Any | None, str, str]:
    parsed = urlparse(service_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        log.warning("[keepalive] invalid RENDER_EXTERNAL_URL=%r", service_url)
        return None, "", ""

    scheme = parsed.scheme
    host = parsed.hostname
    port = parsed.port or (443 if scheme == "https" else 80)
    base_path = parsed.path.rstrip("/")
    health_path = f"{base_path}/health" if base_path else "/health"

    retries = urllib3.Retry(connect=2, read=0, status=0, backoff_factor=0.3)
    if scheme == "https":
        pool = urllib3.HTTPSConnectionPool(
            host,
            port=port,
            maxsize=1,
            cert_reqs="CERT_REQUIRED",
            ca_certs=certifi.where(),
            retries=retries,
        )
    else:
        pool = urllib3.HTTPConnectionPool(host, port=port, maxsize=1, retries=retries)

    target = f"{scheme}://{host}:{port}{health_path}"
    return pool, health_path, target


def _keepalive() -> None:
    pool: Any | None = None
    health_path = "/health"
    active_url = ""
    warned_missing_url = False
    cycle = 0

    while not stop_event.is_set():
        current_url = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
        if not current_url:
            if not warned_missing_url:
                log.warning("[keepalive] RENDER_EXTERNAL_URL is empty; self-ping disabled until it appears")
                warned_missing_url = True
            stop_event.wait(KEEPALIVE_INTERVAL_SECONDS)
            continue
        warned_missing_url = False

        if current_url != active_url or pool is None:
            pool, health_path, target = _build_keepalive_target(current_url)
            active_url = current_url
            if pool is None:
                stop_event.wait(KEEPALIVE_INTERVAL_SECONDS)
                continue
            log.info("[keepalive] target updated -> %s (every %ss)", target, KEEPALIVE_INTERVAL_SECONDS)

        try:
            response = pool.urlopen(
                "GET",
                health_path,
                timeout=urllib3.Timeout(connect=5, read=10),
                headers={"User-Agent": "render-keepalive/1.0"},
            )
            log.debug("[keepalive] ping #%s -> %s", cycle, response.status)
        except Exception as exc:
            log.warning("[keepalive] ping #%s failed: %s", cycle, _redact_secret(str(exc)))

        cycle += 1
        stop_event.wait(KEEPALIVE_INTERVAL_SECONDS)


def _start_background_threads() -> None:
    global _threads_started
    with _threads_lock:
        if _threads_started:
            return

        threading.Thread(target=_sender_worker, daemon=True, name="sender-worker").start()
        if KEEPALIVE_ENABLED:
            threading.Thread(target=_keepalive, daemon=True, name="keepalive").start()
        _threads_started = True


def _shutdown_background_threads() -> None:
    stop_event.set()


@app.post("/webhook")
def webhook() -> tuple[str, int]:
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(secret_header, WEBHOOK_SECRET):
        abort(403)

    if not request.is_json:
        abort(400)

    update = request.get_json(silent=True)
    if not isinstance(update, dict):
        abort(400)

    update_id_value = update.get("update_id")
    has_update_id = isinstance(update_id_value, int) and not isinstance(update_id_value, bool)
    update_id = update_id_value if has_update_id else None
    claimed_update_id: int | None = None
    update_finalized = False

    if update_id is not None:
        if not _claim_update(update_id):
            return "ok", 200
        claimed_update_id = update_id

    try:
        message = update.get("message")
        if not isinstance(message, dict):
            if claimed_update_id is not None:
                _commit_update(claimed_update_id)
                update_finalized = True
            return "ok", 200

        text = message.get("text")
        chat = message.get("chat")
        if not isinstance(text, str) or not isinstance(chat, dict):
            if claimed_update_id is not None:
                _commit_update(claimed_update_id)
                update_finalized = True
            return "ok", 200
        if not _is_start_command(text):
            if claimed_update_id is not None:
                _commit_update(claimed_update_id)
                update_finalized = True
            return "ok", 200

        chat_id = chat.get("id")
        if not isinstance(chat_id, int):
            if claimed_update_id is not None:
                _commit_update(claimed_update_id)
                update_finalized = True
            return "ok", 200

        username = "—"
        from_user = message.get("from")
        if isinstance(from_user, dict):
            maybe_username = from_user.get("username")
            if isinstance(maybe_username, str) and maybe_username:
                username = maybe_username

        task = SendTask(chat_id=chat_id, username=username, enqueued_at=time.monotonic())
        try:
            send_queue.put_nowait(task)
            if claimed_update_id is not None:
                _commit_update(claimed_update_id)
                update_finalized = True
        except queue.Full:
            if claimed_update_id is not None:
                _release_update(claimed_update_id)
                update_finalized = True
            log.error("send queue overflow; returning 503 for Telegram retry")
            return "busy", 503

        return "ok", 200
    finally:
        if claimed_update_id is not None and not update_finalized:
            _release_update(claimed_update_id)


@app.get("/health")
def health() -> tuple[dict[str, Any], int]:
    return {"status": "ok", "queue_size": send_queue.qsize()}, 200


_start_background_threads()
atexit.register(_shutdown_background_threads)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
