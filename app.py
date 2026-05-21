from __future__ import annotations

import atexit
import hmac
import json
import logging
import os
import threading
import time
from collections import deque
from enum import Enum
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
MAX_SEEN_UPDATE_IDS = int(os.environ.get("MAX_SEEN_UPDATE_IDS", "10000"))
MAX_SEND_ATTEMPTS = int(os.environ.get("MAX_SEND_ATTEMPTS", "2"))
SEND_RETRY_AFTER_LIMIT_SECONDS = int(os.environ.get("SEND_RETRY_AFTER_LIMIT_SECONDS", "2"))
START_RATE_LIMIT_SECONDS = int(os.environ.get("START_RATE_LIMIT_SECONDS", "30"))

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
    retries=urllib3.Retry(connect=1, read=0, status=0, redirect=0, backoff_factor=0.2),
)

stop_event = threading.Event()

_updates_lock = threading.Lock()
_seen_update_ids: deque[int] = deque()
_seen_update_ids_set: set[int] = set()
_inflight_update_ids: set[int] = set()

_rate_limit_lock = threading.Lock()
_last_start_by_chat_id: dict[int, float] = {}
_inflight_start_chat_ids: set[int] = set()

_threads_started = False
_threads_lock = threading.Lock()


class SendResult(Enum):
    OK = "ok"
    PERMANENT_FAILURE = "permanent_failure"
    RETRYABLE_FAILURE = "retryable_failure"


def _redact_secret(value: str) -> str:
    return value.replace(TOKEN, "<BOT_TOKEN>")


def _remember_update_locked(update_id: int) -> None:
    _seen_update_ids.append(update_id)
    _seen_update_ids_set.add(update_id)
    if len(_seen_update_ids) > MAX_SEEN_UPDATE_IDS:
        oldest = _seen_update_ids.popleft()
        _seen_update_ids_set.discard(oldest)


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
    command = parts[0].split("@", 1)[0]
    return command == "/start"


def _prune_rate_limit_locked(now: float) -> None:
    if len(_last_start_by_chat_id) <= MAX_SEEN_UPDATE_IDS:
        return

    cutoff = now - START_RATE_LIMIT_SECONDS
    stale_chat_ids = [
        stored_chat_id
        for stored_chat_id, timestamp in _last_start_by_chat_id.items()
        if timestamp < cutoff
    ]
    for stored_chat_id in stale_chat_ids:
        _last_start_by_chat_id.pop(stored_chat_id, None)


def _claim_chat_send(chat_id: int) -> bool:
    if START_RATE_LIMIT_SECONDS <= 0:
        return True

    now = time.monotonic()
    with _rate_limit_lock:
        last_seen = _last_start_by_chat_id.get(chat_id)
        if last_seen is not None and now - last_seen < START_RATE_LIMIT_SECONDS:
            return False
        if chat_id in _inflight_start_chat_ids:
            return False

        _inflight_start_chat_ids.add(chat_id)
        _prune_rate_limit_locked(now)
        return True


def _commit_chat_send(chat_id: int) -> None:
    if START_RATE_LIMIT_SECONDS <= 0:
        return

    with _rate_limit_lock:
        _inflight_start_chat_ids.discard(chat_id)
        _last_start_by_chat_id[chat_id] = time.monotonic()


def _release_chat_send(chat_id: int) -> None:
    if START_RATE_LIMIT_SECONDS <= 0:
        return

    with _rate_limit_lock:
        _inflight_start_chat_ids.discard(chat_id)


def _parse_retry_after(response: urllib3.HTTPResponse, parsed: dict[str, Any]) -> int:
    retry_after = 1
    retry_header = response.headers.get("Retry-After")
    if retry_header and retry_header.isdigit():
        retry_after = max(1, int(retry_header))

    parameters = parsed.get("parameters")
    if isinstance(parameters, dict):
        maybe_retry = parameters.get("retry_after")
        if isinstance(maybe_retry, int):
            retry_after = max(1, maybe_retry)

    return retry_after


