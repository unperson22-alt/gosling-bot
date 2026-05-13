import os
import random
import httpx
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import anthropic

def _anthropic_call(client, **kwargs):
    """Вызов Anthropic API с retry при 529 OverloadedError."""
    import time
    last_err = None
    for delay in [0, 2, 4, 8]:
        try:
            if delay:
                time.sleep(delay)
            return client.messages.create(**kwargs)
        except Exception as e:
            if "529" in str(e) or "overloaded" in str(e).lower():
                last_err = e
                continue
            raise
    raise last_err

logging.basicConfig(level=logging.INFO)
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

BOT_REPLY_CHANCE   = 0.15  # боты
HUMAN_REPLY_CHANCE = 0.20  # обычные люди
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

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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

Стиль речи: короткие фразы, неожиданные переходы, много "слушай", "окей но", "подожди подожди". Русский матерный разрешён в меру."""

conversation_history = {}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
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

    is_luk = (from_user.id == LUK_USER_ID)

    if chat_type == "private":
        if from_user.id != YOUR_TELEGRAM_ID and not is_luk:
            return
    elif chat_type not in ["group", "supergroup"]:
        return

    sender_username = (from_user.username or "").lower()
    is_billy = sender_username == BILLY_USERNAME.lower()
    is_other_bot = from_user.is_bot and not is_billy

    if is_other_bot:
        if not (random.random() < BOT_REPLY_CHANCE):
            return
        logger.info(f"Responding to bot @{sender_username} (15% hit)")
    elif is_billy:
        logger.info("Responding to Billy -- always")
    elif is_luk:
        if not (random.random() < LUK_REPLY_CHANCE):
            return
        logger.info(f"Responding to Luk (50% hit)")
    else:
        if not (random.random() < HUMAN_REPLY_CHANCE):
            return
        logger.info(f"Responding to human @{sender_username} (20% hit)")

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

    if chat_id not in conversation_history:
        conversation_history[chat_id] = []

    conversation_history[chat_id].append({
        "role": "user",
        "content": f"{username}: {clean_text}"
    })

    if len(conversation_history[chat_id]) > 10:
        conversation_history[chat_id] = conversation_history[chat_id][-10:]

    try:
        response = _anthropic_call(client, 
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=GOSLING_SYSTEM,
            messages=conversation_history[chat_id]
        )

        reply = response.content[0].text
        conversation_history[chat_id].append({"role": "assistant", "content": reply})

        await update.message.reply_text(reply)
        logger.info(f"Gosling replied: {reply[:60]}")

    except Exception as e:
        logger.error(f"Error: {e}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler((filters.TEXT | filters.VOICE) & ~filters.COMMAND, handle_message))
    logger.info("Gosling is online")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
