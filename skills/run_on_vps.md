# 🔄 Как перезапустить / запустить бота

> ВАЖНО: сейчас бот живёт на **Render** (не на VPS). Перезапуск там — в части 1.
> Часть 2 — на будущее, если решишь переехать на свой сервер (VPS).

---

## Часть 1. Render (как сейчас)

Бот перезапускается сам при любом `git push`. Но можно и вручную.

### Перезапустить вручную
1. Зайди на https://dashboard.render.com → сервис `tgbot-strelyaev`.
2. Кнопка **Manual Deploy** (справа сверху) → **Deploy latest commit**
   (или **Clear build cache & deploy**, если что-то залипло).

### Посмотреть, живой ли бот
Открой в браузере: https://tgbot-strelyaev.onrender.com/health
Должен ответить `{"status":"ok", ...}`.

### Посмотреть логи (если бот молчит)
Render → сервис → слева **Logs**. Ищи строки с `ERROR`.

### После смены переменных окружения
Render → **Environment** → поменял → **Save, rebuild and deploy**.

---

## Часть 2. Свой VPS (на будущее)

Если переедешь на сервер (Ubuntu). Кратко, по шагам.

### Первый запуск
```bash
# 1. поставить Python и git
sudo apt update && sudo apt install -y python3 python3-venv git

# 2. забрать код
git clone https://github.com/Daniilstrelyaev/tgbot-strelyaev.git
cd tgbot-strelyaev

# 3. окружение и зависимости
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. переменные (вставь свои значения!)
export BOT_TOKEN="..."
export TELEGRAM_WEBHOOK_SECRET="..."
export ADMIN_ID="..."
export LEAD_MAGNET_PATH="lead_magnet.pdf"

# 5. запустить
gunicorn app:app --bind 0.0.0.0:8080 --workers 1 --worker-class gthread --threads 4
```

### Чтобы работал постоянно (даже после перезагрузки)
Проще всего — `systemd`-сервис или `screen`/`tmux`. Самый простой вариант на старте:
```bash
# запустить в фоне в отдельной сессии
sudo apt install -y tmux
tmux new -s bot
# внутри tmux запусти gunicorn (шаг 5 выше), потом нажми Ctrl+B затем D — выйти
```

### Перезапустить на VPS
```bash
tmux attach -t bot     # вернуться в сессию
# Ctrl+C — остановить, потом снова запустить gunicorn
```

### Плюс VPS перед Render
- `leads.csv` **не стирается** (постоянный диск).
- Бот не «засыпает».

> Если решишь переезжать — скажи ИИ-помощнику «переносим бота с Render на VPS»,
> и дай ему этот файл + `context/BOT_OVERVIEW.md`.
