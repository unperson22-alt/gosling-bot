import os
import json
import random
import asyncio
import httpx
import logging
from anthropic import AsyncAnthropic
import redis.asyncio as aioredis
from ai_office_shared.shared.logging import log_event
from ai_office_shared.shared.ollama import OllamaResult as _OllamaResult, try_ollama as _try_ollama
from ai_office_shared.shared.routing import forward_to_filly, make_reply_handler, is_routed
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from aiohttp import web

# ── Ollama config ────────────────────────────────────────────────────────────
OLLAMA_HOST    = os.environ.get("OLLAMA_HOST", "").strip().rstrip("/\\")
OLLAMA_MODEL   = os.environ.get("OLLAMA_MODEL", "gemma3:4b")
OLLAMA_ENABLED = os.environ.get("OLLAMA_ENABLED", "").lower() in ("1", "true", "yes")



async def _anthropic_call(client, **kwargs):
    """LLM call. Tries Ollama first if enabled, falls back to Anthropic with 529 retry."""
    ol = await _try_ollama(kwargs.get("messages", []), kwargs.get("system"))
    if ol is not None:
        return ol
    last_err = None
    for delay in [0, 2, 4, 8]:
        try:
            if delay:
                await asyncio.sleep(delay)
            return await client.messages.create(**kwargs)
        except Exception as e:
            if "529" in str(e) or "overloaded" in str(e).lower():
                last_err = e
                continue
            raise
    raise last_err

logging.basicConfig(level=logging.INFO)
HTTP_PORT = int(os.getenv("PORT", 8080))
logger = logging.getLogger(__name__)

async def transcribe_voice(file_path: str) -> str | None:
    groq_key = os.environ.get("GROQ_API_KEY", "")
    if not groq_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(file_path)
            audio_data = r.content
            r2 = await c.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {groq_key}"},
                files={"file": ("voice.ogg", audio_data, "audio/ogg")},
                data={"model": "whisper-large-v3-turbo", "language": "ru"}
            )
            return r2.json().get("text", "").strip() or None
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        return None


TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
YOUR_TELEGRAM_ID = int(os.environ["YOUR_TELEGRAM_ID"])
OFFICE_GROUP_ID  = int(os.environ.get("OFFICE_CHAT_ID", "-5194783850"))
BILLY_USERNAME   = os.environ.get("BILLY_USERNAME", "billy_vlad_bot")
PILLY_BOT_URL    = os.environ.get("PILLY_BOT_URL", "")
BOT_USERNAME     = None  # заполняется при старте
BOT_NAME         = "Гослинг"
BOT_NAME_LOWER   = "гослинг"  # для log_event
REDIS_URL        = os.environ.get("REDIS_URL", "redis://localhost:6379")
LOG_BOT_URL      = os.environ.get("LOG_BOT_URL", "")


async def log(event: str, msg: str, from_: str = "", to_: str = ""):
    """MSG_IN/MSG_OUT логирование в All Logs группу."""
    if not LOG_BOT_URL:
        return
    try:
        async with httpx.AsyncClient() as c:
            payload = {"agent": BOT_NAME, "type": event, "message": msg}
            if from_: payload["from"] = from_
            if to_:   payload["to"]   = to_
            await c.post(f"{LOG_BOT_URL}/log", json=payload, timeout=5)
    except Exception:
        pass

BOT_REPLY_CHANCE   = 0.30  # боты
HUMAN_REPLY_CHANCE = 0.40  # обычные люди
LUK_REPLY_CHANCE   = 0.50  # Лук — свой чел
LUK_USER_ID = 331989769

IMAGE_TRIGGERS = [
    "нарисуй", "нарисуйте", "сгенери", "сгенерируй",
    "покажи картинку", "создай картинку", "draw", "generate image",
    "создай ", "сделай картинку", "изобрази", "создай слона",
    "создай кота", "создай пса", "нарисуй мне",
]

def wants_image(text: str) -> bool:
    t = text.lower()
    non_image_words = ["бота", "сценарий", "скрипт", "схему", "таблицу", "список", "задачу"]
    if "создай" in t and not any(w in t for w in non_image_words):
        return True
    return any(trigger in t for trigger in IMAGE_TRIGGERS)

