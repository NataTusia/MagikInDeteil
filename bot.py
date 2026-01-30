import os
import asyncio
import logging
import datetime
import time
import requests
import psycopg2
import google.generativeai as genai
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InputMediaPhoto
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiohttp import web

# --- Налаштування ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
UNSPLASH_KEY = os.environ.get("UNSPLASH_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Налаштування AI
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-flash-latest')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- ЛОКАЛІЗАЦІЯ І ПРОМПТ ---
# Щоб змінити мову генерації, зміни це слово на "ukrainian"
TARGET_LANGUAGE = "russian" 

# --- Допоміжні функції ---
def clean_text(text):
    return text.replace("**", "").replace("### ", "").replace("## ", "")

def connect_to_db_with_retry():
    for i in range(3):
        try:
            return psycopg2.connect(DATABASE_URL)
        except Exception as e:
            time.sleep(5)
            if i == 2: raise e

# --- 1. Логіка AI (Езотерика / Таро) ---
async def generate_ai_post(topic, context, platform):
    if platform == "tg":
        role_desc = "Ты опытный таролог и энергопрактик, автор Telegram-канала."
        # Вимоги для Телеграму
        requirements = (
            "Пиши глубоко, но без 'воды'. Стиль: мистический, но прикладной и современный. "
            "Давай конкретные практики или советы. Используй <b>жирный шрифт</b> для акцентов. "
            "В конце задай вопрос аудитории для обсуждения."
        )
    else: # inst
        role_desc = "Ты Instagram-блогер в нише эзотерики и самопознания."
        # Вимоги для Інстаграму
        requirements = (
            "Стиль: атмосферный, цепляющий, эмоциональный. Структура: Заголовок-хук -> Суть -> Призыв к действию (сохрани/напиши). "
            "Обязательно добавь 10-15 тематических хэштегов (таро, эзотерика, энергия, и т.д.) в конце."
        )

    prompt = (
        f"{role_desc} Напиши пост на языке: {TARGET_LANGUAGE}.\n"
        f"Тема: {topic}.\nКонтекст: {context}.\n"
        f"Требования: {requirements}\n"
        f"Объем: До 950 символов. Добавь соответствующие эзотерические эмодзи (✨, 🔮, 🌙)."
    )
    
    try:
        response = model.generate_content(prompt)
        return clean_text(response.text)
    except Exception as e:
        return f"ERROR_AI: {str(e)}"

# --- 2. Пошук фото ---
async def get_random_photo(keywords):
    url = f"https://api.unsplash.com/photos/random?query={keywords}&client_id={UNSPLASH_KEY}&orientation=landscape&count=1&t={int(time.time())}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data[0]['urls']['regular'] if isinstance(data, list) else data['urls']['regular']
    except Exception as e:
        logging.error(f"Unsplash Error: {e}")
    return "https://via.placeholder.com/800x600?text=No+Photo"

# --- 3. Основна функція генерації ---
async def prepare_draft(platform, manual_day=None, from_command=False):
    day_now = manual_day if manual_day else datetime.datetime.now().day
    table_name = "telegram_posts" if platform == "tg" else "instagram_posts"
    platform_name = "Telegram" if platform == "tg" else "Instagram"
    
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        
        cursor.execute(f"SELECT topic, content, photo_keywords FROM {table_name} WHERE day_number = %s", (day_now,))
        result = cursor.fetchone()
        
        if result:
            topic, short_context, keywords = result
            
            if from_command:
                await bot.send_message(ADMIN_ID, f"🔮 Генерирую для {platform_name} (День {day_now})...")
            elif not manual_day:
                await bot.send_message(ADMIN_ID, f"⏰ Время поста для {platform_name}!")

            photo_url = await get_random_photo(keywords)
            full_post_text = await generate_ai_post(topic, short_context, platform)
            
            # Заголовок повідомлення (UI російською)
            caption = f"<b>📸 {platform_name.upper()} (День {day_now})</b>\n\n{full_post_text}"
            if len(caption) > 1020: caption = caption[:1015] + "..."
            
            builder = InlineKeyboardBuilder()
            
            if platform == "tg":
                builder.row(types.InlineKeyboardButton(text="✅ Опубликовать", callback_data="confirm_publish"))
            
            builder.row(
                types.InlineKeyboardButton(text="🖼 Другое фото", callback_data=f"photo_{platform}_{day_now}"),
                types.InlineKeyboardButton(text="📝 Другой текст", callback_data=f"text_{platform}_{day_now}")
            )
            
            await bot.send_photo(chat_id=ADMIN_ID, photo=photo_url, caption=caption, reply_markup=builder.as_markup(), parse_mode="HTML")
        else:
            await bot.send_message(ADMIN_ID, f"⚠️ В таблице {table_name} нет темы на день {day_now}!")
            
        cursor.close()
        conn.close()
    except Exception as e:
        await bot.send_message(ADMIN_ID, f"🆘 Ошибка ({platform}): {e}")

# --- Обробка команд ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "👋 <b>Магическая Панель</b>\n\n"
            "/generate_tg — Создать пост для Telegram\n"
            "/generate_inst — Создать пост для Instagram\n"
            "/start — Обновить меню",
            parse_mode="HTML"
        )

