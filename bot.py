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

# Мова генерації
TARGET_LANGUAGE = "russian" 

# Підпис для помилок
ERROR_SIGNATURE = "\n\n📩 <b>Перешлите это сообщение программисту Нате, она знает что с этим делать и поможет вам исправить ошибку.</b>"

# --- Допоміжні функції ---
def clean_text(text):
    text = text.replace("**", "").replace("### ", "").replace("## ", "")
    clean = re.compile('<.*?>')
    return re.sub(clean, '', text).strip()

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
        role_desc = "Ты опытный таролог и энергопрактик."
        requirements = "Стиль: мистический, но без 'воды'. Пиши обычным текстом без форматирования."
    else: 
        role_desc = "Ты популярный эзотерик-блогер."
        requirements = "Стиль: цепляющий. Добавь хэштеги. Пиши обычным текстом без форматирования."

    # Перший запит
    prompt = (
        f"{role_desc} Напиши пост на языке: {TARGET_LANGUAGE} на дату {date_str}.\n"
        f"Тема: {topic}.\nКонтекст: {context}.\n"
        f"Требования: {requirements}\n"
        f"ВАЖНО: Максимальная длина — 850 символов. Не используй жирный шрифт."
    )
    
    try:
        response = model.generate_content(prompt)
        text = clean_text(response.text)

        # --- ЕТАП ПЕРЕВІРКИ ДОВЖИНИ ---
        # Якщо текст вийшов довшим за 950 символів, просимо переписати
        if len(text) > 950:
            logging.info(f"Текст задовгий ({len(text)}), прошу скоротити...")
            shorten_prompt = (
                f"Твой предыдущий текст получился слишком длинным ({len(text)} символов).\n"
                f"Пожалуйста, перепиши его короче, чтобы он был СТРОГО до 850 символов.\n"
                f"Сохрани главную мысль и мистический стиль.\n"
                f"Вот текст: {text}"
            )
            response_short = model.generate_content(shorten_prompt)
            text = clean_text(response_short.text)

        return text
    except Exception as e:
        return f"ERROR_AI: {str(e)}"

# --- 2. Пошук фото (Оновлений) ---
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
        
        elif response.status_code == 404:
            # Запасний пошук
            backup_url = f"https://api.unsplash.com/photos/random?query=tarot+magic+candles&client_id={UNSPLASH_KEY}&orientation=landscape&count=1&t={int(time.time())}"
            backup_response = requests.get(backup_url, timeout=10)
            if backup_response.status_code == 200:
                data = backup_response.json()
                if isinstance(data, list) and len(data) > 0:
                    return data[0]['urls']['regular']
                elif isinstance(data, dict) and 'urls' in data:
                    return data['urls']['regular']

    except Exception as e:
        logging.error(f"Unsplash Error: {e}")
    
    # Запасне фото (Таро)
    return "https://images.unsplash.com/photo-1603522370258-067c2162b775?q=80&w=1000&auto=format&fit=crop"

# --- 3. Основна функція (ПО ДАТІ) ---
async def prepare_draft(platform, manual_date=None, from_command=False):
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
                await bot.send_message(ADMIN_ID, f"🔮 Генерирую для {platform_name} (Дата {today_date})...")
            elif not manual_date:
                await bot.send_message(ADMIN_ID, f"⏰ Время поста для {platform_name} ({today_date})!")

            photo_url = await get_random_photo(keywords)
            full_post_text = await generate_ai_post(topic, short_context, platform, str(today_date))
            
            caption = f"📸 {platform_name.upper()} ({today_date})\n\n{full_post_text}"
            
            if len(caption) > 1020: caption = caption[:1015] + "..."
            
            builder = InlineKeyboardBuilder()
            if platform == "tg":
                builder.row(types.InlineKeyboardButton(text="✅ Опубликовать", callback_data="confirm_publish"))
            
            builder.row(
                types.InlineKeyboardButton(text="🖼 Другое фото", callback_data=f"photo_{platform}_{today_date}"),
                types.InlineKeyboardButton(text="📝 Другой текст", callback_data=f"text_{platform}_{today_date}")
            )
            
            await bot.send_photo(chat_id=ADMIN_ID, photo=photo_url, caption=caption, reply_markup=builder.as_markup())
        else:
            await bot.send_message(ADMIN_ID, f"⚠️ В таблице {table_name} нет темы на дату {today_date}!")
            
        cursor.close()
        conn.close()
    except Exception as e:
        await bot.send_message(ADMIN_ID, f"🆘 Ошибка ({platform}): {e}{ERROR_SIGNATURE}", parse_mode="HTML")

# --- Обробка команд ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👋 Magic Bot (Dates + Smart AI)\n/generate_tg\n/generate_inst")

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

    try:
        await callback.answer("🔄 Ищу новое фото...")
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
        if "message is not modified" in str(e):
            await callback.answer("⚠️ Фото не изменилось", show_alert=True)
        else:
            await callback.message.answer(f"Ошибка: {e}")

@dp.callback_query(F.data.startswith("text_"))
async def regen_text(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    platform = parts[1]
    date_str = parts[2]
    table_name = "telegram_posts" if platform == "tg" else "instagram_posts"
    platform_name = "TELEGRAM" if platform == "tg" else "INSTAGRAM"

    try:
        await callback.answer("📝 Переписую текст...")
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
        if "message is not modified" in str(e):
            await callback.answer("⚠️ Текст получился таким же", show_alert=True)
        else:
            await callback.message.answer(f"Ошибка: {e}")

@dp.callback_query(F.data == "confirm_publish")
async def publish_to_channel(callback: types.CallbackQuery):
    caption = callback.message.caption
    clean_caption = caption
    if "TELEGRAM" in caption:
         parts = caption.split("\n\n", 1)
         if len(parts) > 1: clean_caption = parts[1]
    
    await bot.send_photo(chat_id=CHANNEL_ID, photo=callback.message.photo[-1].file_id, caption=clean_caption)
    await callback.message.edit_caption(caption=f"✅ <b>ОПУБЛИКОВАНО</b>\n\n{clean_caption}", parse_mode="HTML")

# --- Сервер ---
async def handle(request): return web.Response(text="Magic Bot Running")

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
        await bot.send_message(ADMIN_ID, "✨ Портал связи открыт! Обновление успешно, теперь связь с космосом на высшем уровне) 🔮", parse_mode="HTML")
    except:
        pass

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())