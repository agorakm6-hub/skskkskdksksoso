"""Оффер-бот: офферы на NFT через инлайн-режим Telegram.

В любом чате набери @бот ссылка сумма — сверху появится карточка оффера
с кнопками «Принять» / «Отклонить». Всё чисто визуальное: подарки никуда
не передаются, это прототип интерфейса.

Если написать /offer прямо боту в личку, оффер покажется там как предпросмотр.
"""
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
STRICT = os.environ.get("OFFER_STRICT") == "1"
MAX_STARS = 100_000_000
UPDATES = ["message", "callback_query", "inline_query"]
NBSP = "\u00a0"

http = None
bot_name = ""
offers = {}
tasks = set()


# ── утилиты ─────────────────────────────────────────────────

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


async def send(chat, text, kb=None):
    params = dict(chat_id=chat, text=text, parse_mode="HTML",
                  link_preview_options={"is_disabled": True})
    if kb:
        params["reply_markup"] = kb
    return await api("sendMessage", **params)


async def edit_offer(o, text, kb=None):
    params = dict(text=text, parse_mode="HTML",
                  link_preview_options={"is_disabled": True},
                  reply_markup=kb or {"inline_keyboard": []})
    if o.get("inline_message_id"):
        params["inline_message_id"] = o["inline_message_id"]
    else:
        params["chat_id"], params["message_id"] = o["chat"], o["mid"]
    return await api("editMessageText", **params)


def save():
    for oid in sorted(offers, key=lambda k: offers[k]["created"])[:-500]:
        del offers[oid]
    tmp = f"{DATA_FILE}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"offers": offers}, f, ensure_ascii=False)
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
    offers.update(data.get("offers", {}))


# ── разбор команды ──────────────────────────────────────────

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
    stars = int(re.sub(r"[_,]", "", amount.group()))
    if not 1 <= stars <= MAX_STARS:
        return None, f"Сумма должна быть от 1 до {num(MAX_STARS)} звёзд."
    return {"slug": slug, "name": name, "stars": stars}, None


# ── тексты и кнопки оффера ──────────────────────────────────

def who(o):
    u = o["maker"]
    return f"@{u['username']}" if u.get("username") else u["name"] or "покупатель"


def gift_link(o):
    return f'<a href="https://t.me/nft/{esc(o["slug"])}">{esc(o["name"])}</a>'


def stars(o):
    return f"{num(o['stars'])}{NBSP}⭐"


def left_text(seconds):
    minutes = math.ceil(max(0, seconds) / 60)
    h, m = divmod(minutes, 60)
    return f"{h} ч. {m} мин." if h else f"{m} мин."


def offer_text(o):
    text = (f"Пользователь предлагает Вам {stars(o)} Звёзд за подарок {gift_link(o)}.\n\n"
            f"Предложение действует ещё {left_text(o['expires'] - time.time())}")
    return text + (f"\n\n{esc(TEST_NOTICE)}" if TEST_NOTICE else "")


def sold_text(o):
    return (f"✅ Подарок {gift_link(o)} был успешно продан за {stars(o)}!\n\n"
            f"Осталось передать подарок покупателю — нажми «Как передать», "
            f"там пошаговая инструкция.")


def how_text(o):
    u = o["maker"]
    mention = f"@{u['username']}" if u.get("username") else esc(u.get("name") or "покупатель")
    return (f"<b>Как передать подарок</b>\n\n"
            f"Покупатель {mention} уже принял оффер и ждёт подарок.\n\n"
            f"1️⃣ Открой профиль покупателя: {mention}\n"
            f"2️⃣ Нажми <b>«Подарить подарок»</b> (или «Передать NFT»).\n"
            f"3️⃣ Выбери <b>{esc(o['name'])}</b> из своих подарков.\n"
            f"4️⃣ Подтверди передачу.\n\n"
            f"После передачи {esc(BRAND)} автоматически зачислит "
            f"<b>{num(o['stars'])}{NBSP}⭐</b> на твой баланс.\n\n"
            f"⏳ У тебя есть <b>{left_text(o['expires'] - time.time())}</b>, "
            f"пока оффер не истечёт.")


def declined_text(o):
    return f"❌ Предложение {stars(o)} за подарок {gift_link(o)} отклонено."


def expired_text(o):
    return f"⌛ Предложение {stars(o)} за подарок {gift_link(o)} истекло."


B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def b36(n):
    n = int(n)
    if n == 0:
        return "0"
    out = []
    while n:
        n, r = divmod(n, 36)
        out.append(B36[r])
    return "".join(reversed(out))


def ub36(s):
    return int(s, 36)


def inline_cb(o, action):
    u = o["maker"]
    username = u.get("username") or ""
    label = (username or u.get("name") or "")[:16]
    data = f"oi:{action}:{o['slug']}:{b36(o['stars'])}:{b36(u['id'])}:{'u' if username else 'n'}{label}"
    return data if len(data.encode()) <= 64 else None


