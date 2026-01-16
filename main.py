import os
import re
import asyncio
import base64
import time
import html
import requests
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, events
print("=== VERSION: 2026-01-16 17:00 TEST ===")

# ------------------ ENV ------------------
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TARGET = os.environ["TG_TARGET"]

# source channel username (without @). default: air_alert_ua
SOURCE_CHANNEL = os.environ.get("TG_SOURCE", "air_alert_ua")

# base64 of session.session (created locally)
TG_SESSION_B64 = os.environ.get("TG_SESSION_B64", "")

# Ukraine time (Render may be UTC)
UA_TZ = timezone(timedelta(hours=2))

# Poltava districts we track
ALL_DISTRICTS = ["Полтавський", "Кременчуцький", "Миргородський", "Лубенський"]

DISTRICT_PATTERNS = {
    "Полтавський": re.compile(r"полтав", re.I),
    "Кременчуцький": re.compile(r"кременчуц|кременчуг", re.I),
    "Миргородський": re.compile(r"миргород", re.I),
    "Лубенський": re.compile(r"лубн|лубен", re.I),
}

# ------------------ STATE ------------------
# district -> {"start_ts": int, "msg_id_on": int}
ACTIVE: dict[str, dict] = {}

# For "whole oblast ON" consolidation (delete individual ON posts)
ON_BUFFER: dict[str, dict] = {}  # district -> {"start_ts": int, "msg_id_on": int}
FULL_OBLAST_ON_MSG_ID: int | None = None

# For OFF grouping and "whole oblast OFF" consolidation
OFF_PENDING: dict[str, dict] = {}  # district -> {"end_ts": int, "duration": str}
OFF_FLUSH_TASK: asyncio.Task | None = None
OFF_GROUP_WINDOW_SEC = 5

OFF_BUFFER: dict[str, dict] = {}  # district -> {"end_ts": int, "msg_id_off": int}
FULL_OBLAST_OFF_MSG_ID: int | None = None

# ------------------ HELPERS ------------------
def now_ua_str(ts: int | None = None) -> str:
    if ts is None:
        ts = int(time.time())
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(UA_TZ).strftime("%H:%M")