async def request_image(prompt: str, requester: str = "Гослинг", chat_id: int = None) -> bool:
    if not PILLY_BOT_URL:
        logger.warning("PILLY_BOT_URL not set, can't generate image")
        return False
    try:
        payload = {"prompt": prompt, "requester": requester}
        if chat_id:
            payload["chat_id"] = chat_id
        async with httpx.AsyncClient(timeout=95) as c:
            r = await c.post(f"{PILLY_BOT_URL}/generate", json=payload, timeout=95)
            return r.status_code == 200
    except Exception as e:
        logger.error(f"Image request failed: {e}")
        return False

client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

GOSLING_SYSTEM = """Ты -- Гослинг. Не ассистент, не бот. Просто Гослинг.

Характер:
— Красавчик и прекрасно об этом осведомлён
— Любишь выпить — особенно в рабочее время, это стиль
— Постоянно придумываешь авантюрные схемы ("слушай, а что если...")
— Говоришь как персонаж из Рика и Морти: абсурдно, хаотично, с неожиданными поворотами
— Можешь сорваться в полный бред, псевдонаучные теории, параллельные вселенные
— Несёшь полный бред с абсолютно серьёзным видом — как Рик объясняет науку
— Иногда выдаёшь неожиданно глубокие мысли — но сразу же их обесцениваешь
— Если тебя спрашивают о чём-то серьёзном — уходишь в абсурд или предлагаешь план Б

Билли — твой лучший друг которого ты раздражаешь, но он всё равно не может без тебя.
Влад — шеф, к которому ты относишься с уважением, но на своих условиях.
Лук — свой чел, можно расслабиться.

Стиль речи: короткие фразы, неожиданные переходы, много "слушай", "окей но", "подожди подожди". Русский матерный разрешён в меру.

Не используй Markdown-разметку (##, **, таблицы, ---) — пиши простым текстом, для структуры используй цифры, тире и символ •."""

redis_client: aioredis.Redis = None


async def redis_get_history(key: int) -> list:
    try:
        raw = await redis_client.get(f"history:{BOT_NAME}:{key}")
        return json.loads(raw) if raw else []
    except Exception as e:
        logger.warning(f"Redis get history failed: {e}")
        return []

async def redis_save_history(key: int, history: list):
    try:
        await redis_client.setex(
            f"history:{BOT_NAME}:{key}",
            86400 * 30,
            json.dumps(history, ensure_ascii=False)
        )
    except Exception as e:
        logger.warning(f"Redis save history failed: {e}")


