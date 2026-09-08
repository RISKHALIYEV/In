import asyncio
import logging
import os
import re
import shutil
import sys
import traceback
import uuid

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import yt_dlp

# ==========================================================
#  SOZLAMALAR
# ==========================================================
# Token avval muhit o'zgaruvchisidan (Railway -> Variables) olinadi;
# agar topilmasa, quyidagi standart qiymatga tushadi.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8450352692:AAEeM2qVAP4nMvKEDfPTi8bNmCROFqpKNbk")

DOWNLOADS_DIR = "downloads"
MAX_FILE_SIZE = 50 * 1024 * 1024  # Telegram bot API orqali 50MB dan katta fayl yuborib bo'lmaydi

# MUHIM: ba'zi hosting muhitlari (terminal bo'lmagan joyda ishlaydigan)
# Python stdout'ni buferlab qo'yadi — shuning uchun print() chiqishi
# darhol ko'rinmasligi mumkin. Bufferni majburan o'chiramiz, aks holda
# dastur biror joyda "osilib" qolsa, konsolda umuman hech narsa
# ko'rinmaydi.
try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except AttributeError:
    pass  # Eski Python versiyalarida reconfigure yo'q — e'tiborsiz qoldiramiz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)


def log(msg: str) -> None:
    """Konsolda darhol ko'rinishi kafolatlangan chiqish."""
    print(msg, flush=True)

router = Router()

# Har bir foydalanuvchining oxirgi yuborgan Instagram havolasini vaqtincha saqlab turamiz
user_links: dict[int, str] = {}

INSTAGRAM_URL_PATTERN = re.compile(
    r"(https?://)?(www\.)?instagram\.com/[A-Za-z0-9_\-./?=&%]+",
    re.IGNORECASE,
)


# ==========================================================
#  YORDAMCHI FUNKSIYALAR
# ==========================================================
def build_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎥 Video", callback_data="mode_video"),
                InlineKeyboardButton(text="🎵 Audio (MP3)", callback_data="mode_audio"),
            ]
        ]
    )


def _extract_ydl_opts(mode: str, out_template: str) -> dict:
    base_opts = {
        "outtmpl": out_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "retries": 3,
        "socket_timeout": 30,
    }

    if mode == "audio":
        base_opts.update(
            {
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        )
    else:
        base_opts.update(
            {
                "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "merge_output_format": "mp4",
            }
        )

    return base_opts


def _download_sync(url: str, mode: str, work_dir: str) -> str:
    """
    Bloklovchi (sinxron) yuklab olish funksiyasi.
    Asosiy event loop qotib qolmasligi uchun bu alohida threadda chaqiriladi.
    Yakuniy fayl yo'lini qaytaradi.
    """
    out_template = os.path.join(work_dir, "%(id)s.%(ext)s")
    ydl_opts = _extract_ydl_opts(mode, out_template)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

        if mode == "audio":
            base, _ = os.path.splitext(ydl.prepare_filename(info))
            final_path = base + ".mp3"
        else:
            final_path = ydl.prepare_filename(info)
            if not os.path.exists(final_path):
                base, _ = os.path.splitext(final_path)
                candidate_mp4 = base + ".mp4"
                if os.path.exists(candidate_mp4):
                    final_path = candidate_mp4

    if not os.path.exists(final_path):
        raise FileNotFoundError("Yuklab olingan fayl topilmadi.")

    return final_path


async def download_content(url: str, mode: str, work_dir: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download_sync, url, mode, work_dir)


# ==========================================================
#  HANDLERLAR
# ==========================================================
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 Salom! Men Instagram'dan video va musiqa yuklab beruvchi botman.\n\n"
        "📌 Menga Instagram Reels, Post yoki Video havolasini yuboring — "
        "men sizga o'sha videoni yoki undagi musiqani (MP3) yuklab beraman.\n\n"
        "🔗 Boshlash uchun havolani yuboring!"
    )


@router.message(F.text)
async def handle_link(message: Message) -> None:
    text = (message.text or "").strip()
    match = INSTAGRAM_URL_PATTERN.search(text)

    if not match:
        await message.answer(
            "⚠️ Bu Instagram havolasiga o'xshamayapti.\n"
            "Iltimos, to'g'ri Instagram Reels/Post/Video havolasini yuboring.\n\n"
            "Masalan: https://www.instagram.com/reel/XXXXXXXXXXX/"
        )
        return

    url = match.group(0)
    if not url.startswith("http"):
        url = "https://" + url

    user_links[message.from_user.id] = url

    await message.answer(
        "✅ Havola qabul qilindi!\n\nQaysi formatda olishni xohlaysiz?",
        reply_markup=build_mode_keyboard(),
    )


