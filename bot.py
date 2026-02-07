import os
import asyncio
import logging
import datetime
import time
import requests
import psycopg2
import re
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

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-flash-latest')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Допоміжні функції ---
def clean_text(text):
    """Видаляє будь-які спроби форматування"""
    text = text.replace("**", "").replace("### ", "").replace("## ", "")
    clean = re.compile('<.*?>')
    text = re.sub(clean, '', text)
    return text.strip()

def connect_to_db_with_retry():
    for i in range(3):
        try:
            return psycopg2.connect(DATABASE_URL)
        except Exception as e:
            time.sleep(5)
            if i == 2: raise e

# --- 1. Логіка AI (З ПЕРЕПИСУВАННЯМ) ---
async def generate_ai_post(topic, context, platform, date_str):
    if platform == "tg":
        role_desc = "Ти автор блогу дитячого садка в Telegram."
        requirements = "Стиль корисний, спокійний. Пиши звичайним текстом без виділень."
    else: 
        role_desc = "Ти Instagram-блогер дитячого садка."
        requirements = "Стиль емоційний. Структура: Хук -> Історія -> Користь. Додай хештеги."

    # Перший запит
    prompt = (
        f"{role_desc} Напиши пост українською мовою на дату {date_str}.\n"
        f"Тема: {topic}.\nКонтекст: {context}.\n"
        f"Вимоги: {requirements}\n"
        f"ВАЖЛИВО: Не використовуй жодного форматування (ніяких ** або <b>). Просто чистий текст.\n"
        f"Орієнтуйся на обсяг до 850 символів."
    )
    
    try:
        response = model.generate_content(prompt)
        text = clean_text(response.text)
        
        # --- ЕТАП ПЕРЕВІРКИ ДОВЖИНИ ---
        # Якщо текст вийшов довшим за 950 символів (з запасом), просимо переписати
        if len(text) > 950:
            logging.info(f"Текст задовгий ({len(text)}), прошу скоротити...")
            shorten_prompt = (
                f"Твій попередній текст вийшов занадто довгим ({len(text)} символів).\n"
                f"Будь ласка, перепиши його коротше, щоб він був СУВОРО до 850 символів.\n"
                f"Збережи основну думку та стиль.\n"
                f"Ось текст: {text}"
            )
            response_short = model.generate_content(shorten_prompt)
            text = clean_text(response_short.text)
            
        return text

    except Exception as e:
        return f"ERROR_AI: {str(e)}"

# --- 2. Пошук фото ---
async def get_random_photo(keywords):
    url = f"https://api.unsplash.com/photos/random?query={keywords}&client_id={UNSPLASH_KEY}&orientation=landscape&count=1&t={int(time.time())}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list) and len(data) > 0:
                return data[0]['urls']['regular']
            elif isinstance(data, dict) and 'urls' in data:
                return data['urls']['regular']
    except Exception as e:
        logging.error(f"Unsplash Error: {e}")
    return "https://via.placeholder.com/800x600?text=No+Photo"

