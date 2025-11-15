# wifi_handlers.py (fix)
import asyncio
import logging
import html
from aiogram import types
from loader import dp, wifi_db, user_db, bot
from data.config import ADMINS

logger = logging.getLogger(__name__)

async def is_admin(telegram_id: int) -> bool:
    if telegram_id in ADMINS:
        return True
    try:
        user = await asyncio.to_thread(user_db.select_user, telegram_id=telegram_id)
        if not user:
            return False
        user_id = user[0]
        return await asyncio.to_thread(user_db.check_if_admin, user_id=user_id)
    except Exception as e:
        logger.exception("Error checking admin status: %s", e)
        return False

# /setwifi <new_password>
@dp.message_handler(commands=["setwifi"])
async def cmd_setwifi(message: types.Message):
    telegram_id = message.from_user.id
    if not await is_admin(telegram_id):
        await message.reply("❌ Sizda bunday ruxsat yoʻq.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        # Escaping angle brackets so Telegram HTML parse_mode won't crash
        usage = "Foydalanish: /setwifi &lt;yangi_parol&gt;\nMasalan: /setwifi mynewpass123"
        # send with parse_mode="HTML" (bot global parse_mode ham HTML bo'lsa shu ham ishlaydi)
        await message.reply(usage, parse_mode="HTML")
        return

    new_pass = parts[1].strip()
    try:
        # saqlash (sync DB => to_thread)
        await asyncio.to_thread(wifi_db.set_password, new_pass, f"set by {telegram_id}")
        # yuborishda parolni <code> ichida jo'natamiz va HTML-escape qilamiz
        await message.reply(f"✅ Wi-Fi parol muvaffaqiyatli yangilandi.\nParol: <code>{html.escape(new_pass)}</code>", parse_mode="HTML")
        logger.info("Wi-Fi password set by %s", telegram_id)
    except Exception as e:
        logger.exception("Error setting wifi password: %s", e)
        await message.reply("❌ Parolni saqlashda xatolik yuz berdi.")

# /delwifi
@dp.message_handler(commands=["delwifi"])
async def cmd_delwifi(message: types.Message):
    telegram_id = message.from_user.id
    if not await is_admin(telegram_id):
        await message.reply("❌ Sizda bunday ruxsat yoʻq.")
        return

    try:
        await asyncio.to_thread(wifi_db.remove_password)
        await message.reply("✅ Wi-Fi parol oʻchirildi.")
        logger.info("Wi-Fi password removed by %s", telegram_id)
    except Exception as e:
        logger.exception("Error removing wifi password: %s", e)
        await message.reply("❌ Parolni oʻchirishda xatolik yuz berdi.")

# /getwifi
@dp.message_handler(commands=["getwifi"])
async def cmd_getwifi(message: types.Message):
    try:
        pw = await asyncio.to_thread(wifi_db.get_password)
        if pw:
            # always escape before inserting into HTML
            await message.answer(f"🔐 Wi-Fi parol: <code>{html.escape(pw)}</code>", parse_mode="HTML")
        else:
            await message.answer("❌ Hozirda Wi-Fi parol saqlanmagan. Iltimos admin bilan bog'laning.")
    except Exception as e:
        logger.exception("Error fetching wifi password: %s", e)
        await message.answer("❌ Parolni olishda xatolik yuz berdi. Iltimos admin bilan bog'laning.")
