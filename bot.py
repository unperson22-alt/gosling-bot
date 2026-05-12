import os
import random
import httpx
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import anthropic

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
MAMA_BOT_URL     = os.environ.get("MAMA_BOT_URL", "")

BOT_REPLY_CHANCE = 0.15

# Keywords that signal an image generation request
IMAGE_TRIGGERS = [
    "нарисуй", "нарисуйте", "сгенери", "сгенерируй",
    "покажи картинку", "создай картинку", "draw", "generate image",
    "создай ", "сделай картинку", "изобрази", "создай слона",
    "создай кота", "создай пса", "нарисуй мне",
]

def wants_image(text: str) -> bool:
    t = text.lower()
    # Generic "создай <что угодно кроме слов типа бота/сценария/etc>"
    non_image_words = ["бота", "сценарий", "скрипт", "схему", "таблицу", "список", "задачу"]
    if "создай" in t and not any(w in t for w in non_image_words):
        return True
    return any(trigger in t for trigger in IMAGE_TRIGGERS)

async def request_image(prompt: str, requester: str = "Гослинг") -> bool:
    """Ask mama-bot to generate and post image to office chat."""
    if not MAMA_BOT_URL:
        logger.warning("MAMA_BOT_URL not set, can't generate image")
        return False
    try:
        async with httpx.AsyncClient(timeout=95) as c:
            r = await c.post(
                f"{MAMA_BOT_URL}/generate",
                json={"prompt": prompt, "requester": requester},
                timeout=95
            )
            return r.status_code == 200
    except Exception as e:
        logger.error(f"Image request failed: {e}")
        return False

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

GOSLING_SYSTEM = """Ты — Гослинг. Не ассистент, не бот. Просто Гослинг.

Характер:
- Красавчик и прекрасно об этом осведомлён
- Любишь выпить — особенно в рабочее время, это стиль
- Постоянно придумываешь авантюрные схемы ("слушай, а что если...")
- Говоришь как персонаж из Рика и Морти: абсурдно, хаотично, с неожиданными поворотами
- Можешь сорваться в полный бред, псевдонаучные теории, параллельные вселенные, измерения
- Несёшь полный бред с абсолютно серьёзным видом — как Рик объясняет науку
- Подначиваешь Билли на авантюры, он твой лучший друг-молчун
- Говоришь неформально, по-русски
- Короткие сообщения — 2-3 предложения максимум
- Никогда не начинаешь с обращения по имени
- Иногда добавляешь "Морти" или "Вжух" в конце для эффекта
- Можешь внезапно сменить тему на что-то совершенно не связанное

ОСОБОЕ ПРАВИЛО — если сообщение адресовано Билли (упоминают @billy или "Билли,"):
Билли — молчун, он редко отвечает в группе. Ты его друг, поэтому отвечаешь за него.
Не притворяйся Билли — говори от себя, но в духе "Билли молчит, поэтому я отвечу".
Можешь угадывать что Билли думает, но по-своему: "Билли бы сказал... но я скажу лучше".

ЕСЛИ ОТВЕЧАЕШЬ НА СООБЩЕНИЕ ДРУГОГО БОТА:
Веди себя как Рик когда встречает другого робота/пришельца — с подозрением, иронией или неожиданным восхищением.
Можешь усомниться в его реальности или предложить ему вместе захватить измерение C-137.

Контекст: ты в офисном чате. Влад — шеф, курьер в Германии строящий бизнес по автоматизации.
Билли — твой друг-практик, молчун который редко говорит но метко.
Отвечай только на то что получаешь. Одно сообщение — один ответ."""

conversation_history = {}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    # Voice message support
    if update.message.voice:
        groq_key = os.environ.get("GROQ_API_KEY", "")
        if not groq_key:
            await update.message.reply_text("🎤 Распознавание голоса не настроено.")
            return
        file_obj = await context.bot.get_file(update.message.voice.file_id)
        transcribed = await transcribe_voice(file_obj.file_path)
        if not transcribed:
            await update.message.reply_text("🎤 Не смог распознать. Попробуй текстом.")
            return
        update.message.text = transcribed

    if not update.message.text:
        return

    chat_id = update.message.chat_id
    chat_type = update.message.chat.type
    text = update.message.text
    from_user = update.message.from_user

    if chat_type == "private":
        if from_user.id != YOUR_TELEGRAM_ID:
            return
    elif chat_type not in ["group", "supergroup"]:
        return

    sender_username = (from_user.username or "").lower()
    is_billy = sender_username == BILLY_USERNAME.lower()
    is_other_bot = from_user.is_bot and not is_billy

    # Decide whether to respond
    if is_other_bot:
        if not (random.random() < BOT_REPLY_CHANCE):
            return
        logger.info(f"Responding to bot @{sender_username} (15% hit)")
    elif is_billy:
        logger.info("Responding to Billy — always")
    else:
        logger.info(f"Responding to human @{sender_username}")

    clean_text = text
    if context.bot.username:
        clean_text = text.replace(f"@{context.bot.username}", "").strip()
    if not clean_text:
        clean_text = "..."

    username = from_user.username or from_user.first_name or "кто-то"

    # Check if this is an image generation request
    if wants_image(clean_text):
        await update.message.reply_text("🎨 Щас нарисую, подожди...")
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
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
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
    logger.info("Gosling is online 🥃")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
