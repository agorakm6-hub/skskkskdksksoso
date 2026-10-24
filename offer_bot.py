import asyncio
import hmac
import html
import json
import logging
import math
import os
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

BRAND = os.environ.get("BRAND", "Telegram")
TEST_NOTICE = os.environ.get("TEST_NOTICE", "")
OFFER_TTL = int(float(os.environ.get("OFFER_HOURS", 6)) * 3600)
TRANSFER_MIN = int(os.environ.get("TRANSFER_MINUTES", 10))
STRICT = os.environ.get("OFFER_STRICT") == "1"
MAX_STARS = 100_000_000
UPDATES = ["message", "callback_query", "business_connection", "business_message"]
NBSP = "\u00a0"

OFFER_EDIT_DELAY = max(1.0, float(os.environ.get("OFFER_EDIT_DELAY", "1.0")))

http = None
bot_name = ""
connections, offers = {}, {}
tasks = set()


# ============ STYLING ============

# Латиница: Mathematical Sans-Serif Bold
_LATIN_UPPER = "𝗔𝗕𝗖𝗗𝗘𝗙𝗚𝗛𝗜𝗝𝗞𝗟𝗠𝗡𝗢𝗣𝗤𝗥𝗦𝗧𝗨𝗩𝗪𝗫𝗬𝗭"
_LATIN_LOWER = "𝗮𝗯𝗰𝗱𝗲𝗳𝗴𝗵𝗶𝗷𝗸𝗹𝗺𝗻𝗼𝗽𝗾𝗿𝘀𝘁𝘂𝘃𝘄𝘅𝘆𝘇"

# Греческий: Mathematical Sans-Serif Bold (там где есть в Unicode)
_GREEK_UPPER = {
    "Α": "𝗔", "Β": "𝗕", "Γ": "𝚪", "Δ": "𝚫", "Ε": "𝗘", "Ζ": "𝗭", "Η": "𝗛",
    "Θ": "𝚯", "Ι": "𝗜", "Κ": "𝗞", "Λ": "𝚲", "Μ": "𝗠", "Ν": "𝗡", "Ξ": "𝚵",
    "Ο": "𝗢", "Π": "𝚷", "Ρ": "𝗣", "Σ": "𝚺", "Τ": "𝗧", "Υ": "𝗨", "Φ": "𝚽",
    "Χ": "𝗫", "Ψ": "𝚿", "Ω": "𝛀",
}
_GREEK_LOWER = {
    "α": "𝗮", "β": "𝗯", "γ": "𝛄", "δ": "𝛅", "ε": "𝗲", "ζ": "𝘇", "η": "𝗵",
    "θ": "𝛉", "ι": "𝗶", "κ": "𝗸", "λ": "𝛌", "μ": "𝛍", "ν": "𝗻", "ξ": "𝛏",
    "ο": "𝗼", "π": "𝛑", "ρ": "𝗽", "σ": "𝛔", "ς": "𝛓", "τ": "𝘁", "υ": "𝛖",
    "φ": "𝛗", "χ": "𝛘", "ψ": "𝛙", "ω": "𝛚",
}

# Кириллица: визуально похожие стилизованные символы (small caps / подобные)
_CYR_MAP = {
    "а": "ᴀ", "б": "б", "в": "в", "г": "г", "д": "д", "е": "ᴇ", "ё": "ё",
    "ж": "ж", "з": "з", "и": "и", "й": "й", "к": "ᴋ", "л": "л", "м": "ᴍ",
    "н": "н", "о": "ᴏ", "п": "п", "р": "ᴘ", "с": "ᴄ", "т": "ᴛ", "у": "у",
    "ф": "ф", "х": "х", "ц": "ц", "ч": "ч", "ш": "ш", "щ": "щ", "ъ": "ъ",
    "ы": "ы", "ь": "ь", "э": "э", "ю": "ю", "я": "я",
    "А": "ᴀ", "Б": "Б", "В": "В", "Г": "Г", "Д": "Д", "Е": "ᴇ", "Ё": "Ё",
    "Ж": "Ж", "З": "З", "И": "И", "Й": "Й", "К": "ᴋ", "Л": "Л", "М": "ᴍ",
    "Н": "Н", "О": "ᴏ", "П": "П", "Р": "ᴘ", "С": "ᴄ", "Т": "ᴛ", "У": "У",
    "Ф": "Ф", "Х": "Х", "Ц": "Ц", "Ч": "Ч", "Ш": "Ш", "Щ": "Щ", "Ъ": "Ъ",
    "Ы": "Ы", "Ь": "Ь", "Э": "Э", "Ю": "Ю", "Я": "Я",
}


def _style_char(ch: str) -> str:
    o = ord(ch)
    if 0x41 <= o <= 0x5A:  # A-Z
        return _LATIN_UPPER[o - 0x41]
    if 0x61 <= o <= 0x7A:  # a-z
        return _LATIN_LOWER[o - 0x61]
    if ch in _GREEK_UPPER:
        return _GREEK_UPPER[ch]
    if ch in _GREEK_LOWER:
        return _GREEK_LOWER[ch]
    return _CYR_MAP.get(ch, ch)


