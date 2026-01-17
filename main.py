import os
import re
import asyncio
import base64
from datetime import datetime, timezone, timedelta

from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ---------------- ENV ----------------
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]

# Bot token used ONLY for sending (optional; can also send from user)
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()

# Where to post results: channel/group username WITHOUT @ (e.g. poltavaRanger) or numeric id
TARGET = os.environ["TG_TARGET"].strip()

# Source channel username WITHOUT @ (e.g. air_alert_ua)
SOURCE_CHANNEL = os.environ.get("TG_SOURCE", "air_alert_ua").strip().lstrip("@")

# base64 of Telethon .session file -> used to start as USER
TG_SESSION_B64 = os.environ.get("TG_SESSION_B64", "").strip()

UA_TZ = timezone(timedelta(hours=2))

# ---- District patterns (fix Kremenchuk) ----
DISTRICT_PATTERNS = {
    "Полтавський": re.compile(r"полтав", re.I),
    "Кременчуцький": re.compile(r"кременчук|кременчуг|кременчуц|кременчуць", re.I),
    "Миргородський": re.compile(r"миргород", re.I),
    "Лубенський": re.compile(r"лубн|лубен", re.I),
}

# ---------------- helpers ----------------
def now_ua() -> datetime:
    return datetime.now(tz=UA_TZ)

def fmt_hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")

def is_alert(text: str) -> bool:
    t = text.lower()
    return ("повітряна тривога" in t) or ("повітряна трив" in t) or ("🔴" in t)

def is_all_clear(text: str) -> bool:
    t = text.lower()
    return ("відбій" in t) or ("🟩" in t)

def detect_districts(text: str):
    found = []
    for name, rx in DISTRICT_PATTERNS.items():
        if rx.search(text):
            found.append(name)
    return found

async def resolve_entity(client: TelegramClient, value: str):
    """
    value can be username (without @) or numeric id.
    """
    v = value.strip()
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return await client.get_entity(v.lstrip("@"))

async def send_text(user_client: TelegramClient, bot_client: TelegramClient | None, target_entity, text: str):
    # Prefer bot if token provided, else send from user session
    if bot_client is not None:
        await bot_client.send_message(target_entity, text, link_preview=False)
    else:
        await user_client.send_message(target_entity, text, link_preview=False)

# ---------------- main ----------------
async def main():
    # 1) USER client (must work on Render, no prompts)
    if not TG_SESSION_B64:
        raise RuntimeError("TG_SESSION_B64 is empty. Upload your session.b64 into Render env var TG_SESSION_B64")

    try:
        session_bytes = base64.b64decode(TG_SESSION_B64.encode("utf-8"))
        session_str = session_bytes.decode("utf-8", errors="strict")
    except Exception as e:
        raise RuntimeError(f"TG_SESSION_B64 decode failed: {e}")

    user = TelegramClient(StringSession(session_str), API_ID, API_HASH)

    # 2) Optional BOT client for sending
    bot = None
    if BOT_TOKEN:
        bot = TelegramClient("bot_sender", API_ID, API_HASH).start(bot_token=BOT_TOKEN)

    await user.start()
    me = await user.get_me()
    print(f"[AUTH] USER OK: id={me.id} username={getattr(me, 'username', None)}")

    if bot is not None:
        bme = await bot.get_me()
        print(f"[AUTH] BOT OK: id={bme.id} username={getattr(bme, 'username', None)}")

    # Resolve entities
    source_entity = await resolve_entity(user, SOURCE_CHANNEL)
    target_entity = await resolve_entity(user, TARGET)
    print(f"[CFG] SOURCE={SOURCE_CHANNEL} -> {source_entity}")
    print(f"[CFG] TARGET={TARGET} -> {target_entity}")

    async def process_text(text: str):
        # DEBUG входящего текста
        print("IN:", (text or "")[:160].replace("\n", " "))

        districts = detect_districts(text or "")
        if not districts:
            # Если не нашли район — просто игнор
            return

        t = now_ua()
        if is_alert(text):
            header = "**🟥 ПОВІТРЯНА ТРИВОГА**"
            body = "\n".join([f"• {d} район" for d in districts])
            msg = f"{header}\n\n📍 Райони:\n{body}\n\nЧас початку тривоги: {fmt_hhmm(t)}"
            await send_text(user, bot, target_entity, msg)
            print("[OUT] alert sent")

        elif is_all_clear(text):
            header = "**🟩 ВІДБІЙ ТРИВОГИ**"
            body = "\n".join([f"• {d} район" for d in districts])
            msg = f"{header}\n\n📍 Райони:\n{body}\n\nЧас закінчення тривоги: {fmt_hhmm(t)}"
            await send_text(user, bot, target_entity, msg)
            print("[OUT] clear sent")

    @user.on(events.NewMessage(chats=source_entity))
    async def on_new(event):
        await process_text(event.raw_text)

    @user.on(events.MessageEdited(chats=source_entity))
    async def on_edit(event):
        await process_text(event.raw_text)

    print("[RUN] Listening...")
    await user.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