# --- 3. Основна функція ---
async def prepare_draft(platform, manual_date=None, from_command=False):
    # Визначаємо дату: або передана вручну, або сьогоднішня
    today_date = manual_date if manual_date else datetime.datetime.now().date()
    
    table_name = "telegram_posts" if platform == "tg" else "instagram_posts"
    platform_name = "Telegram" if platform == "tg" else "Instagram"
    
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        cursor.execute(f"SELECT topic, content, photo_keywords FROM {table_name} WHERE publish_date = %s", (today_date,))
        result = cursor.fetchone()
        
        if result:
            topic, short_context, keywords = result
            
            if from_command:
                await bot.send_message(ADMIN_ID, f"🎨 Генерую для {platform_name} (Дата {today_date})...")
            elif not manual_date:
                await bot.send_message(ADMIN_ID, f"⏰ Час посту для {platform_name} ({today_date})!")

            photo_url = await get_random_photo(keywords)
            full_post_text = await generate_ai_post(topic, short_context, platform, str(today_date))
            
            caption = f"📸 {platform_name.upper()} ({today_date})\n\n{full_post_text}"
            
            # Якщо навіть після скорочення він довгий (малоймовірно), ставимо крапки
            if len(caption) > 1020: 
                caption = caption[:1015] + "..."
            
            builder = InlineKeyboardBuilder()
            if platform == "tg":
                builder.row(types.InlineKeyboardButton(text="✅ Опублікувати", callback_data="confirm_publish"))
            
            builder.row(
                types.InlineKeyboardButton(text="🖼 Інше фото", callback_data=f"photo_{platform}_{today_date}"),
                types.InlineKeyboardButton(text="📝 Інший текст", callback_data=f"text_{platform}_{today_date}")
            )
            
            await bot.send_photo(chat_id=ADMIN_ID, photo=photo_url, caption=caption, reply_markup=builder.as_markup())
            
        else:
            await bot.send_message(ADMIN_ID, f"⚠️ У таблиці {table_name} немає плану на дату {today_date}!")
            
        cursor.close()
        conn.close()
    except Exception as e:
        await bot.send_message(ADMIN_ID, f"🆘 Помилка ({platform}): {e}")

# --- Обробка команд ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👋 KidsLand Bot (Smart Shortening + Dates)")

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
    parts = callback.data.split("_")
    platform = parts[1]
    date_str = parts[2]
    
    table_name = "telegram_posts" if platform == "tg" else "instagram_posts"

    await callback.answer("🔄 Шукаю нове фото...")
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        cursor.execute(f"SELECT photo_keywords FROM {table_name} WHERE publish_date = %s", (date_str,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()

        if result:
            new_photo_url = await get_random_photo(result[0])
            media = InputMediaPhoto(media=new_photo_url, caption=callback.message.caption)
            await callback.message.edit_media(media=media, reply_markup=callback.message.reply_markup)
    except Exception as e:
        await callback.message.answer(f"Помилка: {e}")

@dp.callback_query(F.data.startswith("text_"))
async def regen_text(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    platform = parts[1]
    date_str = parts[2]
    
    table_name = "telegram_posts" if platform == "tg" else "instagram_posts"
    platform_name = "TELEGRAM" if platform == "tg" else "INSTAGRAM"

    await callback.answer("📝 Переписую текст...")
    try:
        conn = connect_to_db_with_retry()
        cursor = conn.cursor()
        cursor.execute(f"SELECT topic, content FROM {table_name} WHERE publish_date = %s", (date_str,))
        result = cursor.fetchone()
        cursor.close()
        conn.close()

        if result:
            new_text = await generate_ai_post(result[0], result[1], platform, date_str)
            new_caption = f"📸 {platform_name} ({date_str})\n\n{new_text}"
            if len(new_caption) > 1020: new_caption = new_caption[:1015] + "..."
            
            await callback.message.edit_caption(caption=new_caption, reply_markup=callback.message.reply_markup)
    except Exception as e:
        await callback.message.answer(f"Помилка: {e}")

@dp.callback_query(F.data == "confirm_publish")
async def publish_to_channel(callback: types.CallbackQuery):
    caption = callback.message.caption
    clean_caption = caption
    if "TELEGRAM" in caption:
         parts = caption.split("\n\n", 1)
         if len(parts) > 1: clean_caption = parts[1]
    
    await bot.send_photo(chat_id=CHANNEL_ID, photo=callback.message.photo[-1].file_id, caption=clean_caption)
    await callback.message.edit_caption(caption=f"✅ <b>ОПУБЛІКОВАНО</b>\n\n{clean_caption}", parse_mode="HTML")

# --- Сервер ---
async def handle(request): return web.Response(text="KidsLand Bot Running")

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
        await bot.send_message(ADMIN_ID, "🟢 KidsLand: Розумне скорочення активовано! Тепер все працюватиме краще)")
    except:
        pass

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())