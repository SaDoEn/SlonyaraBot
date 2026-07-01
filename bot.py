import os
import logging
import asyncio
import sqlite3
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from google import genai
from google.genai import types as genai_types

# Налаштування логування
logging.basicConfig(level=logging.INFO)

# Ініціалізація токенів (беруться зі змінних середовища для безпеки)
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# База даних для збереження налаштувань мов у чатах
DB_NAME = "chats_config.db"


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_languages (
            chat_id INTEGER PRIMARY KEY,
            languages TEXT DEFAULT 'uk,ru,en'
        )
    """)
    conn.commit()
    conn.close()


def get_chat_languages(chat_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT languages FROM chat_languages WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0].split(',')
    return ['uk', 'ru', 'en']


def update_chat_languages(chat_id, langs_list):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    langs_str = ",".join(langs_list)
    cursor.execute("""
        INSERT INTO chat_languages (chat_id, languages) 
        VALUES (?, ?) 
        ON CONFLICT(chat_id) DO UPDATE SET languages = ?
    """, (chat_id, langs_str, langs_str))
    conn.commit()
    conn.close()


# Клавіатура налаштувань
def get_settings_keyboard(chat_id):
    current_langs = get_chat_languages(chat_id)
    builder = InlineKeyboardBuilder()

    uk_check = "✅" if "uk" in current_langs else "❌"
    ru_check = "✅" if "ru" in current_langs else "❌"
    en_check = "✅" if "en" in current_langs else "❌"

    builder.button(text=f"{uk_check} Українська", callback_data="toggle_uk")
    builder.button(text=f"{ru_check} Російська", callback_data="toggle_ru")
    builder.button(text=f"{en_check} Англійська", callback_data="toggle_en")
    builder.adjust(1)
    return builder.as_markup()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привіт! Я Слоняра (@SurzhykTranslatorBot2.0). 🐘\n"
        "Надішли мені голосове повідомлення, і я розшифрую його в текст, навіть якщо там суржик!\n"
        "Налаштувати дозволені мови для цього чату: /settings"
    )


@dp.message(Command("settings"))
async def cmd_settings(message: types.Message):
    await message.answer(
        "Оберіть мови, які бот має приймати та розшифровувати у цьому чаті:",
        reply_markup=get_settings_keyboard(message.chat.id)
    )


@dp.callback_query(F.data.startswith("toggle_"))
async def toggle_language(callback: types.CallbackQuery):
    lang_to_toggle = callback.data.split("_")[1]
    chat_id = callback.message.chat.id
    current_langs = get_chat_languages(chat_id)

    if lang_to_toggle in current_langs:
        # Не дозволяємо видалити останню мову
        if len(current_langs) > 1:
            current_langs.remove(lang_to_toggle)
    else:
        current_langs.append(lang_to_toggle)

    update_chat_languages(chat_id, current_langs)

    await callback.message.edit_reply_markup(reply_markup=get_settings_keyboard(chat_id))
    await callback.answer("Налаштування оновлено!")


@dp.message(F.voice)
async def handle_voice(message: types.Message):
    chat_id = message.chat.id
    allowed_langs = get_chat_languages(chat_id)

    # Словник для красивого виводу в промпт
    lang_names = {"uk": "українська", "ru": "російська", "en": "англійська"}
    target_langs = ", ".join([lang_names[l] for l in allowed_langs])

    # Надсилаємо статус, що Слоняра думає
    processing_msg = await message.reply("Слоняра слухає та розшифровує... 🎧")

    try:
        # Завантажуємо голосове повідомлення з Telegram
        voice = message.voice
        file_info = await bot.get_file(voice.file_id)
        file_path = file_info.file_path

        local_filename = f"{voice.file_id}.ogg"
        await bot.download_file(file_path, local_filename)

        # Читаємо аудіо у бінарному форматі
        with open(local_filename, "rb") as f:
            audio_data = f.read()

        # Видаляємо локальний файл після читання
        os.remove(local_filename)

        # Формуємо чітку інструкцію для Gemini (промпт)
        prompt = (
            f"Ти — аудіо-транскрибатор. Твоє завдання — перевести аудіо в текст слово в слово. "
            f"Дозволені мови в цьому чаті: {target_langs}. "
            f"ВАЖЛИВО: Якщо в аудіо використовується суржик (суміш українських та російських слів), "
            f"запиши його ТОЧНО так, як людина його вимовляє, не виправляй суржик на чисту мову! "
            f"Якщо мова аудіо не входить до списку дозволених ({target_langs}), просто напиши: '[Повідомлення мовою, яка вимкнена в налаштуваннях бота]'."
        )

        # Передаємо файл безпосередньо в Gemini API за допомогою SDK
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                genai_types.Part.from_bytes(
                    data=audio_data,
                    mime_type='audio/ogg',
                ),
                prompt
            ]
        )

        text_result = response.text if response.text else "[Не вдалося розпізнати мову або звук занадто тихий]"

        # Відповідаємо користувачу
        await processing_msg.edit_text(f"**Розшифровка:**\n\n{text_result}", parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Помилка обробки: {e}")
        await processing_msg.edit_text("Ой, щось пішло не так при розпізнаванні аудіо... Спробуй ще раз.")


async def main():
    init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())