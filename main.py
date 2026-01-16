import os
import re
import asyncio
import requests
from datetime import datetime, timedelta
from telethon import TelegramClient, events

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
TARGET = os.environ["TG_TARGET"]  # @channel или -100...

SOURCE_CHANNEL = "awedfadadawfdagerewsgfqaqaw"

DISTRICTS = {
    "Полтавський": re.compile(r"полтав", re.I),
    "Кременчуцький": re.compile(r"кременчук|кременчуг", re.I),
    "Миргородський": re.compile(r"миргород", re.I),
    "Лубенський": re.compile(r"лубн|лубен", re.I),
}
ALL_DISTRICTS = list(DISTRICTS.keys())

TRIVOGA_RX = re.compile(r"тривог|повітрян|воздушн", re.I)
VIDBIY_RX = re.compile(r"відбій|отбой", re.I)

# район -> время начала
active_alerts: dict[str, datetime] = {}

# окно на "вся область" (без ожидания отправки)
WINDOW_SEC = 120
window_started_at: datetime | None = None
window_started_districts: set[str] = set()
window_start_msg_ids: list[int] = []
window_expire_task: asyncio.Task | None = None
oblast_start_posted = False  # чтобы не пытаться "схлопывать" повторно

def tg_send(text: str) -> int | None:
    """SendMessage -> возвращает message_id (нужен для удаления)."""
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": TARGET,
            "text": text,
            "disable_web_page_preview": True
        },
        timeout=20
    )
    if r.status_code >= 400:
        print("Send failed:", r.status_code, r.text)
        return None

    try:
        data = r.json()
        return data.get("result", {}).get("message_id")
    except Exception as e:
        print("Send parse error:", e)
        return None

def tg_delete(message_id: int):
    r = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
        json={"chat_id": TARGET, "message_id": message_id},
        timeout=20
    )
    if r.status_code >= 400:
        # если нет прав — просто залогируем и продолжим
        print("Delete failed:", r.status_code, r.text)

def detect_districts(text: str) -> list[str]:
    return [name for name, rx in DISTRICTS.items() if rx.search(text)]

def format_duration(start: datetime, end: datetime) -> str:
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 0:
        minutes = 0
    hours = minutes // 60
    mins = minutes % 60
    if hours > 0:
        return f"{hours}:{mins:02d} год"
    return f"{mins} хв"

def build_start_district_message(districts_with_time: list[tuple[str, datetime]]) -> str:
    lines = [f"• {d} — {t.strftime('%H:%M')}" for d, t in districts_with_time]
    return (
        "🟥 ПОВІТРЯНА ТРИВОГА\n\n"
        "📍 Райони (час початку):\n"
        + "\n".join(lines)
    )

def build_start_oblast_message(earliest: datetime) -> str:
    return (
        "🟥 ПОВІТРЯНА ТРИВОГА\n\n"
        "Тривога по всій Полтавській області\n\n"
        f"Час початку тривоги: {earliest.strftime('%H:%M')}"
    )

def build_end_message(districts_with_info: list[tuple[str, datetime, datetime]]) -> str:
    lines = []
    for d, s, e in districts_with_info:
        dur = format_duration(s, e)
        lines.append(f"• {d} — {e.strftime('%H:%M')} (тривалість {dur})")
    return (
        "🟩 ВІДБІЙ ТРИВОГИ\n\n"
        "📍 Райони (час закінчення + тривалість):\n"
        + "\n".join(lines)
    )

async def expire_window_later(start_at: datetime):
    """Через 2 минуты закрываем окно, если 'вся область' не собралась."""
    global window_started_at, window_started_districts, window_start_msg_ids, window_expire_task
    await asyncio.sleep(WINDOW_SEC)
    # если окно не менялось и не схлопнули в область — просто сбрасываем
    if window_started_at == start_at:
        window_started_at = None
        window_started_districts = set()
        window_start_msg_ids = []
        window_expire_task = None

async def main():
    global window_started_at, window_started_districts, window_start_msg_ids, window_expire_task, oblast_start_posted
    client = TelegramClient("session", API_ID, API_HASH)

    @client.on(events.NewMessage(chats=SOURCE_CHANNEL))
    async def handler(event):
        global window_started_at, window_started_districts, window_start_msg_ids, window_expire_task, oblast_start_posted

        text = event.message.message or ""
        districts = detect_districts(text)
        if not districts:
            return

        now = datetime.now()

        # 🟥 START
        if TRIVOGA_RX.search(text) and not VIDBIY_RX.search(text):
            newly_started: list[tuple[str, datetime]] = []

            for d in districts:
                if d not in active_alerts:
                    active_alerts[d] = now
                    newly_started.append((d, now))

            if not newly_started:
                return

            # 1) Отправляем СРАЗУ районный пост
            msg_id = tg_send(build_start_district_message(newly_started))
            if msg_id is not None:
                # 2) Открываем/ведем окно на "вся область" только если еще не схлопнули
                if not oblast_start_posted:
                    if window_started_at is None:
                        window_started_at = now
                        window_started_districts = set()
                        window_start_msg_ids = []
                        # таймер сброса окна
                        window_expire_task = asyncio.create_task(expire_window_later(window_started_at))

                    # записываем район(ы) и message_id
                    for d, _ in newly_started:
                        window_started_districts.add(d)
                    window_start_msg_ids.append(msg_id)

                    # 3) Если все 4 района начались в пределах 2 минут — удаляем старые посты и шлем "вся область"
                    if window_started_at and (now - window_started_at) <= timedelta(seconds=WINDOW_SEC):
                        if all(d in window_started_districts for d in ALL_DISTRICTS):
                            # удалить районные старт-посты
                            for mid in window_start_msg_ids:
                                tg_delete(mid)

                            earliest = min(active_alerts[d] for d in ALL_DISTRICTS if d in active_alerts)
                            tg_send(build_start_oblast_message(earliest))

                            oblast_start_posted = True
                            # закрываем окно
                            window_started_at = None
                            window_started_districts = set()
                            window_start_msg_ids = []
                            window_expire_task = None

            return

        # 🟩 END
        if VIDBIY_RX.search(text):
            ended: list[tuple[str, datetime, datetime]] = []
            for d in districts:
                start = active_alerts.pop(d, None)
                if start:
                    ended.append((d, start, now))

            if not ended:
                return

            tg_send(build_end_message(ended))

            # если тревоги больше нигде нет — сбрасываем флаг "вся область"
            if not active_alerts:
                oblast_start_posted = False

    await client.start()
    print("Air alert bot started")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
