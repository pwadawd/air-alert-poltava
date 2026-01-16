import os
import re
import asyncio
import base64
import requests
from datetime import datetime, timezone, timedelta

from telethon import TelegramClient, events

# ---------------- ENV ----------------
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TARGET = os.environ["TG_TARGET"]

# base64 of session.session (created locally)
TG_SESSION_B64 = os.environ.get("TG_SESSION_B64", "")

SOURCE_CHANNEL = "SOURCE_CHANNEL = "awedfadadawfdagerewsgfqaqaw"

# Ukraine time for nice timestamps
UA_TZ = timezone(timedelta(hours=2))

DISTRICT_PATTERNS = {
    "Полтавський": re.compile(r"полтав", re.I),
    "Кременчуцький": re.compile(r"кременчук|кременчуг", re.I),
    "Миргородський": re.compile(r"миргород", re.I),
    "Лубенський": re.compile(r"лубн|лубен", re.I),
}

# ---------------- Telegram Bot API helpers ----------------
def tg_send(text: str) -> int | None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={
            "chat_id": TARGET,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    try:
        data = r.json()
        if data.get("ok") and data.get("result", {}).get("message_id"):
            return int(data["result"]["message_id"])
    except Exception:
        pass
    return None

def tg_delete(message_id: int) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
    try:
        requests.post(
            url,
            json={"chat_id": TARGET, "message_id": message_id},
            timeout=20,
        )
    except Exception:
        pass

# ---------------- formatting ----------------
def now_ua_str() -> str:
    return datetime.now(UA_TZ).strftime("%H:%M")

def fmt_duration(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    m = seconds // 60
    h = m // 60
    m = m % 60
    if h > 0:
        return f"{h}:{m:02d} год"
    return f"0:{m:02d} хв"

def detect_districts(text: str) -> list[str]:
    found = []
    for name, rx in DISTRICT_PATTERNS.items():
        if rx.search(text or ""):
            found.append(name)
    return found

def is_alert_on(text: str) -> bool:
    t = (text or "").lower()
    return ("повітряна тривога" in t) or ("повітряна тривога" in t) or ("тривога" in t and "відб" not in t)

def is_alert_off(text: str) -> bool:
    t = (text or "").lower()
    return ("відбій" in t) or ("отбой" in t)

# ---------------- state ----------------
# per district: {"start_ts": epoch_seconds, "msg_id_on": int}
ACTIVE: dict[str, dict] = {}
# remember last "full oblast" ON message id to delete if needed
FULL_OBLAST_MSG_ID: int | None = None

ALL_DISTRICTS = list(DISTRICT_PATTERNS.keys())

def build_on_message(districts: list[str], start_time_str: str) -> str:
    lines = [
        "🟥 ПОВІТРЯНА ТРИВОГА",
        "",
        "📍 Райони:",
    ]
    for d in districts:
        lines.append(f"• {d}")
    lines += [
        "",
        f"Час початку тривоги: {start_time_str}",
    ]
    return "\n".join(lines)

def build_off_message(districts: list[str], end_time_str: str, duration_str: str) -> str:
    lines = [
        "🟩 ВІДБІЙ ТРИВОГИ",
        "",
        "📍 Райони:",
    ]
    for d in districts:
        lines.append(f"• {d}")
    lines += [
        "",
        f"Час закінчення тривоги: {end_time_str}",
        f"Тривалість: {duration_str}",
    ]
    return "\n".join(lines)

# ---------------- main ----------------
def restore_session_file():
    if not TG_SESSION_B64:
        print("TG_SESSION_B64 is empty - session file will not be restored")
        return
    try:
        raw = base64.b64decode(TG_SESSION_B64.encode("utf-8"))
        with open("session.session", "wb") as f:
            f.write(raw)
        print("Session restored from TG_SESSION_B64")
    except Exception as e:
        print("Failed to restore session:", e)

async def main():
    restore_session_file()

    client = TelegramClient("session", API_ID, API_HASH)

    # IMPORTANT: start() without phone -> will use existing session.session
    await client.start()
    print("Air alert bot started")

    @client.on(events.NewMessage(chats=SOURCE_CHANNEL))
    async def handler(event):
        global FULL_OBLAST_MSG_ID

        text = event.message.message or ""
        districts = detect_districts(text)
        if not districts:
            return

        ts = int(event.message.date.replace(tzinfo=timezone.utc).timestamp())
        # Determine ON/OFF
        if is_alert_off(text):
            # For each district in message: compute duration separately
            for d in districts:
                if d in ACTIVE:
                    start_ts = int(ACTIVE[d]["start_ts"])
                    dur = fmt_duration(ts - start_ts)
                    msg = build_off_message([d], now_ua_str(), dur)
                    tg_send(msg)
                    # clear active
                    del ACTIVE[d]
            return

        if not is_alert_on(text):
            return

        # alert ON
        # If all districts become active, post one "full oblast" and delete district ON posts
        # We do not wait; we post district(s) immediately, then if it turns into full oblast we delete.
        for d in districts:
            if d in ACTIVE:
                continue
            msg_id = tg_send(build_on_message([d], now_ua_str()))
            if msg_id:
                ACTIVE[d] = {"start_ts": ts, "msg_id_on": msg_id}
            else:
                ACTIVE[d] = {"start_ts": ts, "msg_id_on": None}

        # if all districts are active -> consolidate
        if all(d in ACTIVE for d in ALL_DISTRICTS):
            # send full oblast post
            full_msg = build_on_message(["ВСЯ ПОЛТАВСЬКА ОБЛАСТЬ"], now_ua_str())
            FULL_OBLAST_MSG_ID = tg_send(full_msg)

            # delete previous district ON messages (cleanup)
            for d in list(ALL_DISTRICTS):
                mid = ACTIVE.get(d, {}).get("msg_id_on")
                if mid:
                    tg_delete(int(mid))

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
