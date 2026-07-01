import os
import logging
import asyncio
import sqlite3
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from google import genai
from google.genai import types as genai_types
from aiohttp import web

# Логування
logging.basicConfig(level=logging.INFO)

# Токени
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Зчитуємо рядок із ключами, які вказані через кому в налаштуваннях Render
raw_keys = os.getenv("GEMINI_API_KEYS", "")
# Розбиваємо рядок на окремі ключі та очищаємо від зайвих пробілів
GEMINI_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]

# Якщо раптом забув додати нову змінну, спробуємо стару поодиноку змінну для зворотної сумісності
if not GEMINI_KEYS and os.getenv("GEMINI_API_KEY"):
    GEMINI_KEYS = [os.getenv("GEMINI_API_KEY")]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Функція для створення клієнта на льоту з потрібним ключем
def get_gemini_client(index):
    key = GEMINI_KEYS[index % len(GEMINI_KEYS)]
    return genai.Client(api_key=key)

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
    lang_names = {"uk": "українська", "ru": "російська", "en": "англійська"}
    target_langs = ", ".join([lang_names[l] for l in allowed_langs])

    processing_msg = await message.reply("Слоняра слухає та розшифровує... 🎧")

    try:
        voice = message.voice
        file_info = await bot.get_file(voice.file_id)
        local_filename = f"{voice.file_id}.ogg"
        await bot.download_file(file_info.file_path, local_filename)

        with open(local_filename, "rb") as f:
            audio_data = f.read()
        os.remove(local_filename)

        prompt = (
            f"Ти — професійний аудіо-транскрибатор. Твоє завдання — перекласти це аудіо в текст слово в слово.\n"
            f"Дозволені мови в цьому чаті: {target_langs}.\n"
            f"ПРАВИЛА ОБРОБКИ:\n"
            f"1. Якщо в аудіо є фоновий шум — ігноруй його.\n"
            f"2. Якщо повідомлення дуже коротке, обов'язково запиши його текстом.\n"
            f"3. Якщо використовується суржик, запиши його ТОЧНО так, як людина його вимовляє.\n"
            f"4. Твоя відповідь повинна містити ВИКЛЮЧНО розпізнаний текст."
        )

        response = None
        # Пробуємо кожен ключ по черзі, якщо вилітає ліміт 429
        for i in range(len(GEMINI_KEYS)):
            try:
                client = get_gemini_client(i)
                response = client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[
                        genai_types.Part.from_bytes(data=audio_data, mime_type='audio/ogg'),
                        prompt
                    ],
                    config=genai_types.GenerateContentConfig(temperature=0.0)
                )
                # Якщо запит пройшов успішно — перериваємо цикл перебору ключів
                break
            except Exception as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    logging.warning(f"Ключ №{i} вичерпав ліміт, пробуємо наступний...")
                    continue
                else:
                    raise e

        if response is None:
            await processing_msg.edit_text("⚠️ Усі безкоштовні ключі Слоняри на сьогодні вичерпали ліміт запитів. Спробуйте пізніше.")
            return

        text_result = response.text.strip() if response.text else ""
        if not text_result or len(text_result) < 1:
            text_result = "[Не вдалося чітко розпізнати слова]"

        await processing_msg.edit_text(f"**Розшифровка:**\n\n{text_result}", parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Помилка розпізнавання: {e}")
        await processing_msg.edit_text("Ой, щось пішло не так при обробці... Спробуйте ще раз.")
        
# Сервер-заглушка для Render (приймає порт автоматично)
async def handle_render_health(request):
    return web.Response(text="Slonyara is alive and kicking!")

async def main():
    init_db()
    
    # Запускаємо довго опитування телеграм бота у фоні
    asyncio.create_task(dp.start_polling(bot))
    
    # Піднімаємо веб-сервер на порт, який виділить Render (за замовчуванням 10000)
    port = int(os.getenv("PORT", 10000))
    app = web.Application()
    app.router.add_get('/', handle_render_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    # Не даємо скрипту закритися
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
