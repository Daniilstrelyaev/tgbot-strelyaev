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

DEFAULT_CHANNEL_URL = "https://t.me/strelyae_v"

TOKEN = os.environ["BOT_TOKEN"].strip()
WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"].strip()
PORT = int(os.environ.get("PORT", "8080"))
KEEPALIVE_ENABLED = os.environ.get("KEEPALIVE_ENABLED", "true").lower() in {"1", "true", "yes"}
KEEPALIVE_INTERVAL_SECONDS = int(os.environ.get("KEEPALIVE_INTERVAL_SECONDS", "240"))
MAX_SEEN_UPDATE_IDS = int(os.environ.get("MAX_SEEN_UPDATE_IDS", "10000"))
MAX_SEND_ATTEMPTS = int(os.environ.get("MAX_SEND_ATTEMPTS", "2"))
SEND_RETRY_AFTER_LIMIT_SECONDS = int(os.environ.get("SEND_RETRY_AFTER_LIMIT_SECONDS", "2"))
START_RATE_LIMIT_SECONDS = int(os.environ.get("START_RATE_LIMIT_SECONDS", "30"))
CALLBACK_RATE_LIMIT_SECONDS = int(os.environ.get("CALLBACK_RATE_LIMIT_SECONDS", "3"))

CHANNEL_ID_RAW = os.environ.get("CHANNEL_ID", "-1003726576543").strip()
try:
    CHANNEL_ID: int | str = int(CHANNEL_ID_RAW)
except ValueError:
    CHANNEL_ID = CHANNEL_ID_RAW

CHANNEL_URL = os.environ.get("CHANNEL_URL", DEFAULT_CHANNEL_URL).strip()
WELCOME_PHOTO_FILE_ID = os.environ.get("WELCOME_PHOTO_FILE_ID", "").strip()
APP_VERSION = os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("APP_VERSION", "unknown")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is empty")
if not WEBHOOK_SECRET:
    raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is empty")


def _validate_button_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("CHANNEL_URL must be an absolute http(s) URL")
    return value


CHANNEL_URL = _validate_button_url(CHANNEL_URL)

WELCOME_TEXT = (
    "Привет 👋\n\n"
    "Меня зовут Даниил — я создаю AI-контент\n"
    "и обучаю людей делать вирусные видео\n"
    "через нейросети и зарабатывать на этом.\n\n"
    "35 млн просмотров на AI-контенте.\n"
    "8,6 млн на одном ролике.\n"
    "61 500 подписчиков за 66 дней.\n\n"
    "В моём Telegram-канале регулярно выкладываю\n"
    "рабочие нейросети, связки и приёмы,\n"
    "которые помогут тебе делать вирусные видео\n"
    "и зарабатывать на AI-контенте.\n\n"
    "Подпишись на канал и жми кнопку 👇"
)

NOT_SUBSCRIBED_TEXT = (
    "Я не вижу твоей подписки 😔\n\n"
    "Подпишись на канал и нажми «Готово» —\n"
    "жду тебя внутри 🤍"
)

SUBSCRIBED_TEXT = (
    "Готово ✅\n\n"
    "Спасибо, что подписался!\n\n"
    "Вся польза — в канале.\n"
    "Контент выходит регулярно, не пропусти 🤍"
)

TEMPORARY_ERROR_TEXT = "Не получилось проверить подписку. Попробуй ещё раз через несколько секунд."
CALLBACK_THROTTLED_TEXT = "Секунду, уже проверяю."
GROUP_CONTEXT_TEXT = "Открой бота в личном чате и нажми /start."

WELCOME_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "✅ Я подписался", "callback_data": "check_sub"}],
        [{"text": "📢 Перейти на канал", "url": CHANNEL_URL}],
    ]
}
NOT_SUBSCRIBED_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "Готово ✅", "callback_data": "check_sub"}],
        [{"text": "📢 Перейти на канал", "url": CHANNEL_URL}],
    ]
}
SUBSCRIBED_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "📢 Открыть канал", "url": CHANNEL_URL}],
    ]
}

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