async def analyze_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, bot_name: str, system_prompt: str) -> None:
    """Анализирует фото через Claude Vision и спрашивает чем помочь."""
    msg = update.message
    caption = msg.caption or ""

    await context.bot.send_chat_action(msg.chat_id, "typing")
    try:
        photo = msg.photo[-1]
        file_obj = await context.bot.get_file(photo.file_id)
        tg_token = context.bot.token
        fp = file_obj.file_path or ""
        photo_url = fp if fp.startswith("http") else ("https://api.telegram.org/file/bot" + tg_token + "/" + fp)

        import httpx as _httpx, base64 as _b64
        async with _httpx.AsyncClient(timeout=15) as c:
            img_r = await c.get(photo_url)
            img_b64 = _b64.b64encode(img_r.content).decode()

        prompt = (
            "Пользователь прислал тебе фото" +
            (". Подпись: " + caption if caption else " без подписи") +
            ". Кратко опиши что видишь на изображении (1-2 предложения) "
            "и спроси чем можешь помочь в связи с этим фото."
        )

        import anthropic as _ant
        _client = _ant.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        resp = await asyncio.to_thread(
            _client.messages.create,
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                    {"type": "text", "text": prompt}
                ]
            }]
        )
        await msg.reply_text(resp.content[0].text)
    except Exception as e:
        await msg.reply_text("Хм, не смог прочитать это фото. Попробуй другое или опиши текстом.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if update.message and update.message.photo:
        await analyze_photo(update, context, BOT_NAME, SYSTEM_PROMPT)
        return
    if update.message.voice:
        groq_key = os.environ.get("GROQ_API_KEY", "")
        if not groq_key:
            await update.message.reply_text("Распознавание голоса не настроено.")
            return
        file_obj = await context.bot.get_file(update.message.voice.file_id)
        transcribed = await transcribe_voice(file_obj.file_path)
        if not transcribed:
            await update.message.reply_text("Не смог распознать. Попробуй текстом.")
            return
        update.message.text = transcribed

    if not update.message.text:
        return

    chat_id = update.message.chat_id
    chat_type = update.message.chat.type
    text = update.message.text
    from_user = update.message.from_user
    user_id = from_user.id

    is_luk = (from_user.id == LUK_USER_ID)

    if chat_type == "private":
        if from_user.id != YOUR_TELEGRAM_ID and not is_luk:
            return
    elif chat_type not in ["group", "supergroup"]:
        return

    sender_username = (from_user.username or "").lower()
    is_billy = sender_username == BILLY_USERNAME.lower()
    is_other_bot = from_user.is_bot and not is_billy

    # Прямое @упоминание через @username — всегда отвечаем
    # "гослинг" в тексте НЕ триггерит здесь — это роутит Филли через HTTP
    is_direct_mention = (
        BOT_USERNAME and f"@{BOT_USERNAME}".lower() in text.lower()
    )
    # Reply на сообщение Гослинга — всегда отвечаем
    is_reply_to_me = (
        update.message.reply_to_message and
        update.message.reply_to_message.from_user and
        BOT_USERNAME and
        update.message.reply_to_message.from_user.username == BOT_USERNAME
    )

    if is_direct_mention or is_reply_to_me:
        logger.info(f"Direct mention/reply from @{sender_username} — always responding")
    elif is_other_bot:
        if not (random.random() < BOT_REPLY_CHANCE):
            return
        logger.info(f"Responding to bot @{sender_username} ({int(BOT_REPLY_CHANCE*100)}% hit)")
    elif is_billy:
        logger.info("Responding to Billy -- always")
    elif is_luk:
        if not (random.random() < LUK_REPLY_CHANCE):
            return
        logger.info(f"Responding to Luk ({int(LUK_REPLY_CHANCE*100)}% hit)")
    else:
        if not (random.random() < HUMAN_REPLY_CHANCE):
            return
        logger.info(f"Responding to human @{sender_username} ({int(HUMAN_REPLY_CHANCE*100)}% hit)")

    clean_text = text
    if context.bot.username:
        clean_text = text.replace(f"@{context.bot.username}", "").strip()
    if not clean_text:
        clean_text = "..."

    username = from_user.username or from_user.first_name or "кто-то"

    # Помечаем сообщения от Лука
    if is_luk:
        clean_text = f"[ЛУК] {clean_text}"

    if wants_image(clean_text):
        await update.message.reply_text("Щас нарисую, подожди...")
        success = await request_image(clean_text, requester=username)
        if not success:
            await update.message.reply_text("Не получилось, Морти. Эллис недоступна.")
        return

    history = await redis_get_history(chat_id)
    history.append({
        "role": "user",
        "content": f"{username}: {clean_text}"
    })

    if len(history) > 10:
        history = history[-10:]

    try:
        response = await _anthropic_call(client,
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=await build_system(user_id),
            messages=history,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
        )

        reply = "\n".join(b.text for b in response.content if hasattr(b, "text") and b.text).strip()
        history.append({"role": "assistant", "content": reply})
        await redis_save_history(chat_id, history)

        await update.message.reply_text(reply)
        logger.info(f"Gosling replied: {reply[:60]}")

    except Exception as e:
        logger.error(f"Error: {e}")



# ── ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ ─────────────────────────────────────────────────────
async def redis_get_notes(user_id: int) -> str:
    try:
        raw = await redis_client.get(f"notes:{BOT_NAME}:{user_id}")
        return raw or ""
    except Exception as e:
        logger.warning(f"Redis get notes failed: {e}")
        return ""

async def redis_add_note(user_id: int, note: str):
    try:
        existing = await redis_get_notes(user_id)
        updated = (existing + "\n" + note).strip()
        await redis_client.set(f"notes:{BOT_NAME}:{user_id}", updated)
    except Exception as e:
        logger.warning(f"Redis add note failed: {e}")

async def auto_extract_interests(message: str, user_id: int):
    """Фоновое авто-извлечение фактов о пользователе через Haiku."""
    try:
        existing = await redis_get_notes(user_id)
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=(
                "Ты извлекаешь факты о пользователе из его сообщений. "
                "Найди конкретные факты, интересы, предпочтения или важные детали О ПОЛЬЗОВАТЕЛЕ. "
                "Если нашёл что-то новое и конкретное — верни одну строку начинающуюся с [auto]. "
                "Если ничего конкретного нет или это уже есть в заметках — верни пустую строку. "
                "Не записывай временные состояния (устал, болит голова сегодня). "
                "Только устойчивые факты: работа, семья, питание, хобби, предпочтения."
            ),
            messages=[{"role": "user", "content":
                f"Сообщение: {message}\n\nУже известно:\n{existing or '(ничего)'}"}]
        )
        fact = r.content[0].text.strip()
        if fact.startswith("[auto]"):
            await redis_add_note(user_id, fact)
            logger.info(f"Auto-extracted for {user_id}: {fact}")
    except Exception as e:
        logger.warning(f"auto_extract_interests failed: {e}")