_TAG_RE = re.compile(r"<(/?)(b|i|u|s|code|a|pre|em|strong|tg-spoiler)(\s[^>]*)?>", re.IGNORECASE)


def _style_plain(s: str) -> str:
    res = []
    for ch in s:
        o = ord(ch)
        # эмодзи, символы, управляющие, NBSP — не трогаем
        if (
            0x1F000 <= o <= 0x1FAFF or
            0x2600 <= o <= 0x27BF or
            0x2300 <= o <= 0x23FF or
            0x2B00 <= o <= 0x2BFF or
            0xFE00 <= o <= 0xFE0F or
            o == 0x200D or
            o == 0x20E3 or
            o == 0x00A0 or
            o < 0x20
        ):
            res.append(ch)
        else:
            res.append(_style_char(ch))
    return "".join(res)


def style(text: str) -> str:
    """Стилизует текст, не трогая содержимое HTML-тегов Telegram."""
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
    return {"text": text, "callback_data": data}


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


async def render_card(o, text, kb, preview=True):
    """Правит карточку; если edit не прошёл — пересоздаёт сообщение с кнопками."""
    res = await edit_offer(o, text, kb, preview=preview)
    if res.get("ok"):
        return res
    log.warning("edit упал (%s), пересоздаю карточку %s", res.get("description"), o["id"])
    res2 = await send(o["chat"], text, kb, o["bcid"], preview=preview)
    if res2.get("ok"):
        o["mid"] = res2["result"]["message_id"]
        save()
    else:
        log.error("не удалось пересоздать карточку %s: %s", o["id"], res2.get("description"))
    return res2


def save():
    for oid in sorted(offers, key=lambda k: offers[k]["created"])[:-500]:
        del offers[oid]
    tmp = f"{DATA_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"connections": connections, "offers": offers}, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except OSError as e:
        log.error("не удалось сохранить %s: %s", DATA_FILE, e)


def load():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        log.error("%s не читается: %s", DATA_FILE, e)
        return
    connections.update(data.get("connections", {}))
    offers.update(data.get("offers", {}))


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
        return None, "Это не похоже на ссылку на NFT: в конце должен быть номер, например DurovsCap-1234."
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
    return f"{num(o['stars'])}{NBSP}⭐"


def left_text(seconds):
    minutes = math.ceil(max(0, seconds) / 60)
    h, m = divmod(minutes, 60)
    return f"{h} ч. {m} мин." if h else f"{m} мин."


def offer_text(o):
    text = (f"Пользователь {maker_label(o)} предлагает Вам <b>{stars(o)} Звёзд</b> за подарок {gift_link(o)}.\n\n"
            f"Предложение действует ещё {left_text(o['expires'] - time.time())}")
    return text + (f"\n\n{esc(TEST_NOTICE)}" if TEST_NOTICE else "")


def sold_text(o):
    return (f"🎁 Подарок: {gift_link(o)} успешно продан!\n\n"
            f"✅ Передайте подарок покупателю для поступления на баланс {stars(o)}.\n\n"
            f"На передачу подарка есть {TRANSFER_MIN}м")


def declined_text(o):
    return "❌ Отклонено."


def expired_text(o):
    return f"⌛ Предложение {stars(o)} за подарок {gift_link(o)} истекло."


def offer_kb(o):
    return {"inline_keyboard": [[btn("✕ Отклонить", f"of:no:{o['id']}"), btn("✓ Принять", f"of:ok:{o['id']}")]]}


def sold_kb(o):
    u = o["maker"]
    if u.get("username"):
        transfer = {"text": "🎁 Передать", "url": f"tg://send_gift?to={u['username']}"}
    else:
        transfer = {"text": "🎁 Передать", "url": f"tg://user?id={u.get('id', 0)}"}
    return {"inline_keyboard": [[transfer], [btn("✅ Готово", f"of:done:{o['id']}")]]}


async def make_offer(m, maker, bcid):
    cmd, _, arg = (m.get("text") or "").partition(" ")
    parsed, error = parse_offer(arg)
    chat = m["chat"]["id"]
    now = time.time()

    res = await send(chat, "⏳ Готовлю оффер…", bcid=bcid, preview=False)
    if not res.get("ok"):
        desc = res.get("description", "")
        try:
            await delete_business_message(bcid, m["message_id"])
        except Exception:
            pass
        if "PEER_FLOOD" in desc:
            return await send(chat, "⚠️ Telegram временно ограничил отправку сообщений этому аккаунту "
                                    "(PEER_FLOOD) — это защита от спама новым людям, с которыми раньше не "
                                    "переписывались. Попробуй позже и реже слать офферы незнакомым.", bcid=bcid)
        return await send(chat, f"⚠️ Не получилось отправить оффер: {esc(desc)}", bcid=bcid)

    offer_mid = res["result"]["message_id"]

    try:
        await delete_business_message(bcid, m["message_id"])
    except Exception:
        log.exception("не удалось удалить команду %s", m["message_id"])

    placeholder = {"id": "tmp", "bcid": bcid, "chat": chat, "mid": offer_mid}
    await edit_offer(placeholder, "⏳ Почти готово…", None, preview=False)
    await asyncio.sleep(OFFER_EDIT_DELAY)

    if error:
        return await render_card(placeholder, f"⚠️ {esc(error)}\n\n{USAGE}", None, preview=False)

    o = {"id": uuid.uuid4().hex[:8], **parsed, "bcid": bcid, "chat": chat, "mid": offer_mid, "maker": maker,
         "status": "pending", "created": now, "expires": now + OFFER_TTL}
    offers[o["id"]] = o
    save()

    await render_card(o, offer_text(o), offer_kb(o), preview=True)


