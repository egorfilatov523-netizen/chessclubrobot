"""
CHESS CLUB — Этап 2: Telegram-бот + API с проверкой initData.

Установка (в терминале PyCharm):
    pip install aiogram fastapi uvicorn python-dotenv

Переменные окружения (Run → Edit Configurations → Environment variables
или файл .env рядом со скриптом):
    BOT_TOKEN=токен_от_BotFather
    WEBAPP_URL=https://ваш-https-адрес-фронтенда
    BOT_USERNAME=имя_бота_без_@
    PORT=8000

Запуск: просто Run на этом файле.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from urllib.parse import parse_qsl

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "8874890143:AAG5J7aBk8sJbjmj2QczHxgnXqoPw_TdKOU")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "@chessclubrobot")
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = os.getenv("DB_PATH", "chess_club.db")
INIT_DATA_MAX_AGE = 24 * 3600  # секунд

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("chess-club")


# ───────────────────────── База данных (SQLite) ─────────────────────────
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER NOT NULL UNIQUE,
                game_id TEXT NOT NULL UNIQUE,
                nickname TEXT NOT NULL,
                rating INTEGER NOT NULL DEFAULT 1200,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invites (
                token TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL REFERENCES users(id),
                created_at INTEGER NOT NULL,
                used_by INTEGER REFERENCES users(id)
            );
            """
        )


def get_or_create_user(tg_id: int, name: str) -> sqlite3.Row:
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
        if row:
            return row
        # Игровой ID — случайный, а не Telegram ID.
        game_id = secrets.token_hex(4).upper()
        c.execute(
            "INSERT INTO users (tg_id, game_id, nickname, created_at) VALUES (?,?,?,?)",
            (tg_id, game_id, name[:32] or "Player", int(time.time())),
        )
        return c.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()


def create_invite(user_id: int) -> str:
    token = secrets.token_urlsafe(16)  # непредсказуемый токен
    with db() as c:
        c.execute(
            "INSERT INTO invites (token, owner_id, created_at) VALUES (?,?,?)",
            (token, user_id, int(time.time())),
        )
    return token


# ───────────────────────── Проверка Telegram initData ─────────────────────────
def verify_init_data(init_data: str, bot_token: str) -> dict:
    """Проверяет подпись initData по алгоритму Telegram и возвращает данные пользователя."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise ValueError("нет hash")

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        raise ValueError("неверная подпись")

    if time.time() - int(pairs.get("auth_date", "0")) > INIT_DATA_MAX_AGE:
        raise ValueError("initData устарел")

    return json.loads(pairs["user"])


# ───────────────────────── FastAPI ─────────────────────────
app = FastAPI(title="CHESS CLUB API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[WEBAPP_URL] if WEBAPP_URL else ["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


def current_user(authorization: str | None) -> sqlite3.Row:
    """Ожидает заголовок: Authorization: tma <initData>."""
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "Нет данных авторизации")
    try:
        tg_user = verify_init_data(authorization[4:], BOT_TOKEN)
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        raise HTTPException(401, f"Авторизация не пройдена: {e}")
    name = tg_user.get("username") or tg_user.get("first_name") or "Player"
    return get_or_create_user(int(tg_user["id"]), name)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/me")
def me(authorization: str | None = Header(default=None)):
    u = current_user(authorization)
    return {
        "game_id": u["game_id"],
        "nickname": u["nickname"],
        "rating": u["rating"],
        "created_at": u["created_at"],
    }


# ───────────────────────── Telegram-бот (aiogram 3) ─────────────────────────
dp = Dispatcher()


def open_app_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="♟ Открыть CHESS CLUB", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )


@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    user = get_or_create_user(message.from_user.id, message.from_user.username or message.from_user.full_name)

    # Переход по ссылке-приглашению: t.me/<bot>?start=inv_<token>
    if command.args and command.args.startswith("inv_"):
        token = command.args[4:]
        with db() as c:
            inv = c.execute("SELECT * FROM invites WHERE token = ?", (token,)).fetchone()
        if not inv or inv["used_by"] is not None:
            await message.answer("Приглашение недействительно или уже использовано.")
        elif inv["owner_id"] == user["id"]:
            await message.answer("Это ваше собственное приглашение.")
        else:
            await message.answer("Вас пригласили сыграть партию! Открывайте приложение.", reply_markup=open_app_kb())
            # Онлайн-партии появятся на Этапе 4; здесь токен пока не «сжигается».
        return

    await message.answer(
        f"Добро пожаловать в CHESS CLUB, {user['nickname']}!\nВаш игровой ID: {user['game_id']}",
        reply_markup=open_app_kb(),
    )


@dp.message(Command("app"))
async def cmd_app(message: Message):
    await message.answer("Открыть игру:", reply_markup=open_app_kb())


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    u = get_or_create_user(message.from_user.id, message.from_user.username or message.from_user.full_name)
    await message.answer(f"Никнейм: {u['nickname']}\nИгровой ID: {u['game_id']}\nРейтинг Elo: {u['rating']}")


@dp.message(Command("invite"))
async def cmd_invite(message: Message):
    u = get_or_create_user(message.from_user.id, message.from_user.username or message.from_user.full_name)
    token = create_invite(u["id"])
    await message.answer(f"Ссылка-приглашение в партию:\nhttps://t.me/{BOT_USERNAME}?start=inv_{token}")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "/start — главное меню\n/app — открыть игру\n/profile — профиль и рейтинг\n/invite — пригласить друга в партию"
    )


# ───────────────────────── Запуск ─────────────────────────
async def main():
    if not BOT_TOKEN or not WEBAPP_URL:
        raise SystemExit("Задайте BOT_TOKEN и WEBAPP_URL в переменных окружения или в .env")
    init_db()

    bot = Bot(BOT_TOKEN)
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info"))
    # API и бот работают в одном процессе.
    await asyncio.gather(server.serve(), dp.start_polling(bot))


if __name__ == "__main__":
    asyncio.run(main())
