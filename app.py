from __future__ import annotations

import atexit
import csv
import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from collections import deque
from typing import Any
from urllib.parse import urlparse

import certifi
import urllib3
from urllib3.filepost import encode_multipart_formdata
from flask import Flask, abort, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DEFAULT_CHANNEL_URL = "https://t.me/strelyae_v"

# ── Основные настройки (берутся из переменных окружения на Render) ────────────
TOKEN = os.environ["BOT_TOKEN"].strip()                       # токен бота от BotFather
WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"].strip()  # секрет для защиты webhook
PORT = int(os.environ.get("PORT", "8080"))
KEEPALIVE_ENABLED = os.environ.get("KEEPALIVE_ENABLED", "true").lower() in {"1", "true", "yes"}
KEEPALIVE_INTERVAL_SECONDS = int(os.environ.get("KEEPALIVE_INTERVAL_SECONDS", "240"))
MAX_SEEN_UPDATE_IDS = int(os.environ.get("MAX_SEEN_UPDATE_IDS", "10000"))
MAX_SEND_ATTEMPTS = int(os.environ.get("MAX_SEND_ATTEMPTS", "2"))
SEND_RETRY_AFTER_LIMIT_SECONDS = int(os.environ.get("SEND_RETRY_AFTER_LIMIT_SECONDS", "2"))
START_RATE_LIMIT_SECONDS = int(os.environ.get("START_RATE_LIMIT_SECONDS", "30"))
CALLBACK_RATE_LIMIT_SECONDS = int(os.environ.get("CALLBACK_RATE_LIMIT_SECONDS", "3"))

# ── ID канала, на подписку которого проверяем ────────────────────────────────
CHANNEL_ID_RAW = os.environ.get("CHANNEL_ID", "-1003726576543").strip()
try:
    CHANNEL_ID: int | str = int(CHANNEL_ID_RAW)
except ValueError:
    CHANNEL_ID = CHANNEL_ID_RAW

CHANNEL_URL = os.environ.get("CHANNEL_URL", DEFAULT_CHANNEL_URL).strip()
WELCOME_PHOTO_FILE_ID = os.environ.get("WELCOME_PHOTO_FILE_ID", "").strip()  # фото в приветствии

# ── ЛИД-МАГНИТ (PDF-гайд) ─────────────────────────────────────────────────────
# Если задан LEAD_MAGNET_FILE_ID — бот шлёт файл по нему мгновенно (рекомендуется).
# Иначе бот возьмёт файл с диска по пути LEAD_MAGNET_PATH (файл лежит в папке проекта).
LEAD_MAGNET_FILE_ID = os.environ.get("LEAD_MAGNET_FILE_ID", "").strip()
LEAD_MAGNET_PATH = os.environ.get("LEAD_MAGNET_PATH", "lead_magnet.pdf").strip()

# ── АДМИН (куда падают заявки на разбор) ──────────────────────────────────────
# Узнать свой ID: напиши боту команду /id — он пришлёт твой числовой ID.
# Потом добавь его на Render как переменную ADMIN_ID.
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "").strip()
try:
    ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW else 0
except ValueError:
    ADMIN_ID = 0

# Куда сохранять заявки локально (на Render файл не вечный — основной канал это ЛС админу)
LEADS_CSV_PATH = os.environ.get("LEADS_CSV_PATH", "leads.csv").strip()

# Сколько живёт незавершённая анкета (после — сбрасывается)
FSM_TTL_SECONDS = int(os.environ.get("FSM_TTL_SECONDS", "1800"))  # 30 минут

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

# ── ТЕКСТЫ СООБЩЕНИЙ (меняй здесь) ────────────────────────────────────────────
WELCOME_TEXT = (
    "Привет 👋\n\n"
    "Меня зовут Даниил — я создаю AI-контент\n"
    "и обучаю людей делать вирусные видео\n"
    "через нейросети и зарабатывать на этом.\n\n"
    "35 млн просмотров на AI-контенте.\n"
    "8,6 млн на одном ролике.\n"
    "61 500 подписчиков за 66 дней.\n\n"
    "Подготовил для тебя бесплатный гайд\n"
    "«Оплата нейросетей без хаоса» —\n"
    "внутри все сервисы, ссылки и как платить из РФ.\n\n"
    "Чтобы забрать гайд — подпишись на канал\n"
    "и нажми кнопку ниже 👇"
)

