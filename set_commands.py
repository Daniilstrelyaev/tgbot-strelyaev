"""
Регистрирует команды бота (их показывает синяя кнопка «Меню» в Telegram).
Запуск:
  BOT_TOKEN=xxx python3 set_commands.py
"""
import os, ssl, json, urllib.request

TOKEN = os.environ["BOT_TOKEN"]

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

COMMANDS = [
    {"command": "start", "description": "Запустить бота и забрать гайд"},
    {"command": "menu", "description": "Меню: гайд, разбор, канал"},
    {"command": "cancel", "description": "Отменить анкету разбора"},
]


def tg(method, data):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    for _ in range(8):
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
                return json.loads(r.read())
        except Exception:
            pass
    return {}


print("setMyCommands:", tg("setMyCommands", {"commands": COMMANDS}))
# Кнопка «Меню» показывает список команд
print("setChatMenuButton:", tg("setChatMenuButton", {"menu_button": {"type": "commands"}}))
print("getMyCommands:", tg("getMyCommands", {}))
