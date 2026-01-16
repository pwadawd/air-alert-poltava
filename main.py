import os
import re
import time
import base64
import html
import asyncio
import threading
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask
from telethon import TelegramClient, events

# -------------------- ENV --------------------
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TARGET = os.environ["TG_TARGET"]  # channel @username or numeric id
SOURCE_CHANNEL = os.environ.get("TG_SOURCE", "air_alert_ua").lstrip("@")

# base64 of session.session (created locally once)
TG_SESSION_B64 = os.environ.get("TG_SESSION_B64", "")

# Render provides PORT for Web Service
PORT = int(os.environ.get("PORT", "10000"))

UA_TZ = timezone(timedelta(hours=2))  # Ukraine time (you can switch to +3 if needed in summer)

ALL_DISTRICTS = ["Полтавський", "Кременчуцький", "Миргородський", "Лубенський"]

# Robust patterns (fix for Кременчуцький)
DISTRICT_PATTERNS = {
    "Полтавський": re.compile(r"\bполтав", re.I),
    "Кременчуцький": re.compile(r"\bкременчу", re.I),  # matches "Кременчуцький"
    "Миргородський": re.compile(r"\bмиргород", re.I),
    "Лубенський": re.compile(r"\bлубен", re.I),
}

# -------------------- STATE --------------------
ACTIVE = {}  # district -> start_dt (aware)
# Track our sent messages to delete when it turns into "whole oblast"
SENT_ALERT_MSGS = []  # list of (ts_epoch, message_id)
SENT_CLEAR_MSGS = []  # list of (ts_epoch, message_id)

FIRST_ALERT_TS = None
FIRST_CLEAR_TS = None

# Debounce buffer for OFF (відбій)
OFF_BUFFER = set()
OFF_DEBOUNCE_TASK = None
OFF_DEBOUNCE_SECONDS = 5

# -------------------- TELEGRAM BOT API --------------------
def tg_send(text: str) -> int | None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": TARGET,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        data = r.json()
        if isinstance(data, dict) and data.get("ok") and data.get("result"):
            return int(data["result"]["message_id"])
    except Exception:
        pass
    return None

def tg_delete(message_id: int) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
    try:
        requests.post(url, json={"chat_id": TARGET, "message_id": message_id}, timeout=15)
    except Exception:
        pass

def now_ua() -> datetime:
    return datetime.now(tz=UA_TZ)

def fmt_hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")