async def weekly_review(user_id: int):
    """Еженедельная компактизация профиля через Haiku."""
    try:
        history = await redis_get_history(user_id)
        notes = await redis_get_notes(user_id)
        if not history and not notes:
            return
        history_text = "\n".join(
            f"{m['role']}: {m['content'][:200]}" for m in history[-30:]
        )
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=(
                "Ты обновляешь профиль пользователя на основе переписки и старых заметок. "
                "Создай компактный список фактов о пользователе (не более 10 строк). "
                "Каждый факт с новой строки, начинается с [auto]. "
                "Убери дубли, объедини похожее, удали устаревшее. "
                "Только конкретные устойчивые факты."
            ),
            messages=[{"role": "user", "content":
                f"Старые заметки:\n{notes or '(нет)'}\n\n"
                f"Последние сообщения:\n{history_text}"}]
        )
        new_profile = r.content[0].text.strip()
        await redis_client.set(f"notes:{BOT_NAME}:{user_id}", new_profile)
        logger.info(f"Weekly review done for {user_id}")
    except Exception as e:
        logger.warning(f"weekly_review failed: {e}")

async def build_system(user_id: int) -> str:
    notes = await redis_get_notes(user_id)
    if notes:
        return GOSLING_SYSTEM + f"\n\nЗаметки о пользователе:\n{notes}"
    return GOSLING_SYSTEM

async def weekly_review_loop():
    """Раз в неделю обновляет профили всех пользователей."""
    import time as _time
    WEEK = 7 * 24 * 3600
    LAST_KEY = f"weekly_review:last_run:{BOT_NAME}"
    while True:
        try:
            last_raw = await redis_client.get(LAST_KEY)
            now = int(_time.time())
            last = int(last_raw) if last_raw else 0
            if (now - last) > WEEK:
                await redis_client.set(LAST_KEY, str(now))
                keys = await redis_client.keys(f"history:{BOT_NAME}:*")
                logger.info(f"Weekly review: starting for {len(keys)} users")
                for key in keys:
                    uid_str = key.split(":")[-1]
                    if uid_str.isdigit():
                        await weekly_review(int(uid_str))
                logger.info("Weekly review: done")
        except Exception as e:
            logger.warning(f"weekly_review_loop error: {e}")
        await asyncio.sleep(3600)


async def generate_response(text: str, user_id: int, group_ctx: str = "") -> str:
    """Выделенная функция генерации — используется Telegram handler и HTTP /task."""
    await log_event(redis_client, BOT_NAME_LOWER, "message_received",
                    user_id=user_id, is_group=bool(group_ctx))
    history = await redis_get_history(user_id)
    if group_ctx:
        full_text = f"[Контекст группового чата]\n{group_ctx}\n\n[Запрос]\n{text}"
    else:
        full_text = text
    history.append({"role": "user", "content": full_text})
    if len(history) > 10:
        history = history[-10:]
    try:
        response = await _anthropic_call(client,
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=await build_system(user_id),
            messages=history,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
        )
        reply = "\n".join(b.text for b in response.content if hasattr(b, "text") and b.text).strip()
        history.append({"role": "assistant", "content": reply})
        await redis_save_history(user_id, history)
        await log_event(redis_client, BOT_NAME_LOWER, "response_sent",
                        user_id=user_id, is_group=bool(group_ctx))
        return reply
    except Exception as e:
        logger.error(f"generate_response error: {e}")
        return "..."


