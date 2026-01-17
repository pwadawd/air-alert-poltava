import os
import re
import asyncio
import base64
import time
import html
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask
from telethon import TelegramClient, events

# ---------------- ENV ----------------
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]

# куда постить (можно @username без @, или -100... id)
TARGET = os.environ["TG_TARGET"]

# откуда читать (публичный канал, username без @)
SOURCE_CHANNEL = os.environ.get("TG_SOURCE", "air_alert_ua")

# base64 от бинарного session.session (сделанного локально)
TG_SESSION_B64 = os.environ.get("TG_SESSION_B64", "").strip()

UA_TZ = timezone(timedelta(hours=2))  # зимой +2, летом будет +3, но для текста не критично

# ---------------- KEEPALIVE WEB (Render Web Service) ----------------
app = Flask(__name__)

@app.get("/")
def home():
    return "OK"

def run_web():
    port = int(os.environ.get("PORT", "10000"))
    # Render смотрит на PORT
    app.run(host="0.0.0.0", port=port)

# ---------------- SESSION RESTORE ----------------
SESSION_PATH = "session.session"

def ensure_session_file():
    """
    Восстанавливает session.session из TG_SESSION_B64.
    НИКАКОГО UTF-8 decode — это бинарь.
    """
    if not TG_SESSION_B64:
        raise RuntimeError("TG_SESSION_B64 is empty. Put base64 from session.session into env.")

    try:
        raw = base64.b64decode(TG_SESSION_B64, validate=True)
    except Exception as e:
        raise RuntimeError(f"TG_SESSION_B64 base64 decode failed: {e}")

    # простая проверка на адекватный размер
    if len(raw) < 500:
        raise RuntimeError(f"TG_SESSION_B64 decoded too small ({len(raw)} bytes). Wrong base64?")

    # перезаписываем файл каждый старт — так надежнее
    with open(SESSION_PATH, "wb") as f:
        f.write(raw)

# ---------------- PARSING ----------------
DISTRICT_PATTERNS = {
    "Лубенський": re.compile(r"\bлубен", re.I),
    "Миргородський": re.compile(r"\bмиргород", re.I),
    "Полтавський": re.compile(r"\bполтав", re.I),

    # фикс Кременчуцький (часто ломается из-за ё/е/і/у/ь/апострофов)
    "Кременчуцький": re.compile(r"\bкременчук|\bкременчуц", re.I),
}

ALERT_RE = re.compile(r"(повітрян\w*\s+тривог\w*)", re.I)
CLEAR_RE = re.compile(r"(відб\w*\s+тривог\w*)", re.I)

def now_ua_str():
    return datetime.now(UA_TZ).strftime("%H:%M")

def extract_districts(text: str):
    t = text.lower()
    found = []
    for name, rx in DISTRICT_PATTERNS.items():
        if rx.search(t):
            found.append(name)
    # если в тексте список буллетами, попробуем вытащить прям "• ... район"
    # и сопоставить по словам
    bullets = re.findall(r"•\s*([^\n#]+)", text)
    for b in bullets:
        bl = b.lower()
        for name, rx in DISTRICT_PATTERNS.items():
            if name not in found and rx.search(bl):
                found.append(name)
    return found

def is_alert(text: str) -> bool:
    return bool(ALERT_RE.search(text))

def is_clear(text: str) -> bool:
    return bool(CLEAR_RE.search(text))

def format_message(kind: str, districts: list[str], extra_line: str | None = None):
    # kind: "alert" or "clear"
    if kind == "alert":
        title = "<b>Повітряна тривога</b>"
        dot = "🔴"
    else:
        title = "<b>Відбій тривоги</b>"
        dot = "🟩"

    lines = [f"{dot} {now_ua_str()} {title}"]
    if districts:
        lines.append("📍 Райони:")
        for d in districts:
            lines.append(f"• {html.escape(d)} район")
    if extra_line:
        lines.append(extra_line)
    return "\n".join(lines)

# ---------------- AGGREGATION (отбои) ----------------
CLEAR_AGG_WINDOW_SEC = 5       # если отбои пришли быстро — склеиваем
CLEAR_ALL_WINDOW_SEC = 120     # если все отбои за 2 минуты — "везде отбой"

ALL_DISTRICTS = list(DISTRICT_PATTERNS.keys())

clear_buffer = {
    "ts_first": None,   # float
    "districts": set(), # set[str]
}

def reset_clear_buffer():
    clear_buffer["ts_first"] = None
    clear_buffer["districts"] = set()

async def flush_clear_if_needed(send_func):
    """
    Если накопили отбои и окно прошло — отправить.
    """
    if clear_buffer["ts_first"] is None:
        return

    age = time.time() - clear_buffer["ts_first"]
    if age < CLEAR_AGG_WINDOW_SEC:
        return

    districts = sorted(clear_buffer["districts"], key=lambda x: ALL_DISTRICTS.index(x) if x in ALL_DISTRICTS else 999)
    # если все районы закрылись за 2 минуты — общий отбой
    age_all = time.time() - clear_buffer["ts_first"]
    if set(districts) >= set(ALL_DISTRICTS) and age_all <= CLEAR_ALL_WINDOW_SEC:
        msg = format_message("clear", [], extra_line="✅ В усіх районах області — відбій.")
    else:
        msg = format_message("clear", districts)

    await send_func(msg)
    reset_clear_buffer()

# ---------------- MAIN ----------------
async def main():
    ensure_session_file()

    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)

    await client.connect()
    if not await client.is_user_authorized():
        # если сессия битая — сразу скажем
        raise RuntimeError("Session is not authorized. Recreate session.session locally and update TG_SESSION_B64.")

    # Проверим доступ к источнику
    src_entity = await client.get_input_entity(SOURCE_CHANNEL)

    async def send_to_target(text_html: str):
        # send_message поддерживает parse_mode через html=True? В telethon это parse_mode='html'
        await client.send_message(TARGET, text_html, parse_mode="html")

    # таймер, чтобы периодически флашить буфер отбоя
    async def buffer_watcher():
        while True:
            try:
                await flush_clear_if_needed(send_to_target)
            except Exception:
                pass
            await asyncio.sleep(1)

    asyncio.create_task(buffer_watcher())

    @client.on(events.NewMessage(chats=src_entity))
    async def handler(event):
        text = event.raw_text or ""
        # игнор пустых/сервисных
        if len(text.strip()) < 3:
            return

        districts = extract_districts(text)
        # если не нашли районы — не спамим
        if not districts:
            return

        if is_alert(text):
            # перед тревогой — если в буфере были отбои, отправим их
            await flush_clear_if_needed(send_to_target)

            msg = format_message("alert", districts, extra_line="Слідкуйте за подальшими повідомленнями.")
            await send_to_target(msg)
            return

        if is_clear(text):
            # копим отбои 5 секунд, чтобы склеить
            if clear_buffer["ts_first"] is None:
                clear_buffer["ts_first"] = time.time()

            for d in districts:
                clear_buffer["districts"].add(d)

            # если уже все районы закрылись — можно отправлять сразу, не ждать 5 сек
            if clear_buffer["districts"] >= set(ALL_DISTRICTS):
                await flush_clear_if_needed(send_to_target)

            return

    print("RUNNING: listening source =", SOURCE_CHANNEL, "-> target =", TARGET)
    await client.run_until_disconnected()

if __name__ == "__main__":
    # На Render Web Service нужен порт — запускаем Flask в отдельном треде
    import threading
    threading.Thread(target=run_web, daemon=True).start()

    asyncio.run(main())