def fmt_duration(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    m = seconds // 60
    h = m // 60
    m = m % 60
    return f"{h}:{m:02d} хв"

def detect_districts(text: str) -> list[str]:
    t = (text or "").lower()
    found = []
    for name, rx in DISTRICT_PATTERNS.items():
        if rx.search(t):
            found.append(name)
    # keep stable order
    return [d for d in ALL_DISTRICTS if d in found]

def is_alert_message(text: str) -> bool:
    t = (text or "").lower()
    # air_alert_ua uses "Повітряна тривога" for alert
    return "повітряна тривога" in t and "відбій" not in t

def is_clear_message(text: str) -> bool:
    t = (text or "").lower()
    return "відбій" in t

def build_alert_post(districts: list[str], start_dt: datetime) -> str:
    # Bold header + list districts
    lines = []
    lines.append("🟥 <b>ПОВІТРЯНА ТРИВОГА</b>")
    lines.append("")
    lines.append("📍 <b>Райони:</b>")
    for d in districts:
        lines.append(f"• {html.escape(d)}")
    lines.append("")
    lines.append(f"Час початку тривоги: {fmt_hhmm(start_dt)}")
    return "\n".join(lines)

def build_oblast_alert_post(start_dt: datetime) -> str:
    lines = []
    lines.append("🟥 <b>ПОВІТРЯНА ТРИВОГА</b>")
    lines.append("")
    lines.append("📍 <b>Полтавська область (всі райони)</b>")
    lines.append("")
    lines.append(f"Час початку тривоги: {fmt_hhmm(start_dt)}")
    return "\n".join(lines)

def build_clear_post(districts: list[str], end_dt: datetime) -> str:
    # Duration per district separately (different start times)
    lines = []
    lines.append("🟩 <b>ВІДБІЙ ТРИВОГИ</b>")
    lines.append("")
    lines.append("📍 <b>Райони:</b>")
    for d in districts:
        lines.append(f"• {html.escape(d)}")
    lines.append("")
    lines.append(f"Час закінчення тривоги: {fmt_hhmm(end_dt)}")
    lines.append("")

    # durations per district
    for d in districts:
        st = ACTIVE.get(d)
        if st:
            dur = int((end_dt - st).total_seconds())
            lines.append(f"Тривалість ({html.escape(d)}): {fmt_duration(dur)}")
        else:
            lines.append(f"Тривалість ({html.escape(d)}): —")

    # remaining active
    remaining = [d for d in ALL_DISTRICTS if d in ACTIVE and d not in districts]
    if remaining:
        lines.append("")
        lines.append("🔴 <b>Далі тривога триває:</b>")
        for d in remaining:
            lines.append(f"• {html.escape(d)}")

    return "\n".join(lines)

def build_oblast_clear_post(end_dt: datetime, start_dt: datetime) -> str:
    lines = []
    lines.append("🟩 <b>ВІДБІЙ ТРИВОГИ</b>")
    lines.append("")
    lines.append("📍 <b>Полтавська область (всі райони)</b>")
    lines.append("")
    lines.append(f"Час закінчення тривоги: {fmt_hhmm(end_dt)}")
    lines.append(f"Тривалість: {fmt_duration(int((end_dt - start_dt).total_seconds()))}")
    return "\n".join(lines)

def prune_sent(list_ref: list[tuple[float, int]], window_sec: int = 180) -> None:
    cutoff = time.time() - window_sec
    while list_ref and list_ref[0][0] < cutoff:
        list_ref.pop(0)

# -------------------- LOGIC --------------------
async def maybe_convert_to_oblast_alert():
    global FIRST_ALERT_TS
    if all(d in ACTIVE for d in ALL_DISTRICTS) and FIRST_ALERT_TS is not None:
        # if all turned on within 120s from first alert activation
        if (time.time() - FIRST_ALERT_TS) <= 120:
            # delete our previous alert messages from last 120s
            prune_sent(SENT_ALERT_MSGS, window_sec=300)
            for ts, mid in list(SENT_ALERT_MSGS):
                if (time.time() - ts) <= 120:
                    tg_delete(mid)
            SENT_ALERT_MSGS.clear()

            # start time of oblast = earliest start among districts
            start_dt = min(ACTIVE[d] for d in ALL_DISTRICTS if d in ACTIVE)
            mid = tg_send(build_oblast_alert_post(start_dt))
            if mid:
                SENT_ALERT_MSGS.append((time.time(), mid))

def clear_districts(districts: list[str], end_dt: datetime):
    for d in districts:
        if d in ACTIVE:
            del ACTIVE[d]

async def maybe_convert_to_oblast_clear(end_dt: datetime):
    global FIRST_CLEAR_TS
    # if now none active AND clear wave happened fast
    if not ACTIVE and FIRST_CLEAR_TS is not None and (time.time() - FIRST_CLEAR_TS) <= 120:
        # delete previous clear messages from last 120s
        prune_sent(SENT_CLEAR_MSGS, window_sec=300)
        for ts, mid in list(SENT_CLEAR_MSGS):
            if (time.time() - ts) <= 120:
                tg_delete(mid)
        SENT_CLEAR_MSGS.clear()

        # for oblast duration take earliest alert start we can infer (best-effort)
        # if we don't know, just don't show duration
        # We'll reconstruct from last known starts saved earlier is gone; so store separately:
        pass

# We need stable oblast start for duration; keep memory
OBLAST_LAST_START = None  # datetime | None

def update_oblast_start_if_needed():
    global OBLAST_LAST_START
    if all(d in ACTIVE for d in ALL_DISTRICTS):
        # earliest start
        OBLAST_LAST_START = min(ACTIVE[d] for d in ALL_DISTRICTS)

async def send_clear_batch():
    global OFF_BUFFER, OFF_DEBOUNCE_TASK, FIRST_CLEAR_TS, OBLAST_LAST_START
    await asyncio.sleep(OFF_DEBOUNCE_SECONDS)

    districts = [d for d in ALL_DISTRICTS if d in OFF_BUFFER]
    OFF_BUFFER.clear()
    OFF_DEBOUNCE_TASK = None

    if not districts:
        return

    end_dt = now_ua()
    if FIRST_CLEAR_TS is None:
        FIRST_CLEAR_TS = time.time()

    # Prepare post BEFORE deleting from ACTIVE to show correct durations
    text = build_clear_post(districts, end_dt)
    mid = tg_send(text)
    if mid:
        SENT_CLEAR_MSGS.append((time.time(), mid))

    # Apply clear
    clear_districts(districts, end_dt)

    # If after clearing nothing remains AND we had oblast start within 120s window -> convert to oblast clear
    if not ACTIVE and OBLAST_LAST_START is not None and (time.time() - FIRST_CLEAR_TS) <= 120:
        # delete last clear messages within 120s and send one oblast clear
        prune_sent(SENT_CLEAR_MSGS, window_sec=300)
        for ts, mid2 in list(SENT_CLEAR_MSGS):
            if (time.time() - ts) <= 120:
                tg_delete(mid2)
        SENT_CLEAR_MSGS.clear()

        mid3 = tg_send(build_oblast_clear_post(end_dt, OBLAST_LAST_START))
        if mid3:
            SENT_CLEAR_MSGS.append((time.time(), mid3))

    # reset timers after wave ends
    if not ACTIVE:
        FIRST_CLEAR_TS = None
        OBLAST_LAST_START = None

async def process_alert(districts: list[str], raw_text: str):
    global FIRST_ALERT_TS
    start_dt = now_ua()

    newly = []
    for d in districts:
        if d not in ACTIVE:
            ACTIVE[d] = start_dt
            newly.append(d)

    if not newly:
        return

    if FIRST_ALERT_TS is None:
        FIRST_ALERT_TS = time.time()

    # send alert for newly started districts
    mid = tg_send(build_alert_post(newly, start_dt))
    if mid:
        SENT_ALERT_MSGS.append((time.time(), mid))

    update_oblast_start_if_needed()
    await maybe_convert_to_oblast_alert()

    # if wave ended (not all districts within 120s) let it be; reset after 2 min window passes
    if FIRST_ALERT_TS is not None and (time.time() - FIRST_ALERT_TS) > 130:
        FIRST_ALERT_TS = None
        prune_sent(SENT_ALERT_MSGS, window_sec=600)

async def process_clear(districts: list[str], raw_text: str):
    global OFF_DEBOUNCE_TASK
    for d in districts:
        if d in ACTIVE:
            OFF_BUFFER.add(d)

    # debounce: combine multiple off events within 5 seconds
    if OFF_DEBOUNCE_TASK is None:
        OFF_DEBOUNCE_TASK = asyncio.create_task(send_clear_batch())

# -------------------- RENDER WEB SERVER --------------------
def run_web():
    app = Flask(__name__)

    @app.get("/")
    def home():
        return "OK", 200

    @app.get("/health")
    def health():
        return "OK", 200

    app.run(host="0.0.0.0", port=PORT, threaded=True)

# -------------------- SESSION FILE --------------------
def ensure_session_file():
    # Telethon expects SQLite file "session.session" if session name is "session"
    if TG_SESSION_B64:
        try:
            data = base64.b64decode(TG_SESSION_B64.encode("utf-8"))
            with open("session.session", "wb") as f:
                f.write(data)
        except Exception:
            pass

# -------------------- MAIN --------------------
async def main():
    ensure_session_file()

    # Start web server in background thread (for Render Web Service port binding)
    t = threading.Thread(target=run_web, daemon=True)
    t.start()

    client = TelegramClient("session", API_ID, API_HASH)

    @client.on(events.NewMessage(chats=SOURCE_CHANNEL))
    async def handler(event):
        text = event.message.message or ""
        dists = detect_districts(text)
        if not dists:
            return

        if is_clear_message(text):
            await process_clear(dists, text)
        elif is_alert_message(text):
            await process_alert(dists, text)

    await client.start()
    print("Bot started. Listening:", SOURCE_CHANNEL)
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