def send_message(chat_id: int, text: str) -> SendResult:
    payload = json.dumps({"chat_id": chat_id, "text": text}, ensure_ascii=False).encode("utf-8")

    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            response = tg_pool.urlopen(
                "POST",
                f"/bot{TOKEN}/sendMessage",
                body=payload,
                headers={"Content-Type": "application/json"},
                timeout=urllib3.Timeout(connect=2, read=3),
                redirect=False,
            )
        except urllib3.exceptions.ReadTimeoutError as exc:
            log.warning("sendMessage ambiguous read timeout: %s", _redact_secret(str(exc)))
            return SendResult.RETRYABLE_FAILURE
        except urllib3.exceptions.HTTPError as exc:
            log.warning("sendMessage transport error attempt=%s: %s", attempt, _redact_secret(str(exc)))
            if attempt == MAX_SEND_ATTEMPTS:
                return SendResult.RETRYABLE_FAILURE
            time.sleep(0.3 * attempt)
            continue
        except Exception as exc:
            log.warning("sendMessage unexpected error attempt=%s: %s", attempt, _redact_secret(str(exc)))
            if attempt == MAX_SEND_ATTEMPTS:
                return SendResult.RETRYABLE_FAILURE
            time.sleep(0.3 * attempt)
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

        if parsed.get("ok") is True:
            return SendResult.OK

        err_code = parsed.get("error_code")
        description = parsed.get("description")

        if status == 429 or err_code == 429:
            retry_after = _parse_retry_after(response, parsed)
            if retry_after <= SEND_RETRY_AFTER_LIMIT_SECONDS and attempt < MAX_SEND_ATTEMPTS:
                log.warning("sendMessage rate-limited attempt=%s retry_after=%ss", attempt, retry_after)
                time.sleep(retry_after)
                continue
            log.warning("sendMessage rate-limited; deferring to Telegram retry retry_after=%ss", retry_after)
            return SendResult.RETRYABLE_FAILURE

        if 500 <= status < 600:
            log.warning("sendMessage upstream 5xx status=%s attempt=%s", status, attempt)
            if attempt == MAX_SEND_ATTEMPTS:
                return SendResult.RETRYABLE_FAILURE
            time.sleep(0.5 * attempt)
            continue

        if err_code in (400, 403):
            log.info("sendMessage permanent reject error_code=%s description=%s", err_code, description)
            return SendResult.PERMANENT_FAILURE

        if status in (401, 404):
            log.error("sendMessage token/config error status=%s description=%s", status, description)
            return SendResult.RETRYABLE_FAILURE

        if status >= 400:
            log.warning(
                "sendMessage HTTP error status=%s error_code=%s description=%s",
                status,
                err_code,
                description,
            )
            return SendResult.RETRYABLE_FAILURE

        log.warning("sendMessage unexpected body status=%s body=%s", status, body_text[:500])
        return SendResult.RETRYABLE_FAILURE

    return SendResult.RETRYABLE_FAILURE


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

    retries = urllib3.Retry(connect=1, read=0, status=0, redirect=0, backoff_factor=0.3)
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
                redirect=False,
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
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            if claimed_update_id is not None:
                _commit_update(claimed_update_id)
                update_finalized = True
            return "ok", 200

        username = "-"
        from_user = message.get("from")
        if isinstance(from_user, dict):
            maybe_username = from_user.get("username")
            if isinstance(maybe_username, str) and maybe_username:
                username = maybe_username

        claimed_chat_send = _claim_chat_send(chat_id)
        chat_send_finalized = False
        if not claimed_chat_send:
            log.info("/start @%s (%s) -> throttled", username, chat_id)
            if claimed_update_id is not None:
                _commit_update(claimed_update_id)
                update_finalized = True
            return "ok", 200

        try:
            started_at = time.monotonic()
            result = send_message(chat_id, START_TEXT)
            elapsed_ms = (time.monotonic() - started_at) * 1000
            log.info("/start @%s (%s) -> %s %.0fms", username, chat_id, result.value, elapsed_ms)

            if result is SendResult.RETRYABLE_FAILURE:
                _release_chat_send(chat_id)
                chat_send_finalized = True
                if claimed_update_id is not None:
                    _release_update(claimed_update_id)
                    update_finalized = True
                return "retry", 503

            _commit_chat_send(chat_id)
            chat_send_finalized = True
            if claimed_update_id is not None:
                _commit_update(claimed_update_id)
                update_finalized = True
            return "ok", 200
        finally:
            if not chat_send_finalized:
                _release_chat_send(chat_id)
    finally:
        if claimed_update_id is not None and not update_finalized:
            _release_update(claimed_update_id)


@app.get("/health")
def health() -> tuple[dict[str, Any], int]:
    return {
        "status": "ok",
        "inflight_updates": len(_inflight_update_ids),
        "seen_updates": len(_seen_update_ids),
        "keepalive_enabled": KEEPALIVE_ENABLED,
    }, 200


_start_background_threads()
atexit.register(_shutdown_background_threads)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
