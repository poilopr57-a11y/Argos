"""Standalone Telegram mini-app bot for Argos VPN service."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from src.vpn_service.database import Database
from src.vpn_service.traffic_daemon import collect_traffic
from src.vpn_service.wg_manager import WireGuardManager

logger = logging.getLogger("argos.vpn.bot")

BOT_TOKEN = os.getenv("ARGOS_VPN_BOT_TOKEN") or os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("ARGOS_VPN_DB_PATH")
WG_INTERFACE = os.getenv("ARGOS_VPN_INTERFACE", "wg0")
SERVER_IP = os.getenv("ARGOS_VPN_SERVER_IP") or os.getenv("SERVER_IP", "your-server.com")
RATE_LIMIT_SECONDS = int(os.getenv("ARGOS_VPN_RATE_LIMIT_SECONDS", "600"))
WEBAPP_URL = os.getenv("ARGOS_VPN_WEBAPP_URL", "")


def _db() -> Database:
    return Database(db_path=DB_PATH)


def _wg() -> WireGuardManager:
    return WireGuardManager(interface=WG_INTERFACE)


def _safe_username(user) -> str:
    if user.username:
        name = user.username.strip()
        if name:
            return name[:64]
    return str(user.id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    username = _safe_username(user)
    keyboard = [
        [InlineKeyboardButton("Получить VPN-конфиг", callback_data="get_config")],
        [InlineKeyboardButton("Открыть Argos VPN mini-app", web_app=WebAppInfo(url=WEBAPP_URL))] if WEBAPP_URL else [],
        [InlineKeyboardButton("Статус", callback_data="status")],
        [InlineKeyboardButton("Помощь", callback_data="help")],
    ]
    keyboard = [row for row in keyboard if row]
    await update.message.reply_text(
        f"Привет, @{username}!\nЯ бот VPN-сервиса АРГОС.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    if not user:
        return
    telegram_id = user.id
    username = _safe_username(user)

    if query.data == "get_config":
        await _handle_get_config(query, telegram_id, username)
    elif query.data == "status":
        await _handle_status(query, telegram_id, username)
    elif query.data == "help":
        await query.edit_message_text("Нажми «Получить VPN-конфиг» — файл импортируется в WireGuard.")


async def _handle_get_config(query, telegram_id: int, username: str) -> None:
    db = _db()
    wg = _wg()

    last = db.get_last_key_time(telegram_id)
    if last and (time.time() - last) < RATE_LIMIT_SECONDS:
        await query.answer("Подожди 10 минут между запросами", show_alert=True)
        return

    user = db.get_user(telegram_id)
    if not user:
        user = db.create_user(telegram_id, username)

    existing = db.get_active_key(user["id"])
    if existing:
        config = wg.generate_client_config(
            existing["private_key"], existing["ip_address"], server_ip=SERVER_IP
        )
        buf = io.BytesIO(config.encode())
        buf.name = f"argos_{username}.conf"
        await query.message.reply_document(
            document=buf,
            filename=f"argos_{username}.conf",
            caption=f"Сервер: {SERVER_IP}\nТариф: Free (3 дня)\n(используется существующий ключ).",
        )
        await query.edit_message_text("Конфиг отправлен (существующий ключ).")
        return

    try:
        ip = db.allocate_ip()
    except RuntimeError as exc:
        await query.edit_message_text(f"Ошибка: {exc}")
        return

    try:
        kp = wg.generate_keypair()
    except RuntimeError as exc:
        db.release_ip(ip)
        await query.edit_message_text(f"Ошибка генерации ключей: {exc}")
        return

    db.create_key(user["id"], kp["private_key"], kp["public_key"], ip, ttl_days=3)
    try:
        wg.add_peer(kp["public_key"], ip)
    except Exception as exc:
        db.deactivate_key(kp["public_key"])
        await query.edit_message_text(f"Ошибка WireGuard: {exc}")
        return

    config = wg.generate_client_config(kp["private_key"], ip, server_ip=SERVER_IP)
    buf = io.BytesIO(config.encode())
    buf.name = f"argos_{username}.conf"
    await query.message.reply_document(
        document=buf,
        filename=f"argos_{username}.conf",
        caption=f"Сервер: {SERVER_IP}\nТариф: Free (3 дня)",
    )
    await query.edit_message_text("Конфиг готов.")


async def _handle_status(query, telegram_id: int, username: str) -> None:
    db = _db()
    user = db.get_user(telegram_id)
    if not user:
        await query.edit_message_text("Сначала получи конфиг.")
        return
    key = db.get_active_key(user["id"])
    if not key:
        await query.edit_message_text("Нет активного конфига.")
        return
    traffic_bytes = db.get_traffic(telegram_id)
    days_left = max(0, (key["expires_at"] - int(time.time())) // 86400)
    await query.edit_message_text(
        f"Статус\n"
        f"Пользователь: @{username}\n"
        f"IP: {key['ip_address']}\n"
        f"Истекает: {days_left} дн.\n"
        f"Трафик: {round(traffic_bytes / (1024**3), 2)} GB\n"
        f"Лимит: 5 GB"
    )


async def traffic_collector_task(app: Application) -> None:
    db = _db()
    wg = _wg()
    await collect_traffic(db, wg)


def main(token: Optional[str] = None) -> None:
    token = token or BOT_TOKEN
    if not token:
        raise RuntimeError("ARGOS_VPN_BOT_TOKEN not set")
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.post_init = lambda app: asyncio.create_task(traffic_collector_task(app))
    application.run_polling()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