FINAL = {"accepted": (sold_text, sold_kb), "declined": (declined_text, None), "expired": (expired_text, None)}


async def settle(o, status):
    text, kb = FINAL[status]
    o["status"] = status
    save()
    await render_card(o, text(o), kb(o) if kb else None, preview=True)


async def sweeper():
    while True:
        await asyncio.sleep(60)
        for o in list(offers.values()):
            if o["status"] != "pending":
                continue
            try:
                if o["expires"] < time.time():
                    await settle(o, "expired")
                else:
                    await render_card(o, offer_text(o), offer_kb(o), preview=True)
            except Exception:
                log.exception("тик оффера %s", o["id"])


async def on_callback(q):
    parts = q.get("data", "").split(":")
    note = None
    o = offers.get(parts[2]) if len(parts) == 3 and parts[0] == "of" else None
    if o is None:
        note = "Этот оффер устарел"
    else:
        action = parts[1]
        if action == "done":
            note = "Сначала передайте подарок для получения средств"
        elif STRICT and q["from"]["id"] == o["maker"]["id"] and action in ("ok", "no"):
            note = "Это твой оффер: отвечать на него должен получатель"
        elif action in ("ok", "no"):
            if o["status"] != "pending":
                note = "Оффер уже обработан"
            elif time.time() > o["expires"]:
                await settle(o, "expired")
                note = "Срок предложения истёк"
            else:
                await settle(o, "accepted" if action == "ok" else "declined")
    extra = {"text": note, "show_alert": True} if note else {}
    await api("answerCallbackQuery", callback_query_id=q["id"], **extra)


def conn_from(c):
    rights = c.get("rights")
    rights = rights if isinstance(rights, dict) else {}
    can_reply = bool(rights.get("can_reply", c.get("can_reply")))
    can_delete = bool(rights.get("can_delete_all_messages"))
    user = c["user"]
    return {"id": c["id"], "user_id": user["id"], "chat": c["user_chat_id"], "name": user.get("first_name", ""),
            "username": user.get("username"), "enabled": c.get("is_enabled", True), "can_reply": can_reply,
            "can_delete": can_delete}


async def on_connection(c):
    conn = conn_from(c)
    connections[conn["id"]] = conn
    save()
    if not conn["enabled"]:
        text = "🔌 Бот отключён от профиля. Подключить снова: Настройки → Телеграм Бизнес → Автоматизация чатов."
    elif not conn["can_reply"]:
        text = ("⚠️ Бот подключён, но без права отвечать на сообщения. Открой Настройки → Телеграм Бизнес → "
                "Автоматизация чатов, выбери бота и включи это право — иначе офферы не создадутся.")
    else:
        text = ("✅ <b>Готово, бот подключён.</b>\n\n"
                "В любом чате напиши:\n<code>/offer ссылка сумма</code>\n"
                "Например: <code>/offer https://t.me/nft/DurovsCap-1234 500</code>\n\n"
                "Сообщение тут же превратится в карточку оффера с кнопками.")
        if not conn["can_delete"]:
            text += ("\n\n⚠️ Без права «Удалять сообщения» твоя команда останется в чате рядом с карточкой — "
                    "включи его в тех же настройках, чтобы она пропадала.")
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
    if (m.get("text") or "").partition(" ")[0].lower() != "/offer":
        return
    conn = await get_connection(m["business_connection_id"])
    frm = m.get("from") or {}
    if not conn or frm.get("id") != conn["user_id"]:
        return
    if not (conn["enabled"] and conn["can_reply"]):
        return await send(conn["chat"], "⚠️ У бота нет права отвечать на сообщения, оффер не создан. "
                                        "Включи его: Настройки → Телеграм Бизнес → Автоматизация чатов.")
    maker = {"id": frm["id"], "name": frm.get("first_name", ""), "username": frm.get("username")}
    await make_offer(m, maker, conn["id"])


async def on_message(m):
    chat = m["chat"]["id"]
    cmd = (m.get("text") or "").strip().split()[:1]
    cmd = cmd[0].split("@")[0].lower() if cmd else ""
    if cmd in ("/start", "/help"):
        kb = {"inline_keyboard": [[{"text": "📋 Скопировать юзернейм бота",
                                    "copy_text": {"text": f"@{bot_name}"}}]]}
        await send(chat, "👋 Привет!", kb)


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
    log.info("юзернейм бота: @%s (https://t.me/%s)", bot_name, bot_name)
    await api("setMyCommands", commands=[{"command": "start", "description": "Подключение"}])
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