_callback_lock = threading.Lock()
_last_callback_by_user_id: dict[int, float] = {}
_inflight_callback_user_ids: set[int] = set()

_threads_started = False
_threads_lock = threading.Lock()


class SendResult(Enum):
    OK = "ok"
    PERMANENT_FAILURE = "permanent_failure"
    RETRYABLE_FAILURE = "retryable_failure"


class SubscriptionResult(Enum):
    SUBSCRIBED = "subscribed"
    NOT_SUBSCRIBED = "not_subscribed"
    UNKNOWN = "unknown"


def _redact_secret(value: str) -> str:
    return value.replace(TOKEN, "<BOT_TOKEN>")


def _safe_log_value(value: Any, max_len: int = 80) -> str:
    text = str(value)
    text = text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    if len(text) > max_len:
        return f"{text[:max_len]}..."
    return text


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


def _prune_mapping_locked(values: dict[int, float], now: float, ttl_seconds: int) -> None:
    if len(values) <= MAX_SEEN_UPDATE_IDS:
        return
    cutoff = now - ttl_seconds
    stale_keys = [key for key, timestamp in values.items() if timestamp < cutoff]
    for key in stale_keys:
        values.pop(key, None)


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
        _prune_mapping_locked(_last_start_by_chat_id, now, START_RATE_LIMIT_SECONDS)
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


def _claim_callback(user_id: int) -> bool:
    if CALLBACK_RATE_LIMIT_SECONDS <= 0:
        return True
    now = time.monotonic()
    with _callback_lock:
        last_seen = _last_callback_by_user_id.get(user_id)
        if last_seen is not None and now - last_seen < CALLBACK_RATE_LIMIT_SECONDS:
            return False
        if user_id in _inflight_callback_user_ids:
            return False
        _inflight_callback_user_ids.add(user_id)
        _prune_mapping_locked(_last_callback_by_user_id, now, CALLBACK_RATE_LIMIT_SECONDS)
        return True


def _commit_callback(user_id: int) -> None:
    if CALLBACK_RATE_LIMIT_SECONDS <= 0:
        return
    with _callback_lock:
        _inflight_callback_user_ids.discard(user_id)
        _last_callback_by_user_id[user_id] = time.monotonic()


def _release_callback(user_id: int) -> None:
    if CALLBACK_RATE_LIMIT_SECONDS <= 0:
        return
    with _callback_lock:
        _inflight_callback_user_ids.discard(user_id)


def _extract_retry_after(headers: Any, parsed: dict[str, Any]) -> int:
    retry_after = 1
    try:
        retry_header = headers.get("Retry-After")
    except AttributeError:
        retry_header = None
    if retry_header and str(retry_header).isdigit():
        retry_after = max(1, int(retry_header))
    parameters = parsed.get("parameters")
    if isinstance(parameters, dict):
        maybe = parameters.get("retry_after")
        if isinstance(maybe, int):
            retry_after = max(1, maybe)
    return retry_after