NOT_SUBSCRIBED_TEXT = (
    "Я не вижу твоей подписки 😔\n\n"
    "Подпишись на канал и нажми «Готово» —\n"
    "и я сразу пришлю гайд 🤍"
)

# Текст-подпись к PDF-гайду (блок 1)
LEAD_MAGNET_CAPTION = (
    "Держи гайд по оплате нейросетей из РФ 🎬\n\n"
    "Внутри — все сервисы, ссылки и как платить без хаоса.\n\n"
    "Когда подготовишь доступы — переходи к следующему шагу."
)

MENU_TEXT = "Выбери, что нужно 👇"
REVIEW_INTRO_TEXT = "Окей, погнали 🔥\nОтветь на 4 коротких вопроса."
REVIEW_DONE_TEXT = "Спасибо! Свяжусь с тобой и назначим разбор 🔥"
CANCEL_TEXT = "Ок, отменил. Если что — жми /menu."

TEMPORARY_ERROR_TEXT = "Не получилось проверить подписку. Попробуй ещё раз через несколько секунд."
CALLBACK_THROTTLED_TEXT = "Секунду, уже обрабатываю."
GROUP_CONTEXT_TEXT = "Открой бота в личном чате и нажми /start."

# ── АНКЕТА «РАЗБОР» (вопросы по порядку) ──────────────────────────────────────
REVIEW_QUESTIONS = [
    "1/4. Как тебя зовут?",
    "2/4. Чем занимаешься / какая у тебя ниша?",
    "3/4. Дай ссылку на свой профиль (Instagram / TikTok / YouTube / сайт).",
    "4/4. Как с тобой связаться? (телеграм @username или телефон)",
]
REVIEW_FIELDS = ["name", "niche", "profile", "contact"]

# ── КЛАВИАТУРЫ (кнопки) ───────────────────────────────────────────────────────
WELCOME_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "✅ Я подписался", "callback_data": "check_sub"}],
        [{"text": "📣 Перейти на канал", "url": CHANNEL_URL}],
    ]
}
NOT_SUBSCRIBED_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "Готово ✅", "callback_data": "check_sub"}],
        [{"text": "📣 Перейти на канал", "url": CHANNEL_URL}],
    ]
}
MENU_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "📥 Забрать гайд", "callback_data": "get_guide"}],
        [{"text": "🎯 Хочу разбор", "callback_data": "want_review"}],
        [{"text": "📣 Канал", "url": CHANNEL_URL}],
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

# Состояние анкеты «разбор»: user_id -> {"step": int, "answers": {...}, "ts": float}
_fsm_lock = threading.Lock()
_fsm_state: dict[int, dict[str, Any]] = {}

# Защита от одновременной записи в CSV
_csv_lock = threading.Lock()

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


# ── Дедупликация update_id ────────────────────────────────────────────────────
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


# ── Разбор команд и ключевых слов ─────────────────────────────────────────────
def _parse_command(text: str) -> str | None:
    """Возвращает команду вида '/start' или None."""
    if not text:
        return None
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return None
    token = parts[0]
    if not token.startswith("/"):
        return None
    return token.split("@", 1)[0].lower()


def _message_tokens(text: str) -> set[str]:
    """Слова сообщения в нижнем регистре (для ловли ключевых слов)."""
    return set(re.findall(r"\w+", text.lower()))


# ── Лимиты частоты ────────────────────────────────────────────────────────────
def _prune_mapping_locked(values: dict[int, float], now: float, ttl_seconds: int) -> None:
    if len(values) <= MAX_SEEN_UPDATE_IDS:
        return
    cutoff = now - ttl_seconds
    stale_keys = [key for key, ts in values.items() if ts < cutoff]
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