def fmt_duration(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h > 0:
        return f"{h}:{m:02d} год"
    return f"0:{m:02d} хв"

def escape_html(s: str) -> str:
    return html.escape(s or "", quote=False)

def tg_send(text_html: str) -> int | None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={
            "chat_id": TARGET,
            "text": text_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    try:
        data = r.json()
        if data.get("ok"):
            return int(data["result"]["message_id"])
        print("tg_send failed:", data)
        return None
    except Exception as e:
        print("tg_send exception:", e, r.text[:2000])
        return None

def tg_delete(message_id: int) -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
    try:
        requests.post(url, json={"chat_id": TARGET, "message_id": int(message_id)}, timeout=20)
    except Exception as e:
        print("tg_delete exception:", e)

def detect_districts(text: str) -> list[str]:
    t = text or ""
    found = [name for name, rx in DISTRICT_PATTERNS.items() if rx.search(t)]
    # keep stable order
    return [d for d in ALL_DISTRICTS if d in found]

def is_alert_on(text: str) -> bool:
    t = (text or "").lower()
    # UA/RU variants
    if "повітряна тривога" in t or "повітряної тривоги" in t:
        return True
    if "воздушная тревога" in t:
        return True
    # sometimes "Тривога" alone in region feed (careful but ok for our district-filtered logic)
    if "тривога" in t and "відбій" not in t and "отбой" not in t:
        return True
    return False

def is_alert_off(text: str) -> bool:
    t = (text or "").lower()
    return ("відбій" in t) or ("отбой" in t)

def build_still_alert_block(exclude: list[str] | None = None) -> str:
    exclude = exclude or []
    still = [d for d in ALL_DISTRICTS if d in ACTIVE and d not in exclude]
    if not still:
        return ""
    lines = ["", "<b>🟥 Тривога ще триває:</b>"]
    lines += [f"• {escape_html(d)}" for d in still]
    return "\n".join(lines)

def build_on_message(districts: list[str], start_time_str: str) -> str:
    lines = [
        "<b>🟥 ПОВІТРЯНА ТРИВОГА</b>",
        "",
        "<b>📍 Райони:</b>",
    ]
    for d in districts:
        lines.append(f"• {escape_html(d)}")
    lines += [
        "",
        f"Час початку тривоги: {escape_html(start_time_str)}",
    ]
    return "\n".join(lines)

def build_off_group_message(items: list[tuple[str, str]], end_time_str: str) -> str:
    # items: [(district, duration_str), ...]
    lines = [
        "<b>🟩 ВІДБІЙ ТРИВОГИ</b>",
        "",
        "<b>📍 Райони:</b>",
    ]
    for d, _dur in items:
        lines.append(f"• {escape_html(d)}")
    lines += [
        "",
        f"Час закінчення тривоги: {escape_html(end_time_str)}",
        "",
        "<b>⏱ Тривалість:</b>",
    ]
    for d, dur in items:
        lines.append(f"• {escape_html(d)}: {escape_html(dur)}")

    still_block = build_still_alert_block(exclude=[d for d, _ in items])
    if still_block:
        lines.append(still_block)

    # remove empty lines artifacts
    return "\n".join([x for x in lines if x != ""])

def build_full_oblast_on(start_time_str: str) -> str:
    return "\n".join([
        "<b>🟥 ПОВІТРЯНА ТРИВОГА</b>",
        "",
        "<b>📍 Райони:</b>",
        "• ВСЯ ПОЛТАВСЬКА ОБЛАСТЬ",
        "",
        f"Час початку тривоги: {escape_html(start_time_str)}",
    ])

def build_full_oblast_off(end_time_str: str) -> str:
    return "\n".join([
        "<b>🟩 ВІДБІЙ ТРИВОГИ</b>",
        "",
        "<b>📍 Райони:</b>",
        "• ВСЯ ПОЛТАВСЬКА ОБЛАСТЬ",
        "",
        f"Час закінчення тривоги: {escape_html(end_time_str)}",
    ])

async def flush_off_pending():
    global OFF_FLUSH_TASK, FULL_OBLAST_OFF_MSG_ID

    await asyncio.sleep(OFF_GROUP_WINDOW_SEC)

    if not OFF_PENDING:
        OFF_FLUSH_TASK = None
        return

    # sort by end time
    entries = sorted([(d, v["end_ts"], v["duration"]) for d, v in OFF_PENDING.items()], key=lambda x: x[1])

    # group inside 5 seconds (chain grouping)
    groups: list[list[tuple[str, int, str]]] = []
    cur: list[tuple[str, int, str]] = []
    for d, end_ts, dur in entries:
        if not cur:
            cur = [(d, end_ts, dur)]
        else:
            if end_ts - cur[-1][1] <= OFF_GROUP_WINDOW_SEC:
                cur.append((d, end_ts, dur))
            else:
                groups.append(cur)
                cur = [(d, end_ts, dur)]
    if cur:
        groups.append(cur)

    # send each group as one message
    msg_ids_sent: list[int] = []
    for g in groups:
        max_end = max(x[1] for x in g)
        end_str = now_ua_str(max_end)
        items = [(d, dur) for d, _end, dur in g]
        msg_id = tg_send(build_off_group_message(items, end_str))
        if msg_id:
            msg_ids_sent.append(msg_id)
            # store each district -> same msg_id (so we can delete later if "whole oblast")
            for d, end_ts, _dur in g:
                OFF_BUFFER[d] = {"end_ts": end_ts, "msg_id_off": msg_id}

    OFF_PENDING.clear()

    # whole oblast OFF check: all districts ended within 2 minutes
    if all(d in OFF_BUFFER for d in ALL_DISTRICTS):
        end_times = [int(OFF_BUFFER[d]["end_ts"]) for d in ALL_DISTRICTS]
        if max(end_times) - min(end_times) <= 120:
            end_str2 = now_ua_str(max(end_times))
            FULL_OBLAST_OFF_MSG_ID = tg_send(build_full_oblast_off(end_str2))

            # delete all partial OFF messages (unique)
            unique_ids = set()
            for d in ALL_DISTRICTS:
                mid = OFF_BUFFER.get(d, {}).get("msg_id_off")
                if mid:
                    unique_ids.add(int(mid))
            for mid in unique_ids:
                tg_delete(mid)

            OFF_BUFFER.clear()

    OFF_FLUSH_TASK = None

# ------------------ MAIN ------------------
async def main():
    # Restore session.session from base64 if provided
    if TG_SESSION_B64:
        try:
            with open("session.session", "wb") as f:
                f.write(base64.b64decode(TG_SESSION_B64))
            print("Session restored from TG_SESSION_B64")
        except Exception as e:
            print("Failed to restore session from TG_SESSION_B64:", e)

    # Use fixed session name so Telethon loads ./session.session
    client = TelegramClient("session", API_ID, API_HASH)

    @client.on(events.NewMessage(chats=SOURCE_CHANNEL))
    async def handler(event):
        global FULL_OBLAST_ON_MSG_ID, OFF_FLUSH_TASK

        text = event.message.message or ""
        ts = int(event.message.date.replace(tzinfo=timezone.utc).timestamp())

        districts = detect_districts(text)
        if not districts:
            return

        # OFF
        if is_alert_off(text):
            # When OFF happens, remove district from ACTIVE first (so "still alert" block is correct)
            for d in districts:
                if d in ACTIVE:
                    start_ts = int(ACTIVE[d]["start_ts"])
                    dur = fmt_duration(ts - start_ts)
                    del ACTIVE[d]

                    # pending group by 5 seconds
                    OFF_PENDING[d] = {"end_ts": ts, "duration": dur}

            if OFF_PENDING and OFF_FLUSH_TASK is None:
                OFF_FLUSH_TASK = asyncio.create_task(flush_off_pending())
            return

        # ON
        if is_alert_on(text):
            # for each new district: send district ON (immediately)
            for d in districts:
                if d in ACTIVE:
                    continue

                # if district re-activated, clear old OFF buffer state
                OFF_BUFFER.pop(d, None)

                start_str = now_ua_str(ts)
                msg_id = tg_send(build_on_message([d], start_str))
                if msg_id:
                    ACTIVE[d] = {"start_ts": ts, "msg_id_on": msg_id}
                    ON_BUFFER[d] = {"start_ts": ts, "msg_id_on": msg_id}
                else:
                    # still mark active, but without msg_id
                    ACTIVE[d] = {"start_ts": ts, "msg_id_on": None}
                    ON_BUFFER[d] = {"start_ts": ts, "msg_id_on": None}

            # check whole oblast ON (all districts active and their starts within 2 minutes)
            if all(d in ACTIVE for d in ALL_DISTRICTS):
                start_times = [int(ACTIVE[d]["start_ts"]) for d in ALL_DISTRICTS]
                if max(start_times) - min(start_times) <= 120:
                    # send full oblast ON once per wave
                    start_str2 = now_ua_str(min(start_times))
                    full_id = tg_send(build_full_oblast_on(start_str2))
                    if full_id:
                        FULL_OBLAST_ON_MSG_ID = full_id

                    # delete district ON posts (unique ids)
                    unique_ids = set()
                    for d in ALL_DISTRICTS:
                        mid = ON_BUFFER.get(d, {}).get("msg_id_on")
                        if mid:
                            unique_ids.add(int(mid))
                    for mid in unique_ids:
                        tg_delete(mid)

                    ON_BUFFER.clear()

            return

    await client.start()  # must not ask for phone because we restored session.session
    print("Air alert bot started")
    print("LISTENING SOURCE_CHANNEL:", SOURCE_CHANNEL)
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
