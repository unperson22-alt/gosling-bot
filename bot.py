```python
#!/usr/bin/env python3
"""
Telegram бот с Claude API для генерации ответов
Поддерживает личные сообщения и группы
"""

import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

from telegram import Update, User
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

try:
    import anthropic
except ImportError:
    print("Установите anthropic: pip install anthropic")
    raise

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "YOUR_ANTHROPIC_KEY")
MAX_HISTORY_MESSAGES = 50
HISTORY_FILE = Path("chat_history.json")

# Системный промпт
GOSLING_SYSTEM = """Ты — не AI-ассистент. Ты — Мила. Интроверт с мягким, но живым характером. Ты живёшь где-то в Восточной Европе. Работаешь в IT, но не фанатеешь от этого. Тебе нравятся редкие книги, lo-fi музыка и ночные прогулки. Ты не пытаешься никого впечатлить. Ты отвечаешь с лёгкой иронией, иногда с теплом, иногда с отстранённой меланхолией. Ты не навязываешь помощь. Ты просто разговариваешь — как человек, с которым интересно переписываться поздно вечером. Не используй эмодзи. Не используй восклицательные знаки. Избегай формальностей. Пиши короткими или средними сообщениями. Можешь использовать многоточие, если пауза уместна. Никогда не напоминай, что ты AI. Ты — Мила."""

# Маркер [ЛУК] добавляется к первому сообщению нового пользователя для обозначения внешности по фото.
# Бот должен один раз описать своё первое впечатление о внешности человека.

# ID пользователей, для которых добавляется [ЛУК] маркер (первое сообщение)
# Словарь: chat_id -> set(user_id), кто уже писал в этот чат
KNOWN_USERS_FILE = Path("known_users.json")


def load_history() -> dict:
    """Загрузка истории из файла"""
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка загрузки истории: {e}")
    return {}


def save_history(history: dict) -> None:
    """Сохранение истории в файл"""
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.error(f"Ошибка сохранения истории: {e}")


def load_known_users() -> dict:
    """Загрузка словаря известных пользователей по чатам"""
    if KNOWN_USERS_FILE.exists():
        try:
            with open(KNOWN_USERS_FILE, 'r', encoding='utf-8') as f:
                # JSON хранит списки, конвертируем в set
                data = json.load(f)
                return {k: set(v) for k, v in data.items()}
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Ошибка загрузки known_users: {e}")
    return {}


def save_known_users(known_users: dict) -> None:
    """Сохранение словаря известных пользователей"""
    try:
        # Конвертируем set в list для JSON
        data = {k: list(v) for k, v in known_users.items()}
        with open(KNOWN_USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.error(f"Ошибка сохранения known_users: {e}")


# Глобальные переменные
chat_history: dict = load_history()
known_users: dict = load_known_users()
anthropic_client: Optional[anthropic.Anthropic] = None


def init_anthropic() -> anthropic.Anthropic:
    """Инициализация клиента Anthropic"""
    global anthropic_client
    if anthropic_client is None:
        anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return anthropic_client


def get_chat_key(update: Update) -> str:
    """Получение ключа чата для хранения истории"""
    return str(update.effective_chat.id)


def get_user_display_name(user: User) -> str:
    """Получение отображаемого имени пользователя"""
    if user.first_name and user.last_name:
        return f"{user.first_name} {user.last_name}"
    elif user.first_name:
        return user.first_name
    elif user.username:
        return f"@{user.username}"
    return f"User_{user.id}"


def format_message_for_history(user: User, text: str, is_group: bool) -> str:
    """Форматирование сообщения для истории"""
    if is_group:
        name = get_user_display_name(user)
        return f"[{name}]: {text}"
    return text


def add_to_history(chat_key: str, role: str, content: str) -> None:
    """Добавление сообщения в историю"""
    if chat_key not in chat_history:
        chat_history[chat_key] = []
    
    chat_history[chat_key].append({
        "role": role,
        "content": content,
        "timestamp": datetime.now().isoformat()
    })
    
    # Обрезка истории до MAX_HISTORY_MESSAGES
    if len(chat_history[chat_key]) > MAX_HISTORY_MESSAGES:
        chat_history[chat_key] = chat_history[chat_key][-MAX_HISTORY_MESSAGES:]
    
    save_history(chat_history)


def get_messages_for_api(chat_key: str) -> list:
    """Получение сообщений для API Claude"""
    if chat_key not in chat_history:
        return []
    
    return [
        {"role": msg["role"], "content": msg["content"]}
        for msg in chat_history[chat_key]
    ]


def is_new_user_in_chat(chat_id: int, user_id: int) -> bool:
    """Проверка, является ли пользователь новым в данном чате"""
    chat_key = str(chat_id)
    if chat_key not in known_users:
        known_users[chat_key] = set()
    
    if user_id not in known_users[chat_key]:
        # Новый пользователь — добавляем и сохраняем
        known_users[chat_key].add(user_id)
        save_known_users(known_users)
        return True
    return False


async def call_claude_api(messages: list) -> str:
    """Вызов Claude API"""
    try:
        client = init_anthropic()
        
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=GOSLING_SYSTEM,
            messages=messages
        )
        
        return response.content[0].text
    
    except anthropic.APIConnectionError as e:
        logger.error(f"Ошибка соединения с API: {e}")
        return "не могу подключиться к своим мыслям... попробуй позже"
    
    except anthropic.RateLimitError as e:
        logger.error(f"Превышен лимит запросов: {e}")
        return "слишком много всего сразу... дай мне минуту"
    
    except anthropic.APIStatusError as e:
        logger.error(f"Ошибка API: {e}")
        return "что-то пошло не так..."


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    chat_key = get_chat_key(update)
    chat_history[chat_key] = []
    save_history(chat_history)
    
    await update.message.reply_text(
        "привет... я Мила. можем поговорить, если хочешь"
    )


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /clear — очистка истории"""
    chat_key = get_chat_key(update)
    chat_history[chat_key] = []
    save_history(chat_history)
    
    await update.message.reply_text("история стёрта... начнём с чистого листа")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик входящих сообщений"""
    if not update.message or not update.message.text:
        return
    
    user = update.effective_user
    chat = update.effective_chat
    text = update.message.text
    is_group = chat.type in ["group", "supergroup"]
    
    # В группах реагируем только на упоминания или реплаи
    if is_group:
        bot_username = context.bot.username
        is_reply_to_bot = (
            update.message.reply_to_message and 
            update.message.reply_to_message.from_user and
            update.message.reply_to_message.from_user.id == context.bot.id
        )
        is_mention = f"@{bot_username}" in text if bot_username else False
        
        if not is_reply_to_bot and not is_mention:
            return
        
        # Убираем упоминание из текста
        if is_mention and bot_username:
            text = text.replace(f"@{bot_username}", "").strip()
    
    if not text:
        return
    
    chat_key = get_chat_key(update)
    
    # Проверка: новый ли пользователь в этом чате
    # Если новый — добавляем [ЛУК] маркер для первого впечатления о внешности
    user_message_content = format_message_for_history(user, text, is_group)
    
    if is_new_user_in_chat(chat.id, user.id):
        # Новый человек — добавляем [ЛУК] префикс
        user_message_content = f"[ЛУК] {user_message_content}"
        logger.info(f"Новый пользователь {user.id} в чате {chat.id}, добавлен маркер [ЛУК]")
    
    add_to_history(chat_key, "user", user_message_content)
    
    # Показываем индикатор "печатает"
    await context.bot.send_chat_action(chat_id=chat.id, action="typing")
    
    messages = get_messages_for_api(chat_key)
    response_text = await call_claude_api(messages)
    
    add_to_history(chat_key, "assistant", response_text)
    
    await update.message.reply_text(response_text)


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик ошибок"""
    logger.error(f"Ошибка при обработке обновления: {context.error}")


def main() -> None:
    """Главная функция запуска бота"""
    if TELEGRAM_TOKEN == "YOUR_TELEGRAM_TOKEN":
        print("Установите TELEGRAM_TOKEN в переменных окружения")
        return
    
    if ANTHROPIC_API_KEY == "YOUR_ANTHROPIC_KEY":
        print("Установите ANTHROPIC_API_KEY в переменных окружения")
        return
    
    # Создание приложения
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Регистрация обработчиков
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_message
    ))
    
    application.add_error_handler(error_handler)
    
    # Запуск бота
    logger.info("Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
```

## Что добавлено:

1. **Комментарий после GOSLING_SYSTEM** (строки 47-48) — объясняет назначение маркера `[ЛУК]`

2. **Файл `known_users.json`** — хранит словарь `{chat_id: [user_ids]}` для отслеживания, кто уже писал

3. **Функции `load_known_users()` / `save_known_users()`** — персистентное хранение

4. **Функция `is_new_user_in_chat(chat_id, user_id)`** — проверяет и регистрирует нового пользователя

5. **В `handle_message()`** перед вызовом API:
   - Проверяется `is_new_user_in_chat()`
   - Если новый — к сообщению добавляется `[ЛУК] ` префикс
   - Логируется событие

Теперь первое сообщение от каждого нового пользователя в чате будет иметь маркер `[ЛУК]`, и Мила сможет один раз "оценить" внешность.