def _claim_action(user_id: int) -> bool:
    """Общий лимит на действия пользователя (кнопки и ключевые слова): 1 раз в N сек."""
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


def _commit_action(user_id: int) -> None:
    if CALLBACK_RATE_LIMIT_SECONDS <= 0:
        return
    with _callback_lock:
        _inflight_callback_user_ids.discard(user_id)
        _last_callback_by_user_id[user_id] = time.monotonic()


def _release_action(user_id: int) -> None:
    if CALLBACK_RATE_LIMIT_SECONDS <= 0:
        return
    with _callback_lock:
        _inflight_callback_user_ids.discard(user_id)


# ── Анкета «разбор» (FSM) ─────────────────────────────────────────────────────
def _clear_fsm(user_id: int) -> None:
    with _fsm_lock:
        _fsm_state.pop(user_id, None)


def _fsm_is_active(user_id: int) -> bool:
    now = time.monotonic()
    with _fsm_lock:
        st = _fsm_state.get(user_id)
        if st is None:
            return False
        if now - st["ts"] > FSM_TTL_SECONDS:
            _fsm_state.pop(user_id, None)
            return False
        return True


def _start_review(user_id: int) -> None:
    now = time.monotonic()
    with _fsm_lock:
        _fsm_state[user_id] = {"step": 0, "answers": {}, "ts": now}
    send_message(user_id, f"{REVIEW_INTRO_TEXT}\n\n{REVIEW_QUESTIONS[0]}")


def _process_review_answer(user_id: int, text: str, username: str) -> None:
    """Сохраняет ответ и задаёт следующий вопрос либо завершает анкету."""
    answer = text.strip()[:500]  # ограничиваем длину ответа
    finished_answers: dict[str, str] | None = None
    next_question: str | None = None

    with _fsm_lock:
        st = _fsm_state.get(user_id)
        if st is None:
            return
        step = st["step"]
        st["answers"][REVIEW_FIELDS[step]] = answer
        st["ts"] = time.monotonic()
        step += 1
        if step < len(REVIEW_QUESTIONS):
            st["step"] = step
            next_question = REVIEW_QUESTIONS[step]
        else:
            finished_answers = dict(st["answers"])
            _fsm_state.pop(user_id, None)

    if next_question is not None:
        send_message(user_id, next_question)
    elif finished_answers is not None:
        _finish_review(user_id, username, finished_answers)


def _finish_review(user_id: int, username: str, answers: dict[str, str]) -> None:
    _send_lead_to_admin(user_id, username, answers)
    _save_lead_csv(user_id, username, answers)
    send_message(user_id, REVIEW_DONE_TEXT, reply_markup=MENU_KEYBOARD)


def _send_lead_to_admin(user_id: int, username: str, answers: dict[str, str]) -> None:
    if not ADMIN_ID:
        log.warning("ADMIN_ID not set — заявка не отправлена в ЛС: %s", _safe_log_value(answers, 200))
        return
    text = (
        "🎯 Новая заявка на разбор\n\n"
        f"Имя: {answers.get('name', '-')}\n"
        f"Ниша: {answers.get('niche', '-')}\n"
        f"Профиль: {answers.get('profile', '-')}\n"
        f"Связь: {answers.get('contact', '-')}\n\n"
        f"Telegram: @{username} (id {user_id})"
    )
    send_message(ADMIN_ID, text)