def _tg_api_post(
    method: str,
    payload: dict[str, Any],
    read_timeout: float = 3.0,
) -> tuple[int, Any, dict[str, Any], str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    response = tg_pool.urlopen(
        "POST",
        f"/bot{TOKEN}/{method}",
        body=body,
        headers={"Content-Type": "application/json"},
        timeout=urllib3.Timeout(connect=2, read=read_timeout),
        pool_timeout=2.0,
        redirect=False,
    )
    body_text = response.data.decode("utf-8", errors="replace")
    parsed: dict[str, Any] = {}
    try:
        candidate = json.loads(body_text)
        if isinstance(candidate, dict):
            parsed = candidate
    except json.JSONDecodeError:
        parsed = {}
    return response.status, response.headers, parsed, body_text


def _send_with_retries(
    api_method: str,
    payload: dict[str, Any],
    log_label: str,
    read_timeout: float = 3.0,
) -> SendResult:
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            status, headers, parsed, body_text = _tg_api_post(api_method, payload, read_timeout=read_timeout)
        except urllib3.exceptions.ReadTimeoutError as exc:
            log.warning("%s ambiguous read timeout: %s", log_label, _redact_secret(str(exc)))
            return SendResult.RETRYABLE_FAILURE
        except urllib3.exceptions.HTTPError as exc:
            log.warning("%s transport error attempt=%s: %s", log_label, attempt, _redact_secret(str(exc)))
            if attempt == MAX_SEND_ATTEMPTS:
                return SendResult.RETRYABLE_FAILURE
            time.sleep(0.3 * attempt)
            continue
        except Exception as exc:
            log.warning("%s unexpected error attempt=%s: %s", log_label, attempt, _redact_secret(str(exc)))
            if attempt == MAX_SEND_ATTEMPTS:
                return SendResult.RETRYABLE_FAILURE
            time.sleep(0.3 * attempt)
            continue

        if parsed.get("ok") is True:
            return SendResult.OK

        error_code = parsed.get("error_code")
        description = _redact_secret(str(parsed.get("description", "")))

        if status == 429 or error_code == 429:
            retry_after = _extract_retry_after(headers, parsed)
            if retry_after <= SEND_RETRY_AFTER_LIMIT_SECONDS and attempt < MAX_SEND_ATTEMPTS:
                log.warning("%s rate-limited attempt=%s retry_after=%ss", log_label, attempt, retry_after)
                time.sleep(retry_after)
                continue
            log.warning("%s rate-limited; deferring to Telegram retry retry_after=%ss", log_label, retry_after)
            return SendResult.RETRYABLE_FAILURE

        if 500 <= status < 600:
            log.warning("%s upstream 5xx status=%s attempt=%s", log_label, status, attempt)
            if attempt == MAX_SEND_ATTEMPTS:
                return SendResult.RETRYABLE_FAILURE
            time.sleep(0.5 * attempt)
            continue

        if 400 <= status < 500 or error_code in (400, 403):
            log.info("%s permanent failure status=%s error_code=%s description=%s",
                     log_label, status, error_code, description)
            return SendResult.PERMANENT_FAILURE

        log.warning("%s unexpected body status=%s body=%s",
                    log_label, status, _redact_secret(body_text[:500]))
        return SendResult.RETRYABLE_FAILURE

    return SendResult.RETRYABLE_FAILURE


def send_message(chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> SendResult:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _send_with_retries("sendMessage", payload, "sendMessage")


def send_photo(
    chat_id: int,
    photo: str,
    caption: str | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> SendResult:
    payload: dict[str, Any] = {"chat_id": chat_id, "photo": photo}
    if caption:
        payload["caption"] = caption
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _send_with_retries("sendPhoto", payload, "sendPhoto", read_timeout=5.0)


def send_welcome(chat_id: int) -> SendResult:
    if WELCOME_PHOTO_FILE_ID:
        result = send_photo(
            chat_id,
            WELCOME_PHOTO_FILE_ID,
            caption=WELCOME_TEXT,
            reply_markup=WELCOME_KEYBOARD,
        )
        if result is SendResult.PERMANENT_FAILURE:
            log.warning("sendPhoto failed permanently; falling back to text welcome")
            return send_message(chat_id, WELCOME_TEXT, reply_markup=WELCOME_KEYBOARD)
        return result
    return send_message(chat_id, WELCOME_TEXT, reply_markup=WELCOME_KEYBOARD)


def answer_callback_query(callback_id: str, text: str = "", show_alert: bool = False) -> bool:
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = show_alert
    try:
        _, _, parsed, _ = _tg_api_post("answerCallbackQuery", payload, read_timeout=3.0)
    except Exception as exc:
        log.warning("answerCallbackQuery failed: %s", _redact_secret(str(exc)))
        return False
    ok = parsed.get("ok") is True
    if not ok:
        log.info("answerCallbackQuery rejected: %s", _redact_secret(str(parsed.get("description", ""))))
    return ok


def check_subscription(user_id: int) -> SubscriptionResult:
    payload = {"chat_id": CHANNEL_ID, "user_id": user_id}
    try:
        status, _, parsed, _ = _tg_api_post("getChatMember", payload, read_timeout=4.0)
    except Exception as exc:
        log.warning("getChatMember failed user_id=%s: %s", user_id, _redact_secret(str(exc)))
        return SubscriptionResult.UNKNOWN

    if status >= 500 or parsed.get("error_code") == 429:
        return SubscriptionResult.UNKNOWN
    if parsed.get("ok") is not True:
        log.warning(
            "getChatMember rejected status=%s error_code=%s description=%s",
            status,
            parsed.get("error_code"),
            _redact_secret(str(parsed.get("description", ""))),
        )
        return SubscriptionResult.UNKNOWN

    result = parsed.get("result")
    if not isinstance(result, dict):
        return SubscriptionResult.UNKNOWN

    member_status = result.get("status")
    if member_status in {"creator", "administrator", "member"}:
        return SubscriptionResult.SUBSCRIBED
    if member_status == "restricted" and result.get("is_member") is True:
        return SubscriptionResult.SUBSCRIBED
    if member_status in {"left", "kicked", "restricted"}:
        return SubscriptionResult.NOT_SUBSCRIBED
    return SubscriptionResult.UNKNOWN


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
                pool_timeout=2.0,
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


def _commit_if_claimed(update_id: int | None) -> bool:
    if update_id is None:
        return True
    _commit_update(update_id)
    return True


def _handle_message(update: dict[str, Any], claimed_update_id: int | None) -> tuple[str, int, bool]:
    message = update.get("message")
    if not isinstance(message, dict):
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    text = message.get("text")
    chat = message.get("chat")
    if not isinstance(text, str) or not isinstance(chat, dict):
        return "ok", 200, _commit_if_claimed(claimed_update_id)
    if not _is_start_command(text):
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    chat_id = chat.get("id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    chat_type = chat.get("type")
    if chat_type != "private":
        from_user = message.get("from")
        user_id = from_user.get("id") if isinstance(from_user, dict) else None
        if isinstance(user_id, int) and not isinstance(user_id, bool):
            answer_result = send_message(user_id, GROUP_CONTEXT_TEXT)
            if answer_result is SendResult.RETRYABLE_FAILURE:
                return "retry", 503, False
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    username = "-"
    from_user = message.get("from")
    if isinstance(from_user, dict):
        maybe_username = from_user.get("username")
        if isinstance(maybe_username, str) and maybe_username:
            username = _safe_log_value(maybe_username)

    claimed_chat_send = _claim_chat_send(chat_id)
    chat_send_finalized = False
    if not claimed_chat_send:
        log.info("/start @%s (%s) -> throttled", username, chat_id)
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    try:
        started_at = time.monotonic()
        result = send_welcome(chat_id)
        elapsed_ms = (time.monotonic() - started_at) * 1000
        log.info("/start @%s (%s) -> %s %.0fms", username, chat_id, result.value, elapsed_ms)

        if result is SendResult.RETRYABLE_FAILURE:
            _release_chat_send(chat_id)
            chat_send_finalized = True
            return "retry", 503, False

        _commit_chat_send(chat_id)
        chat_send_finalized = True
        return "ok", 200, _commit_if_claimed(claimed_update_id)
    finally:
        if not chat_send_finalized:
            _release_chat_send(chat_id)


def _handle_callback_query(callback_query: dict[str, Any], claimed_update_id: int | None) -> tuple[str, int, bool]:
    callback_id = callback_query.get("id")
    from_user = callback_query.get("from")
    data = callback_query.get("data")

    if not isinstance(callback_id, str) or not isinstance(from_user, dict):
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    user_id = from_user.get("id")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        answer_callback_query(callback_id, "Не получилось определить пользователя.", show_alert=True)
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    username = "-"
    maybe_username = from_user.get("username")
    if isinstance(maybe_username, str) and maybe_username:
        username = _safe_log_value(maybe_username)

    if data != "check_sub":
        answer_callback_query(callback_id)
        log.info("callback @%s (%s) -> ignored data=%s", username, user_id, _safe_log_value(data))
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    claimed_callback = _claim_callback(user_id)
    callback_finalized = False
    if not claimed_callback:
        answer_callback_query(callback_id, CALLBACK_THROTTLED_TEXT)
        log.info("callback @%s (%s) -> throttled", username, user_id)
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    try:
        subscription = check_subscription(user_id)
        if subscription is SubscriptionResult.UNKNOWN:
            answer_callback_query(callback_id, TEMPORARY_ERROR_TEXT, show_alert=True)
            _release_callback(user_id)
            callback_finalized = True
            log.warning("callback @%s (%s) -> subscription_unknown", username, user_id)
            return "ok", 200, _commit_if_claimed(claimed_update_id)

        if subscription is SubscriptionResult.SUBSCRIBED:
            answer_callback_query(callback_id, "Готово ✅")
            result = send_message(user_id, SUBSCRIBED_TEXT, reply_markup=SUBSCRIBED_KEYBOARD)
            outcome = "subscribed"
        else:
            answer_callback_query(callback_id, "Подписку пока не вижу", show_alert=True)
            result = send_message(user_id, NOT_SUBSCRIBED_TEXT, reply_markup=NOT_SUBSCRIBED_KEYBOARD)
            outcome = "not_subscribed"

        log.info("callback @%s (%s) -> %s send=%s", username, user_id, outcome, result.value)

        if result is SendResult.RETRYABLE_FAILURE:
            _release_callback(user_id)
            callback_finalized = True
            return "retry", 503, False

        _commit_callback(user_id)
        callback_finalized = True
        return "ok", 200, _commit_if_claimed(claimed_update_id)
    finally:
        if not callback_finalized:
            _release_callback(user_id)


@app.post("/webhook")
def webhook() -> tuple[str, int]:
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(secret, WEBHOOK_SECRET):
        abort(403)

    if not request.is_json:
        abort(400)

    update = request.get_json(silent=True)
    if not isinstance(update, dict):
        abort(400)

    update_id_value = update.get("update_id")
    if not isinstance(update_id_value, int) or isinstance(update_id_value, bool):
        log.warning("dropping malformed update without valid update_id")
        return "ok", 200

    if not _claim_update(update_id_value):
        return "ok", 200

    finalized = False
    try:
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            body, status, finalized = _handle_callback_query(callback_query, update_id_value)
            return body, status

        body, status, finalized = _handle_message(update, update_id_value)
        return body, status
    finally:
        if not finalized:
            _release_update(update_id_value)


@app.get("/health")
def health() -> tuple[dict[str, Any], int]:
    with _updates_lock:
        inflight_updates = len(_inflight_update_ids)
        seen_updates = len(_seen_update_ids)
    with _rate_limit_lock:
        tracked_chats = len(_last_start_by_chat_id)
        inflight_chats = len(_inflight_start_chat_ids)
    with _callback_lock:
        tracked_callback_users = len(_last_callback_by_user_id)
        inflight_callback_users = len(_inflight_callback_user_ids)

    return {
        "status": "ok",
        "version": APP_VERSION,
        "inflight_updates": inflight_updates,
        "seen_updates": seen_updates,
        "tracked_chats": tracked_chats,
        "inflight_chats": inflight_chats,
        "tracked_callback_users": tracked_callback_users,
        "inflight_callback_users": inflight_callback_users,
        "keepalive_enabled": KEEPALIVE_ENABLED,
        "channel_id": CHANNEL_ID,
        "welcome_photo": bool(WELCOME_PHOTO_FILE_ID),
    }, 200


_start_background_threads()
atexit.register(_shutdown_background_threads)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