def cb(o, action):
    token = inline_cb(o, action)
    if token:
        return token
    # слишком длинный слаг/имя не влез в 64 байта — держим как обычный оффер в памяти
    now = time.time()
    o = {**o, "mode": "stored", "chat": None, "mid": None,
         "status": "pending", "created": now, "expires": now + OFFER_TTL}
    offers[o["id"]] = o
    save()
    return f"of:{action}:{o['id']}"


def offer_kb(o):
    return {"inline_keyboard": [[btn("Отклонить", cb(o, "no")), btn("Принять", cb(o, "ok"))]]}


def sold_kb(o):
    u = o["maker"]
    if u.get("username"):
        transfer = {"text": f"Передать NFT @{u['username']}", "url": f"https://t.me/{u['username']}"}
    else:
        transfer = {"text": "Передать NFT", "url": f"tg://user?id={u['id']}"}
    return {"inline_keyboard": [[transfer], [btn("Как передать", cb(o, "how"))]]}


def back_kb(o):
    return {"inline_keyboard": [[btn("Вернуться к передаче", cb(o, "back"))]]}


# ── оффер: создание и жизненный цикл ────────────────────────

async def create_offer(arg, maker, chat, to_name):
    """Шлёт оффер в chat. Ошибки уходят только в личку с ботом, собеседник их не видит."""
    parsed, error = parse_offer(arg)
    if error:
        return await send(chat, f"⚠️ {error}\n\n{USAGE}")
    now = time.time()
    o = {"id": uuid.uuid4().hex[:8], **parsed, "chat": chat, "mid": None,
         "inline_message_id": None, "maker": maker, "to": to_name,
         "status": "pending", "created": now, "expires": now + OFFER_TTL}
    res = await send(chat, offer_text(o), offer_kb(o))
    if not res.get("ok"):
        return await send(chat, "⚠️ Не получилось отправить оффер в чат: "
                                f"{esc(res.get('description', 'неизвестная ошибка'))}")
    o["mid"] = res["result"]["message_id"]
    offers[o["id"]] = o
    save()


FINAL = {"accepted": (sold_text, sold_kb, "принят"),
         "declined": (declined_text, None, "отклонён"),
         "expired": (expired_text, None, "истёк")}


async def settle(o, status):
    text, kb, label = FINAL[status]
    o["status"] = status
    save()
    await edit_offer(o, text(o), kb(o) if kb else None)


async def sweeper():
    while True:
        await asyncio.sleep(60)
        for o in list(offers.values()):
            if o["status"] == "pending" and o["expires"] < time.time():
                try:
                    await settle(o, "expired")
                except Exception:
                    log.exception("истечение %s", o["id"])


def decode_inline_cb(data):
    _, action, slug, stars_b36, maker_b36, flaglabel = data.split(":", 5)
    maker = {"id": ub36(maker_b36), "username": None, "name": ""}
    if flaglabel[:1] == "u":
        maker["username"] = flaglabel[1:]
    else:
        maker["name"] = flaglabel[1:]
    return action, {"id": None, "slug": slug, "name": gift_name(slug) or slug,
                    "stars": ub36(stars_b36), "mode": "inline", "maker": maker,
                    "inline_message_id": None}


async def on_inline_cb(q):
    try:
        action, o = decode_inline_cb(q["data"])
    except (ValueError, IndexError):
        return "Повреждённые данные оффера"
    o["inline_message_id"] = q.get("inline_message_id")
    if STRICT and q["from"]["id"] == o["maker"]["id"] and action in ("ok", "no"):
        return "Это твой оффер: отвечать на него должен получатель"
    if action == "ok":
        await edit_offer(o, sold_text(o), sold_kb(o))
    elif action == "no":
        await edit_offer(o, declined_text(o), None)
    elif action == "how":
        await edit_offer(o, how_text(o), back_kb(o))
    elif action == "back":
        await edit_offer(o, sold_text(o), sold_kb(o))
    return None


async def on_stored_cb(q):
    parts = q.get("data", "").split(":")
    o = offers.get(parts[2]) if len(parts) == 3 else None
    if o is None:
        return "Этот оффер устарел"
    if o.get("chat") is None and not o.get("inline_message_id") and q.get("inline_message_id"):
        o["inline_message_id"] = q["inline_message_id"]
        save()
    action = parts[1]
    if STRICT and q["from"]["id"] == o["maker"]["id"] and action in ("ok", "no"):
        return "Это твой оффер: отвечать на него должен получатель"
    if action in ("ok", "no"):
        if o["status"] != "pending":
            return "Оффер уже обработан"
        if time.time() > o["expires"]:
            await settle(o, "expired")
            return "Срок предложения истёк"
        await settle(o, "accepted" if action == "ok" else "declined")
    elif o["status"] == "accepted" and action == "how":
        await edit_offer(o, how_text(o), back_kb(o))
    elif o["status"] == "accepted" and action == "back":
        await edit_offer(o, sold_text(o), sold_kb(o))
    return None