def _save_lead_csv(user_id: int, username: str, answers: dict[str, str]) -> None:
    try:
        with _csv_lock:
            file_exists = os.path.exists(LEADS_CSV_PATH)
            with open(LEADS_CSV_PATH, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(
                        ["datetime_utc", "user_id", "username", "name", "niche", "profile", "contact"]
                    )
                writer.writerow([
                    datetime.now(timezone.utc).isoformat(),
                    user_id, username,
                    answers.get("name", ""), answers.get("niche", ""),
                    answers.get("profile", ""), answers.get("contact", ""),
                ])
    except OSError as exc:
        log.error("не смог записать leads.csv: %s", exc)


# ── Низкоуровневые вызовы Telegram API ────────────────────────────────────────
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
    payload: dict[str, Any] | None = None,
    read_timeout: float = 3.0,
    multipart_fields: dict[str, Any] | None = None,
) -> tuple[int, Any, dict[str, Any], str]:
    """Один POST-запрос к Telegram. JSON или multipart (для загрузки файла)."""
    if multipart_fields is not None:
        body, content_type = encode_multipart_formdata(multipart_fields)
        req_headers = {"Content-Type": content_type}
    else:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers = {"Content-Type": "application/json"}

    response = tg_pool.urlopen(
        "POST",
        f"/bot{TOKEN}/{method}",
        body=body,
        headers=req_headers,
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
    payload: dict[str, Any] | None = None,
    log_label: str = "",
    read_timeout: float = 3.0,
    multipart_fields: dict[str, Any] | None = None,
) -> SendResult:
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            status, headers, parsed, body_text = _tg_api_post(
                api_method, payload=payload, read_timeout=read_timeout, multipart_fields=multipart_fields
            )
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
            log.warning("%s rate-limited; deferring retry_after=%ss", log_label, retry_after)
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

        log.warning("%s unexpected body status=%s body=%s", log_label, status, _redact_secret(body_text[:500]))
        return SendResult.RETRYABLE_FAILURE

    return SendResult.RETRYABLE_FAILURE