@dp.message(Command("generate_tg"))
async def cmd_gen_tg(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft(platform="tg", from_command=True)

@dp.message(Command("generate_inst"))
async def cmd_gen_inst(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await prepare_draft(platform="inst", from_command=True)

# --- Callbacks ---
@dp.callback_query(F.data.startswith("photo_"))
async def regen_photo(callback: types.CallbackQuery):
    _, platform, day = callback.data.split("_")
    day = int(day)
    table_name = "telegram_posts" if platform == "tg" else "instagram_posts"

    await callback.answer("🔄 Ищу новое фото...")
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        cursor.execute(f"SELECT photo_keywords FROM {table_name} WHERE day_number = %s", (day,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()

        if result:
            new_photo_url = await get_random_photo(result[0])
            media = InputMediaPhoto(media=new_photo_url, caption=callback.message.caption, caption_entities=callback.message.caption_entities)
            await callback.message.edit_media(media=media, reply_markup=callback.message.reply_markup)
    except Exception as e:
        await callback.message.answer(f"Ошибка: {e}")

@dp.callback_query(F.data.startswith("text_"))
async def regen_text(callback: types.CallbackQuery):
    _, platform, day = callback.data.split("_")
    day = int(day)
    table_name = "telegram_posts" if platform == "tg" else "instagram_posts"
    platform_name = "TELEGRAM" if platform == "tg" else "INSTAGRAM"

    await callback.answer("📝 Переписываю текст...")
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        cursor.execute(f"SELECT topic, content FROM {table_name} WHERE day_number = %s", (day,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()

        if result:
            new_text = await generate_ai_post(result[0], result[1], platform)
            new_caption = f"<b>📸 {platform_name} (День {day})</b>\n\n{new_text}"
            if len(new_caption) > 1020: new_caption = new_caption[:1015] + "..."
            
            await callback.message.edit_caption(caption=new_caption, parse_mode="HTML", reply_markup=callback.message.reply_markup)
    except Exception as e:
        await callback.message.answer(f"Ошибка: {e}")

@dp.callback_query(F.data == "confirm_publish")
async def publish_to_channel(callback: types.CallbackQuery):
    caption = callback.message.html_text if callback.message.html_text else callback.message.caption
    clean_caption = caption
    if "TELEGRAM" in caption:
         parts = caption.split("\n\n", 1)
         if len(parts) > 1: clean_caption = parts[1]
    
    await bot.send_photo(
        chat_id=CHANNEL_ID, 
        photo=callback.message.photo[-1].file_id, 
        caption=clean_caption, 
        caption_entities=callback.message.caption_entities
    )
    await callback.message.edit_caption(caption=f"✅ <b>ОПУБЛИКОВАНО В КАНАЛ</b>\n\n{clean_caption}", parse_mode="HTML")

# --- Сервер ---
async def handle(request): return web.Response(text="Tarot Bot Running")

async def main():
    logging.basicConfig(level=logging.INFO)
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()
    
    scheduler = AsyncIOScheduler(timezone="Europe/Kyiv")
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=0, args=['tg'], misfire_grace_time=3600)
    scheduler.add_job(prepare_draft, 'cron', hour=9, minute=10, args=['inst'], misfire_grace_time=3600)
    scheduler.start()
    
    try:
        await bot.send_message(ADMIN_ID, "🟢 Эзотерический бот запущен!")
    except:
        pass

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())