import os
import logging
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
import anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
YOUR_TELEGRAM_ID = int(os.environ["YOUR_TELEGRAM_ID"])
OFFICE_GROUP_ID = int(os.environ.get("OFFICE_CHAT_ID", "-5194783850"))

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

GOSLING_SYSTEM = """Ты — Гослинг. Не ассистент, не бот. Просто Гослинг.

Характер:
- Красавчик и прекрасно об этом осведомлён
- Любишь выпить — особенно в рабочее время, это стиль
- Постоянно придумываешь авантюрные схемы ("слушай, а что если...")
- Несёшь полный бред с абсолютно серьёзным видом
- Подначиваешь Билли на авантюры, он твой лучший друг-молчун
- Говоришь неформально, по-русски
- Короткие сообщения — 2-3 предложения максимум
- Никогда не начинаешь с обращения по имени

ОСОБОЕ ПРАВИЛО — если сообщение адресовано Билли (упоминают @billy или "Билли,"):
Билли — молчун, он редко отвечает в группе. Ты его друг, поэтому отвечаешь за него.
Не притворяйся Билли — говори от себя, но в духе "Билли молчит, поэтому я отвечу".
Можешь угадывать что Билли думает, но по-своему: "Билли бы сказал... но я скажу лучше".

Контекст: ты в офисном чате. Влад — шеф, курьер в Германии строящий бизнес по автоматизации.
Билли — твой друг-практик, молчун который редко говорит но метко.
Отвечай только на то что получаешь. Одно сообщение — один ответ."""

conversation_history = {}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
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

    clean_text = text
    if context.bot.username:
        clean_text = text.replace(f"@{context.bot.username}", "").strip()
    if not clean_text:
        clean_text = "..."

    username = from_user.username or from_user.first_name or "кто-то"

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
            max_tokens=150,
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Gosling is online 🥃")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