def send_message(chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> SendResult:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _send_with_retries("sendMessage", payload=payload, log_label="sendMessage")


def send_photo(chat_id: int, photo: str, caption: str | None = None,
               reply_markup: dict[str, Any] | None = None) -> SendResult:
    payload: dict[str, Any] = {"chat_id": chat_id, "photo": photo}
    if caption:
        payload["caption"] = caption
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _send_with_retries("sendPhoto", payload=payload, log_label="sendPhoto", read_timeout=5.0)


def send_document(chat_id: int, caption: str | None = None) -> SendResult:
    """Отправляет PDF-гайд: по file_id (быстро) или загрузкой файла с диска."""
    # Вариант 1 — по file_id (мгновенно, рекомендуется)
    if LEAD_MAGNET_FILE_ID:
        payload: dict[str, Any] = {"chat_id": chat_id, "document": LEAD_MAGNET_FILE_ID}
        if caption:
            payload["caption"] = caption
        return _send_with_retries("sendDocument", payload=payload, log_label="sendDocument", read_timeout=10.0)

    # Вариант 2 — загрузка файла с диска по пути LEAD_MAGNET_PATH
    if LEAD_MAGNET_PATH and os.path.exists(LEAD_MAGNET_PATH):
        try:
            with open(LEAD_MAGNET_PATH, "rb") as f:
                file_bytes = f.read()
        except OSError as exc:
            log.error("не смог прочитать лид-магнит %s: %s", LEAD_MAGNET_PATH, exc)
            return SendResult.PERMANENT_FAILURE
        filename = os.path.basename(LEAD_MAGNET_PATH)
        fields: dict[str, Any] = {
            "chat_id": str(chat_id),
            "document": (filename, file_bytes, "application/pdf"),
        }
        if caption:
            fields["caption"] = caption
        return _send_with_retries("sendDocument", log_label="sendDocument",
                                  read_timeout=20.0, multipart_fields=fields)

    log.error("лид-магнит не настроен (нет file_id и нет файла %s)", LEAD_MAGNET_PATH)
    return SendResult.PERMANENT_FAILURE


def send_welcome(chat_id: int) -> SendResult:
    if WELCOME_PHOTO_FILE_ID:
        result = send_photo(chat_id, WELCOME_PHOTO_FILE_ID, caption=WELCOME_TEXT, reply_markup=WELCOME_KEYBOARD)
        if result is SendResult.PERMANENT_FAILURE:
            log.warning("sendPhoto failed permanently; fallback to text welcome")
            return send_message(chat_id, WELCOME_TEXT, reply_markup=WELCOME_KEYBOARD)
        return result
    return send_message(chat_id, WELCOME_TEXT, reply_markup=WELCOME_KEYBOARD)


def answer_callback_query(callback_id: str, text: str = "", show_alert: bool = False) -> bool:
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = show_alert
    try:
        _, _, parsed, _ = _tg_api_post("answerCallbackQuery", payload=payload, read_timeout=3.0)
    except Exception as exc:
        log.warning("answerCallbackQuery failed: %s", _redact_secret(str(exc)))
        return False
    return parsed.get("ok") is True


def check_subscription(user_id: int) -> SubscriptionResult:
    payload = {"chat_id": CHANNEL_ID, "user_id": user_id}
    try:
        status, _, parsed, _ = _tg_api_post("getChatMember", payload=payload, read_timeout=4.0)
    except Exception as exc:
        log.warning("getChatMember failed user_id=%s: %s", user_id, _redact_secret(str(exc)))
        return SubscriptionResult.UNKNOWN

    if status >= 500 or parsed.get("error_code") == 429:
        return SubscriptionResult.UNKNOWN
    if parsed.get("ok") is not True:
        log.warning("getChatMember rejected status=%s error_code=%s description=%s",
                    status, parsed.get("error_code"), _redact_secret(str(parsed.get("description", ""))))
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


def _deliver_guide(user_id: int, callback_id: str | None = None) -> str:
    """Проверяет подписку и выдаёт PDF-гайд. Возвращает строку-итог для логов."""
    subscription = check_subscription(user_id)

    if subscription is SubscriptionResult.UNKNOWN:
        if callback_id:
            answer_callback_query(callback_id, TEMPORARY_ERROR_TEXT, show_alert=True)
        else:
            send_message(user_id, TEMPORARY_ERROR_TEXT)
        return "unknown"

    if subscription is SubscriptionResult.SUBSCRIBED:
        if callback_id:
            answer_callback_query(callback_id, "Готово ✅")
        send_document(user_id, caption=LEAD_MAGNET_CAPTION)
        return "subscribed"

    # не подписан
    if callback_id:
        answer_callback_query(callback_id, "Подписку пока не вижу", show_alert=True)
    send_message(user_id, NOT_SUBSCRIBED_TEXT, reply_markup=NOT_SUBSCRIBED_KEYBOARD)
    return "not_subscribed"


# ── Keepalive (не даём Render-free уснуть) ───────────────────────────────────
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
            host, port=port, maxsize=1,
            cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(), retries=retries,
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
                "GET", health_path,
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


# ── Обработка входящих сообщений ──────────────────────────────────────────────
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

    chat_id = chat.get("id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    # узнаём, кто пишет
    from_user = message.get("from")
    user_id = chat_id
    if isinstance(from_user, dict):
        maybe_uid = from_user.get("id")
        if isinstance(maybe_uid, int) and not isinstance(maybe_uid, bool):
            user_id = maybe_uid
    username = "-"
    if isinstance(from_user, dict):
        maybe_username = from_user.get("username")
        if isinstance(maybe_username, str) and maybe_username:
            username = _safe_log_value(maybe_username)

    cmd = _parse_command(text)
    chat_type = chat.get("type")

    # В группах бот не ведёт диалог — только подсказывает зайти в личку
    if chat_type != "private":
        if cmd == "/start":
            send_message(user_id, GROUP_CONTEXT_TEXT)
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    # ── Команды ──
    if cmd == "/start":
        _clear_fsm(user_id)
        if not _claim_chat_send(chat_id):
            log.info("/start @%s (%s) -> throttled", username, chat_id)
            return "ok", 200, _commit_if_claimed(claimed_update_id)
        chat_send_finalized = False
        try:
            result = send_welcome(chat_id)
            log.info("/start @%s (%s) -> %s", username, chat_id, result.value)
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

    if cmd == "/menu":
        _clear_fsm(user_id)
        send_message(chat_id, MENU_TEXT, reply_markup=MENU_KEYBOARD)
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    if cmd == "/id":
        send_message(chat_id, f"Твой ID: {user_id}\n(добавь его на Render как ADMIN_ID)")
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    if cmd == "/cancel":
        _clear_fsm(user_id)
        send_message(chat_id, CANCEL_TEXT)
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    # ── Если идёт анкета «разбор» — это ответ на вопрос ──
    if _fsm_is_active(user_id):
        _process_review_answer(user_id, text, username)
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    # ── Ключевые слова ──
    tokens = _message_tokens(text)
    if "связка" in tokens or "гайд" in tokens:
        if not _claim_action(user_id):
            return "ok", 200, _commit_if_claimed(claimed_update_id)
        try:
            outcome = _deliver_guide(user_id)
            log.info("keyword guide @%s (%s) -> %s", username, user_id, outcome)
            if outcome == "unknown":
                _release_action(user_id)
            else:
                _commit_action(user_id)
        except Exception:
            _release_action(user_id)
            raise
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    if "разбор" in tokens:
        if not _claim_action(user_id):
            return "ok", 200, _commit_if_claimed(claimed_update_id)
        try:
            _start_review(user_id)
            log.info("keyword review @%s (%s) -> started", username, user_id)
            _commit_action(user_id)
        except Exception:
            _release_action(user_id)
            raise
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    # ничего не подошло — молчим
    return "ok", 200, _commit_if_claimed(claimed_update_id)


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

    if data not in {"check_sub", "get_guide", "want_review"}:
        answer_callback_query(callback_id)
        log.info("callback @%s (%s) -> ignored data=%s", username, user_id, _safe_log_value(data))
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    if not _claim_action(user_id):
        answer_callback_query(callback_id, CALLBACK_THROTTLED_TEXT)
        log.info("callback @%s (%s) -> throttled", username, user_id)
        return "ok", 200, _commit_if_claimed(claimed_update_id)

    callback_finalized = False
    try:
        if data in ("check_sub", "get_guide"):
            outcome = _deliver_guide(user_id, callback_id)
            log.info("callback @%s (%s) data=%s -> %s", username, user_id, data, outcome)
            if outcome == "unknown":
                _release_action(user_id)   # дадим повторить сразу
                callback_finalized = True
                return "ok", 200, _commit_if_claimed(claimed_update_id)
            _commit_action(user_id)
            callback_finalized = True
            return "ok", 200, _commit_if_claimed(claimed_update_id)

        if data == "want_review":
            answer_callback_query(callback_id)
            _clear_fsm(user_id)
            _start_review(user_id)
            log.info("callback @%s (%s) -> review started", username, user_id)
            _commit_action(user_id)
            callback_finalized = True
            return "ok", 200, _commit_if_claimed(claimed_update_id)

        return "ok", 200, _commit_if_claimed(claimed_update_id)
    finally:
        if not callback_finalized:
            _release_action(user_id)


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
    with _callback_lock:
        tracked_callback_users = len(_last_callback_by_user_id)
    with _fsm_lock:
        fsm_active = len(_fsm_state)

    return {
        "status": "ok",
        "version": APP_VERSION,
        "inflight_updates": inflight_updates,
        "seen_updates": seen_updates,
        "tracked_chats": tracked_chats,
        "tracked_callback_users": tracked_callback_users,
        "fsm_active": fsm_active,
        "keepalive_enabled": KEEPALIVE_ENABLED,
        "channel_id": CHANNEL_ID,
        "welcome_photo": bool(WELCOME_PHOTO_FILE_ID),
        "lead_magnet": bool(LEAD_MAGNET_FILE_ID) or os.path.exists(LEAD_MAGNET_PATH),
        "admin_set": bool(ADMIN_ID),
    }, 200


_start_background_threads()
atexit.register(_shutdown_background_threads)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