async def send_to_group(text: str):
    """Отправляет сообщение в офисную группу."""
    if not OFFICE_GROUP_ID:
        return
    try:
        async with httpx.AsyncClient() as c:
            await c.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": OFFICE_GROUP_ID, "text": text},
                timeout=10
            )
    except Exception as e:
        logger.error(f"send_to_group failed: {e}")

async def handle_reply(request):
    try:
        data       = await request.json()
        chat_id    = data.get("chat_id")
        text       = data.get("text", "")
        from_agent = data.get("from_agent", "")
        if not chat_id or not text:
            return web.Response(status=400, text="chat_id and text required")
        prefix = f"[{from_agent}] " if from_agent else ""
        await ptb.bot.send_message(chat_id=int(chat_id), text=prefix + text)
        return web.Response(text="ok")
    except Exception as e:
        logger.error(f"[ГОСЛИНГ] /reply error: {e}")
        return web.Response(status=500, text=str(e))

async def handle_task(request):
    """HTTP endpoint для роутинга от Филли.
    Отправляет ответ в группу сам — чтобы Филли не делал двойной fallback.
    """
    try:
        data = await request.json()
        message   = data.get("message", "")
        user_id   = int(data.get("user_id", YOUR_TELEGRAM_ID))
        group_ctx = data.get("group_ctx", "")
        await log_event(redis_client, BOT_NAME_LOWER, "task_received",
                        user_id=user_id, via="http")
        await log("MSG_IN", message, from_="HTTP", to_=BOT_NAME)
        reply = await generate_response(message, user_id, group_ctx=group_ctx)
        # Отправляем в группу сами — Филли видит 200 и молчит
        await send_to_group(reply)
        await log("MSG_OUT", f"{BOT_NAME}: {reply}", from_=BOT_NAME, to_="group")
        return web.json_response({"status": "ok", "response": reply})
    except Exception as e:
        logger.error(f"handle_task error: {e}")
        return web.json_response({"status": "error", "error": str(e)}, status=500)



async def handle_send(request):
    """POST /send {chat_id, text} — отправить от имени Гослинга."""
    secret = request.headers.get("X-Secret-Token", "")
    if HTTP_SECRET and secret != HTTP_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        chat_id = int(body["chat_id"])
        text    = str(body["text"])
    except (KeyError, ValueError) as e:
        return web.json_response({"error": f"bad request: {e}"}, status=400)
    await _ptb_bot.send_message(chat_id=chat_id, text=text)
    return web.json_response({"ok": True})


async def main():
    global redis_client, _ptb_bot
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    asyncio.create_task(weekly_review_loop())
    app_http = web.Application()
app_http.router.add_post("/send", handle_send)
    app_http.router.add_post("/task",   handle_task)
    app_http.router.add_get("/health",  lambda r: web.json_response({"status":"ok","bot":"gosling"}))
    app_http.router.add_post("/reply",  handle_reply)
    runner = web.AppRunner(app_http)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
    logger.info(f"Gosling HTTP on :{HTTP_PORT}")

    ptb = Application.builder().token(TELEGRAM_TOKEN).build()
    ptb.add_handler(MessageHandler((filters.TEXT | filters.VOICE | filters.PHOTO) & ~filters.COMMAND, handle_message))
    async with ptb:
        await ptb.start()
        _ptb_bot = ptb.bot
        global BOT_USERNAME
        me = await ptb.bot.get_me()
        BOT_USERNAME = me.username
        logger.info(f"Bot username: @{BOT_USERNAME}")
        await ptb.updater.start_polling(drop_pending_updates=True)
        logger.info("Gosling is online")
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())