@router.callback_query(F.data.in_({"mode_video", "mode_audio"}))
async def handle_mode_choice(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    url = user_links.get(user_id)

    if not url:
        await callback.answer(
            "⚠️ Avval Instagram havolasini yuboring.", show_alert=True
        )
        return

    mode = "audio" if callback.data == "mode_audio" else "video"
    await callback.answer()

    status_message = await callback.message.edit_text(
        "⏳ Yuklab olinmoqda... Iltimos, biroz kuting."
    )

    work_dir = os.path.join(DOWNLOADS_DIR, str(uuid.uuid4()))
    os.makedirs(work_dir, exist_ok=True)

    try:
        file_path = await download_content(url, mode, work_dir)
        file_size = os.path.getsize(file_path)

        if file_size > MAX_FILE_SIZE:
            await status_message.edit_text(
                "❌ Kechirasiz, fayl hajmi 50MB dan katta.\n"
                "Telegram bot API orqali bunday katta faylni yuborib bo'lmaydi."
            )
            return

        input_file = FSInputFile(file_path)

        if mode == "audio":
            await callback.message.answer_audio(
                input_file, caption="🎵 Mana sizning audio faylingiz!"
            )
        else:
            try:
                await callback.message.answer_video(
                    input_file, caption="🎥 Mana sizning videongiz!"
                )
            except TelegramBadRequest:
                # Ba'zi formatlar "video" sifatida qabul qilinmasa, hujjat sifatida yuboramiz
                await callback.message.answer_document(
                    input_file, caption="🎥 Mana sizning videongiz!"
                )

        await status_message.delete()

    except TelegramEntityTooLarge:
        await status_message.edit_text(
            "❌ Fayl hajmi juda katta, Telegram orqali yuborib bo'lmaydi."
        )
    except yt_dlp.utils.DownloadError:
        await status_message.edit_text(
            "❌ Yuklab bo'lmadi. Havola noto'g'ri, kontent o'chirilgan yoki "
            "akkaunt yopiq (private) bo'lishi mumkin."
        )
    except FileNotFoundError:
        await status_message.edit_text(
            "❌ Fayl topilmadi. Iltimos, boshqa havola bilan qayta urinib ko'ring."
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Kutilmagan xatolik: %s", exc)
        await status_message.edit_text(
            "❌ Kutilmagan xatolik yuz berdi. Iltimos, birozdan so'ng qayta urinib ko'ring."
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        user_links.pop(user_id, None)


# ==========================================================
#  ISHGA TUSHIRISH
# ==========================================================
async def main() -> None:
    log("🔧 Skript boshlandi, papkalar tayyorlanmoqda...")
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)

    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        log("❌ BOT_TOKEN bo'sh yoki noto'g'ri formatda. Kod ichidagi BOT_TOKEN qatorini tekshiring.")
        return

    log("🔧 Bot obyekti yaratilmoqda...")
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    # Token haqiqatan ham ishlayotganini darhol tekshiramiz — shunda
    # muammo bo'lsa aniq sababi konsolda ko'rinadi.
    log("🔧 Telegram serveriga ulanish tekshirilmoqda (get_me)...")
    try:
        me = await bot.get_me()
        log(f"✅ Bot muvaffaqiyatli ulandi: @{me.username} (id={me.id})")
    except TelegramUnauthorizedError:
        log(
            "❌ TOKEN NOTO'G'RI YOKI BEKOR QILINGAN.\n"
            "   @BotFather orqali /mybots -> Bot -> API Token bo'limidan "
            "joriy tokenni tekshiring yoki yangisini oling, so'ng uni "
            "BOT_TOKEN qatoriga qo'ying."
        )
        return
    except TelegramNetworkError as exc:
        log(
            f"❌ TARMOQ XATOSI: Telegram serveriga (api.telegram.org) ulanib bo'lmadi.\n"
            f"   Sabab: {exc}\n"
            "   Hosting muhiti tashqi internetga (ayniqsa Telegram serverlariga) "
            "chiqishni cheklagan bo'lishi mumkin. Hosting provayderdan "
            "'outbound network access' yoqilganini so'rang."
        )
        return
    except Exception as exc:  # noqa: BLE001
        log(f"❌ Bot bilan bog'lanib bo'lmadi: {exc}")
        traceback.print_exc()
        return

    log("🚀 Bot ishga tushdi, xabarlarni kutmoqda...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except Exception as exc:  # noqa: BLE001
        log(f"❌ Polling paytida xatolik: {exc}")
        traceback.print_exc()


if __name__ == "__main__":
    log("▶️ bot.py fayli ishga tushirildi (Python interpretator skriptni yukladi).")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("🛑 Bot to'xtatildi.")
    except Exception as exc:  # noqa: BLE001
        log(f"❌ Kutilmagan boshlang'ich xatolik: {exc}")
        traceback.print_exc()