async def on_callback(q):
    data = q.get("data", "")
    if data == "try:demo":
        frm = q["from"]
        maker = {"id": frm["id"], "name": frm.get("first_name", ""),
                 "username": frm.get("username")}
        await create_offer("https://t.me/nft/DurovsCap-1234 500", maker,
                           q["message"]["chat"]["id"], "предпросмотр")
        return await api("answerCallbackQuery", callback_query_id=q["id"],
                         text="Отправил пример оффера 👇")
    note = await (on_inline_cb(q) if data.startswith("oi:") else on_stored_cb(q))
    extra = {"text": note, "show_alert": True} if note else {}
    await api("answerCallbackQuery", callback_query_id=q["id"], **extra)


# ── инлайн-режим ────────────────────────────────────────────

def inline_preview(oid, parsed, frm):
    return {"id": oid, **parsed, "mode": "inline",
            "maker": {"id": frm["id"], "name": frm.get("first_name", ""),
                      "username": frm.get("username")},
            "expires": time.time() + OFFER_TTL}


async def on_inline_query(q):
    query = q.get("query", "").strip()
    if not query:
        return await api("answerInlineQuery", inline_query_id=q["id"], results=[],
                         cache_time=1, switch_pm_text="Как создать оффер",
                         switch_pm_parameter="help")
    parsed, error = parse_offer(query)
    if error:
        result = {"type": "article", "id": "err",
                  "title": "Не распознал ссылку или сумму",
                  "description": error,
                  "input_message_content": {
                      "message_text": f"⚠️ {esc(error)}\n\n{USAGE}",
                      "parse_mode": "HTML"}}
        return await api("answerInlineQuery", inline_query_id=q["id"],
                         results=[result], cache_time=1)
    oid = uuid.uuid4().hex[:8]
    o = inline_preview(oid, parsed, q["from"])
    result = {"type": "article", "id": oid,
              "title": f"Оффер: {parsed['name']}",
              "description": f"{num(parsed['stars'])} ⭐ — нажми, чтобы отправить сюда",
              "input_message_content": {
                  "message_text": offer_text(o), "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}},
              "reply_markup": offer_kb(o)}
    await api("answerInlineQuery", inline_query_id=q["id"],
              results=[result], cache_time=1)


# ── личка с ботом ───────────────────────────────────────────

def start_text():
    return (
        f"👋 <b>Оффер-бот @{esc(bot_name)}</b>\n\n"
        "Создаёт карточки-офферы на NFT: цена + кнопки «Принять» / «Отклонить», "
        "как в самом Telegram.\n\n"
        "<b>Как отправить оффер:</b>\n"
        f"В любом чате набери <code>@{esc(bot_name)}</code> и через пробел ссылку и сумму:\n"
        f"<code>@{esc(bot_name)} https://t.me/nft/DurovsCap-1234 500</code>\n\n"
        "Сверху появится карточка — тапни её, и оффер уйдёт в чат.\n\n"
        "<b>Попробуй прямо сейчас</b> — жми кнопку ниже, я пришлю пример оффера "
        "в этот чат."
    )


def start_kb():
    return {"inline_keyboard": [
        [{"text": "🧪 Попробовать предпросмотр", "callback_data": "try:demo"}],
        [{"text": "📋 Скопировать ник бота",
          "copy_text": {"text": f"@{bot_name}"}}],
    ]}


async def on_message(m):
    chat, frm = m["chat"]["id"], m.get("from") or {}
    cmd, _, arg = (m.get("text") or "").strip().partition(" ")
    cmd = cmd.split("@")[0].lower()
    if cmd in ("/start", "/help"):
        await send(chat, start_text(), start_kb())
    elif cmd == "/offer":
        if not arg.strip():
            return await send(chat, USAGE)
        maker = {"id": frm.get("id", 0), "name": frm.get("first_name", ""),
                 "username": frm.get("username")}
        await create_offer(arg, maker, chat, "предпросмотр")


async def handle(update):
    try:
        if "callback_query" in update:
            await on_callback(update["callback_query"])
        elif "inline_query" in update:
            await on_inline_query(update["inline_query"])
        elif "message" in update:
            await on_message(update["message"])
    except Exception:
        log.exception("апдейт %s", update.get("update_id"))


# ── запуск ──────────────────────────────────────────────────

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
            data = await api("getUpdates", offset=offset, timeout=30,
                             allowed_updates=UPDATES)
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
    if me.get("supports_inline_queries") is False:
        log.warning("У бота выключен инлайн-режим: BotFather → /setinline → включить для @%s", bot_name)
    await api("setMyCommands", commands=[
        {"command": "start", "description": "Подсказки и предпросмотр"},
        {"command": "offer", "description": "Предпросмотр оффера: ссылка сумма"}])
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
