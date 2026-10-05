import asyncio
import functools
import logging
import os
import re
import shutil
import socket
import sqlite3
import threading
from contextlib import closing
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import uuid4

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

PENDING_CUSTOMER_KEY = "pending_customer_id"
PENDING_CATALOG_UPLOAD_KEY = "pending_catalog_upload"
CATALOG_DB_PATH = Path(__file__).resolve().with_name("file_catalog.sqlite3")
HTML_PREVIEW_DIR = Path(__file__).resolve().with_name("html_previews")
HTML_PREVIEW_PORT = 8765
_preview_server: ThreadingHTTPServer | None = None
_preview_server_lock = threading.Lock()
_cloudflared_process: asyncio.subprocess.Process | None = None
_cloudflared_output_task: asyncio.Task | None = None
_public_tunnel_url: str | None = None
_public_tunnel_ready = asyncio.Event()

load_dotenv(Path(__file__).resolve().with_name(".env"), override=True)


def get_config() -> tuple[str, int]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    admin_id_value = os.environ.get("TELEGRAM_ADMIN_ID", "").strip()

    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required.")
    if not admin_id_value:
        raise RuntimeError("TELEGRAM_ADMIN_ID is required.")

    try:
        admin_id = int(admin_id_value)
    except ValueError as error:
        raise RuntimeError("TELEGRAM_ADMIN_ID must be a numeric Telegram user ID.") from error

    if admin_id <= 0:
        raise RuntimeError("TELEGRAM_ADMIN_ID must be a positive Telegram user ID.")

    return token, admin_id


def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    admin_id = context.application.bot_data["admin_id"]
    return update.effective_user is not None and update.effective_user.id == admin_id


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    if is_admin(update, context):
        await update.message.reply_text(
            "Admin commands:\n"
            "/message <customer_telegram_id> <text> — send a message\n"
            "/send <customer_telegram_id> — choose a customer, then upload a photo or document\n"
            "/upload — submit a file for the owner to approve\n"
            "/done — finish adding files\n"
            "/pending — review user file submissions\n"
            "/delete — remove one or all files from the catalog\n"
            "/files — list files customers can request\n"
            "/view — choose an HTML file to download\n"
            "/previewht — open an HTML preview on this phone, including over 4G\n"
            "/viewht <file_name> — get an HTML file to open in a browser\n"
            "/cancel — cancel the pending delivery\n"
            "/help — show this message"
        )
        return

    await update.message.reply_text(
        "Welcome. This bot is used to send files from customer support. "
        f"Your Telegram ID is {update.effective_user.id}. "
        "Use /upload to submit a file for the owner to review."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    if not is_admin(update, context):
        await update.message.reply_text(
            "Use /files to see available files, /file <file_name> to receive one, and "
            "/view to choose an HTML file. Download it and open it in a browser to use its controls. "
            "Use /id to check your Telegram ID."
        )
        return

    await update.message.reply_text(
        "To deliver a file, send /send <customer_telegram_id>, then upload the file "
        "as a photo or document. Anyone can submit a file with /upload; approve submissions "
        "using /pending. Send multiple files after /upload, then use /done when finished. "
        "Customers use /files to browse approved files and /file <file_name> "
        "to request a catalog file, or /viewht <file_name> to get an HTML file to open "
        "in a browser. Use /cancel to stop an upload session or cancel a pending delivery."
    )


def initialize_catalog() -> None:
    with closing(sqlite3.connect(CATALOG_DB_PATH)) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                file_name TEXT PRIMARY KEY COLLATE NOCASE,
                file_id TEXT NOT NULL,
                media_type TEXT NOT NULL DEFAULT 'document',
                callback_key TEXT,
                uploaded_by INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'approved',
                uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(files)").fetchall()
        }
        if "media_type" not in columns:
            connection.execute(
                "ALTER TABLE files ADD COLUMN media_type TEXT NOT NULL DEFAULT 'document'"
            )
        if "callback_key" not in columns:
            connection.execute("ALTER TABLE files ADD COLUMN callback_key TEXT")
        if "status" not in columns:
            connection.execute(
                "ALTER TABLE files ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'"
            )
        rows_without_keys = connection.execute(
            "SELECT file_name FROM files WHERE callback_key IS NULL"
        ).fetchall()
        for (file_name,) in rows_without_keys:
            connection.execute(
                "UPDATE files SET callback_key = ? WHERE file_name = ?",
                (uuid4().hex, file_name),
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_files_callback_key ON files(callback_key)"
        )
        connection.commit()


def get_catalog_files() -> list[tuple[str, str]]:
    with closing(sqlite3.connect(CATALOG_DB_PATH)) as connection:
        rows = connection.execute(
            """
            SELECT file_name, callback_key FROM files
            WHERE status = 'approved'
            ORDER BY file_name COLLATE NOCASE
            """
        ).fetchall()
    return [(row[0], row[1]) for row in rows]


def get_catalog_file(file_name: str) -> tuple[str, str, str] | None:
    with closing(sqlite3.connect(CATALOG_DB_PATH)) as connection:
        row = connection.execute(
            """
            SELECT file_id, media_type, file_name FROM files
            WHERE file_name = ? AND status = 'approved'
            """,
            (file_name,),
        ).fetchone()
    return (row[0], row[1], row[2]) if row else None


def get_catalog_file_by_key(callback_key: str) -> tuple[str, str, str] | None:
    with closing(sqlite3.connect(CATALOG_DB_PATH)) as connection:
        row = connection.execute(
            """
            SELECT file_id, media_type, file_name FROM files
            WHERE callback_key = ? AND status = 'approved'
            """,
            (callback_key,),
        ).fetchone()
    return (row[0], row[1], row[2]) if row else None


def get_pending_catalog_files() -> list[tuple[str, str, int, str]]:
    with closing(sqlite3.connect(CATALOG_DB_PATH)) as connection:
        rows = connection.execute(
            """
            SELECT file_name, callback_key, uploaded_by, media_type
            FROM files WHERE status = 'pending'
            ORDER BY uploaded_at, file_name COLLATE NOCASE
            """
        ).fetchall()
    return [(row[0], row[1], row[2], row[3]) for row in rows]


def get_all_catalog_files() -> list[tuple[str, str, str]]:
    with closing(sqlite3.connect(CATALOG_DB_PATH)) as connection:
        rows = connection.execute(
            """
            SELECT file_name, callback_key, status FROM files
            ORDER BY file_name COLLATE NOCASE
            """
        ).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def delete_catalog_file(callback_key: str) -> str | None:
    with closing(sqlite3.connect(CATALOG_DB_PATH)) as connection:
        row = connection.execute(
            "SELECT file_name FROM files WHERE callback_key = ?",
            (callback_key,),
        ).fetchone()
        if row is None:
            return None
        connection.execute("DELETE FROM files WHERE callback_key = ?", (callback_key,))
        connection.commit()
    return row[0]


def delete_all_catalog_files() -> int:
    with closing(sqlite3.connect(CATALOG_DB_PATH)) as connection:
        cursor = connection.execute("DELETE FROM files")
        deleted_count = cursor.rowcount
        connection.commit()
    return deleted_count


def save_catalog_file(
    file_name: str,
    file_id: str,
    media_type: str,
    uploader_id: int,
    approved: bool,
) -> None:
    with closing(sqlite3.connect(CATALOG_DB_PATH)) as connection:
        values = (
            file_name,
            file_id,
            media_type,
            uuid4().hex,
            uploader_id,
            "approved" if approved else "pending",
        )
        if approved:
            connection.execute(
                """
                INSERT INTO files
                    (file_name, file_id, media_type, callback_key, uploaded_by, status)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(file_name) DO UPDATE SET
                    file_id = excluded.file_id,
                    media_type = excluded.media_type,
                    callback_key = excluded.callback_key,
                    uploaded_by = excluded.uploaded_by,
                    status = 'approved',
                    uploaded_at = CURRENT_TIMESTAMP
                """,
                values,
            )
        else:
            connection.execute(
                """
                INSERT INTO files
                    (file_name, file_id, media_type, callback_key, uploaded_by, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        connection.commit()


def approve_catalog_file(callback_key: str) -> str | None:
    with closing(sqlite3.connect(CATALOG_DB_PATH)) as connection:
        row = connection.execute(
            "SELECT file_name FROM files WHERE callback_key = ? AND status = 'pending'",
            (callback_key,),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            "UPDATE files SET status = 'approved' WHERE callback_key = ? AND status = 'pending'",
            (callback_key,),
        )
        connection.commit()
    return row[0]


def reject_catalog_file(callback_key: str) -> str | None:
    with closing(sqlite3.connect(CATALOG_DB_PATH)) as connection:
        row = connection.execute(
            "SELECT file_name FROM files WHERE callback_key = ? AND status = 'pending'",
            (callback_key,),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            "DELETE FROM files WHERE callback_key = ? AND status = 'pending'",
            (callback_key,),
        )
        connection.commit()
    return row[0]


async def upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    if update.effective_chat is None or update.effective_chat.type != "private":
        await update.message.reply_text("For privacy, submit files using /upload in a private chat.")
        return

    context.user_data.pop(PENDING_CUSTOMER_KEY, None)
    context.user_data[PENDING_CATALOG_UPLOAD_KEY] = True
    await update.message.reply_text(
        "Upload one or more documents (each filename becomes its listing name), or send "
        "photos with captions to use as their listing names. Your submissions will be visible "
        "to customers only after the owner approves them. Send /done when finished or /cancel "
        "to stop."
    )


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    if context.user_data.pop(PENDING_CATALOG_UPLOAD_KEY, None) is None:
        await update.message.reply_text("There is no active file upload to finish.")
        return

    await update.message.reply_text("File upload session finished.")


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if not is_admin(update, context):
        await update.message.reply_text("Only the owner can review file submissions.")
        return

    try:
        pending_files = get_pending_catalog_files()
    except sqlite3.Error:
        logger.exception("Could not read pending file submissions.")
        await update.message.reply_text("The submission queue is temporarily unavailable.")
        return

    if not pending_files:
        await update.message.reply_text("There are no files waiting for approval.")
        return

    await update.message.reply_text("Review submissions. Approving adds the file to the shared catalog.")
    for file_name, callback_key, uploader_id, media_type in pending_files:
        await update.message.reply_text(
            f"{file_name} ({media_type}), submitted by user {uploader_id}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text="Approve",
                            callback_data=f"approve:{callback_key}",
                        ),
                        InlineKeyboardButton(
                            text="Reject",
                            callback_data=f"reject:{callback_key}",
                        ),
                    ]
                ]
            ),
        )


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    if not is_admin(update, context):
        await update.message.reply_text("Only the owner can delete files from the catalog.")
        return

    try:
        files = get_all_catalog_files()
    except sqlite3.Error:
        logger.exception("Could not list catalog files for deletion.")
        await update.message.reply_text("The file list is temporarily unavailable.")
        return

    if not files:
        await update.message.reply_text("There are no files in the catalog.")
        return

    await update.message.reply_text(
        "Choose a catalog entry to delete, or delete all entries. You will be asked to confirm."
    )
    for start in range(0, len(files), 50):
        buttons = [
            [
                InlineKeyboardButton(
                    text=(
                        f"{name} ({status})"
                        if len(name) + len(status) + 3 <= 64
                        else f"{name[:58]}..."
                    ),
                    callback_data=f"delete-select:{callback_key}",
                )
            ]
            for name, callback_key, status in files[start : start + 50]
        ]
        if start == 0:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text="Delete all files",
                        callback_data="delete-all-select:all",
                    )
                ]
            )
        await update.message.reply_text(
            "Catalog files:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def files_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    try:
        file_names = get_catalog_files()
    except sqlite3.Error:
        logger.exception("Could not read the customer file catalog.")
        await update.message.reply_text("The file list is temporarily unavailable. Please try again later.")
        return

    if not file_names:
        await update.message.reply_text("There are no files available yet.")
        return

    await send_catalog_buttons(update.message.reply_text, file_names, "Choose a file:", "catalog")


async def send_catalog_buttons(reply_text, file_names, prompt: str, callback_prefix: str) -> None:
    await reply_text(prompt)
    for start in range(0, len(file_names), 50):
        buttons = [
            [
                InlineKeyboardButton(
                    text=file_name if len(file_name) <= 64 else f"{file_name[:61]}...",
                    callback_data=f"{callback_prefix}:{callback_key}",
                )
            ]
            for file_name, callback_key in file_names[start : start + 50]
        ]
        await reply_text(
            "Available files:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


async def send_html_browser_buttons(reply_text, file_names, bot) -> None:
    await ensure_public_tunnel()
    await reply_text(
        "Tap an HTML file to open it in your phone's browser. This temporary public link works "
        "over 4G while the bot is running; share it only with people who should see the page."
    )
    for start in range(0, len(file_names), 20):
        buttons = []
        for file_name, callback_key in file_names[start : start + 20]:
            catalog_file = get_catalog_file_by_key(callback_key)
            if catalog_file is None:
                continue
            file_id, media_type, catalog_name = catalog_file
            if media_type != "document" or Path(catalog_name).suffix.lower() not in {".html", ".htm"}:
                continue
            url = await open_public_html_preview(bot, file_id, catalog_name)
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=catalog_name if len(catalog_name) <= 64 else f"{catalog_name[:61]}...",
                        url=url,
                    )
                ]
            )
        if buttons:
            await reply_text(
                "Tap a page to open it:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )


async def catalog_file_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return

    callback_type, _, callback_key = query.data.partition(":")
    if callback_type not in {
        "catalog",
        "html",
        "preview",
        "approve",
        "reject",
        "delete-select",
        "delete-confirm",
        "delete-cancel",
        "delete-all-select",
        "delete-all-confirm",
        "delete-all-cancel",
    } or not callback_key:
        await query.answer()
        await query.message.reply_text("That selection is invalid. Please reopen the command.")
        return
    if callback_type.startswith("delete-"):
        if not is_admin(update, context):
            await query.answer("Only the owner can delete catalog files.", show_alert=True)
            return
        if callback_type == "delete-all-select":
            try:
                files = get_all_catalog_files()
            except sqlite3.Error:
                logger.exception("Could not count catalog entries before bulk deletion.")
                await query.answer("Could not read the catalog.", show_alert=True)
                return
            await query.answer()
            await query.edit_message_text(
                f"Delete all {len(files)} catalog entries, including pending submissions? "
                "This cannot be undone.",
                reply_markup=InlineKeyboardMarkup(
                    [[
                        InlineKeyboardButton(
                            "Confirm delete all",
                            callback_data="delete-all-confirm:all",
                        ),
                        InlineKeyboardButton(
                            "Keep all files",
                            callback_data="delete-all-cancel:all",
                        ),
                    ]]
                ),
            )
            return
        if callback_type == "delete-all-cancel":
            await query.answer()
            await query.edit_message_text("Bulk deletion cancelled. All catalog entries were kept.")
            return
        if callback_type == "delete-all-confirm":
            try:
                deleted_count = delete_all_catalog_files()
            except sqlite3.Error:
                logger.exception("Could not delete all catalog entries.")
                await query.answer("The catalog could not be cleared.", show_alert=True)
                return
            await query.answer()
            await query.edit_message_text(
                f"Deleted {deleted_count} catalog entries. The original Telegram uploads are not deleted."
            )
            return
        if callback_type == "delete-select":
            try:
                catalog_file = get_catalog_file_by_key(callback_key)
                if catalog_file is None:
                    with closing(sqlite3.connect(CATALOG_DB_PATH)) as connection:
                        row = connection.execute(
                            "SELECT file_name FROM files WHERE callback_key = ?",
                            (callback_key,),
                        ).fetchone()
                    file_name = row[0] if row else None
                else:
                    file_name = catalog_file[2]
            except sqlite3.Error:
                logger.exception("Could not inspect selected file for deletion.")
                await query.answer("Could not find that catalog entry.", show_alert=True)
                return
            await query.answer()
            if file_name is None:
                await query.edit_message_text("That catalog entry is already gone.")
                return
            await query.edit_message_text(
                f"Delete “{file_name}” from the bot catalog? This cannot be undone.",
                reply_markup=InlineKeyboardMarkup(
                    [[
                        InlineKeyboardButton("Confirm delete", callback_data=f"delete-confirm:{callback_key}"),
                        InlineKeyboardButton("Keep file", callback_data=f"delete-cancel:{callback_key}"),
                    ]]
                ),
            )
            return
        if callback_type == "delete-cancel":
            await query.answer()
            await query.edit_message_text("Deletion cancelled. The catalog entry was kept.")
            return
        try:
            file_name = delete_catalog_file(callback_key)
        except sqlite3.Error:
            logger.exception("Could not delete catalog entry.")
            await query.answer("The file could not be deleted.", show_alert=True)
            return
        await query.answer()
        if file_name is None:
            await query.edit_message_text("That catalog entry is already gone.")
        else:
            await query.edit_message_text(
                f"Deleted “{file_name}” from the bot catalog. The original Telegram upload is not deleted."
            )
        return
    if callback_type in {"approve", "reject"}:
        if not is_admin(update, context):
            await query.answer("Only the owner can review file submissions.", show_alert=True)
            return
        try:
            file_name = (
                approve_catalog_file(callback_key)
                if callback_type == "approve"
                else reject_catalog_file(callback_key)
            )
        except sqlite3.Error:
            logger.exception("Could not process a pending file submission.")
            await query.answer("The submission could not be updated.", show_alert=True)
            return
        await query.answer()
        if file_name is None:
            await query.edit_message_text("This submission was already reviewed or removed.")
        elif callback_type == "approve":
            await query.edit_message_text(f"Approved: {file_name}")
        else:
            await query.edit_message_text(f"Rejected: {file_name}")
        return
    if callback_type == "preview" and not is_admin(update, context):
        await query.answer("Only the owner can open an HTML preview.", show_alert=True)
        return
    await query.answer()
    try:
        catalog_file = get_catalog_file_by_key(callback_key)
    except sqlite3.Error:
        logger.exception("Could not look up a selected catalog file.")
        await query.message.reply_text("The file list is temporarily unavailable. Please try again later.")
        return

    if catalog_file is None:
        await query.message.reply_text(
            "That file is no longer available. Send /files to refresh the list."
        )
        return

    if callback_type == "preview":
        file_id, media_type, file_name = catalog_file
        if media_type != "document" or Path(file_name).suffix.lower() not in {".html", ".htm"}:
            await query.message.reply_text(
                "That HTML file is no longer available. Send /previewht to refresh the list."
            )
            return
        try:
            preview_url = await open_public_html_preview(context.bot, file_id, file_name)
        except (TelegramError, OSError, RuntimeError) as error:
            logger.exception("Could not prepare HTML preview.")
            await query.message.reply_text(f"Could not prepare the HTML preview: {error}")
            return
        try:
            await query.message.reply_text(
                f"Open “{file_name}” on this phone using the temporary public HTTPS link. "
                "Anyone with the link can view this page while the bot is running; share it "
                "only with intended viewers.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("Open HTML preview", url=preview_url)]]
                ),
            )
        except TelegramError:
            logger.exception("Could not send the HTML preview link.")
        return

    try:
        if callback_type == "html":
            file_id, media_type, file_name = catalog_file
            if media_type != "document" or Path(file_name).suffix.lower() not in {".html", ".htm"}:
                await query.message.reply_text(
                    "That HTML file is no longer available. Send /viewht to refresh the list."
                )
                return
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=file_id,
                filename=Path(file_name).name,
                caption=(
                    "After downloading, open this file from your phone's Downloads app "
                    "and choose a browser or SPCK Editor to use the page controls."
                ),
            )
        else:
            await send_catalog_file(context.bot, query.message.chat_id, catalog_file)
    except TelegramError as error:
        logger.warning("Could not send selected catalog file: %s", error)
        await query.message.reply_text(
            "Telegram could not send that file. Please contact the support team."
        )
        return
    except (OSError, RuntimeError) as error:
        logger.exception("Could not process selected catalog file.")
        await query.message.reply_text(
            f"Could not process that catalog file: {error}"
        )
        return

    if callback_type == "html":
        await query.message.reply_text(
            f"Sent “{catalog_file[2]}”. On your phone, download it, open it from Downloads, "
            "then choose a browser or SPCK Editor. Telegram cannot launch a local HTML file "
            "directly in another app."
        )
    else:
        await query.message.reply_text(f"Sent: {catalog_file[2]}")


async def send_catalog_file(bot, chat_id: int, catalog_file: tuple[str, str, str]) -> None:
    file_id, media_type, file_name = catalog_file
    if media_type == "photo":
        await bot.send_photo(chat_id=chat_id, photo=file_id)
    else:
        await bot.send_document(
            chat_id=chat_id,
            document=file_id,
            filename=file_name,
        )


async def open_html_preview(bot, file_id: str, file_name: str) -> str:
    HTML_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file_name).suffix.lower()
    preview_name = f"{uuid4().hex}{suffix}"
    preview_path = HTML_PREVIEW_DIR / preview_name
    telegram_file = await bot.get_file(file_id)
    await telegram_file.download_to_drive(custom_path=preview_path)
    server = ensure_preview_server()
    host = server.server_address[0]
    return f"http://{host}:{server.server_port}/{quote(preview_name)}"


async def open_public_html_preview(bot, file_id: str, file_name: str) -> str:
    local_preview_url = await open_html_preview(bot, file_id, file_name)
    public_url = await ensure_public_tunnel()
    return f"{public_url.rstrip('/')}{urlsplit(local_preview_url).path}"


def ensure_preview_server() -> ThreadingHTTPServer:
    global _preview_server
    with _preview_server_lock:
        if _preview_server is None:
            HTML_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
            handler = functools.partial(
                PreviewRequestHandler,
                directory=str(HTML_PREVIEW_DIR),
            )
            _preview_server = ThreadingHTTPServer((get_lan_ip(), HTML_PREVIEW_PORT), handler)
            server_thread = threading.Thread(
                target=_preview_server.serve_forever,
                name="html-preview-server",
                daemon=True,
            )
            server_thread.start()
        return _preview_server


def get_lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 80))
        except OSError as error:
            raise RuntimeError(
                "Could not determine this computer's LAN address. Connect it to the same Wi-Fi as the phone."
            ) from error
        address = probe.getsockname()[0]

    if address.startswith("127.") or address == "0.0.0.0":
        raise RuntimeError(
            "No LAN address is available. Connect the bot computer and phone to the same Wi-Fi."
        )
    return address


class PreviewRequestHandler(SimpleHTTPRequestHandler):
    def list_directory(self, path: str):
        self.send_error(404)
        return None


async def file_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_chat is None:
        return

    file_name = " ".join(context.args).strip()
    if not file_name:
        await update.message.reply_text("Usage: /file <file_name>. Use /files to see available names.")
        return

    try:
        catalog_file = get_catalog_file(file_name)
    except sqlite3.Error:
        logger.exception("Could not look up a requested catalog file.")
        await update.message.reply_text("The file is temporarily unavailable. Please try again later.")
        return

    if catalog_file is None:
        await update.message.reply_text(
            f"No file named “{file_name}” was found. Use /files to see available names."
        )
        return

    try:
        await send_catalog_file(context.bot, update.effective_chat.id, catalog_file)
    except TelegramError as error:
        logger.warning("Could not send catalog file %r: %s", file_name, error)
        await update.message.reply_text(
            "Telegram could not send that file. Please contact the support team."
        )
        return

    await update.message.reply_text(f"Sent: {file_name}")


async def preview_html_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_chat is None:
        return

    if not is_admin(update, context):
        await update.message.reply_text(
            "Only the owner can create an HTML preview link. "
            "Customers can use /view to open approved HTML files in their browser."
        )
        return

    file_name = " ".join(context.args).strip()
    if not file_name:
        try:
            file_names = [
                (name, callback_key)
                for name, callback_key in get_catalog_files()
                if Path(name).suffix.lower() in {".html", ".htm"}
            ]
        except sqlite3.Error:
            logger.exception("Could not list HTML catalog files.")
            await update.message.reply_text(
                "The HTML file list is temporarily unavailable. Please try again later."
            )
            return
        if not file_names:
            await update.message.reply_text("There are no HTML files available yet.")
            return
        await send_catalog_buttons(
            update.message.reply_text,
            file_names,
            "Choose an HTML file to preview on this phone over Wi-Fi or mobile data. "
            "The bot computer must stay connected to the internet:",
            "preview",
        )
        return

    if Path(file_name).suffix.lower() not in {".html", ".htm"}:
        await update.message.reply_text(
            "Only .html or .htm files can be previewed. Use /previewht to choose one."
        )
        return

    try:
        catalog_file = get_catalog_file(file_name)
    except sqlite3.Error:
        logger.exception("Could not look up requested HTML catalog file.")
        await update.message.reply_text(
            "The HTML file is temporarily unavailable. Please try again later."
        )
        return

    if catalog_file is None:
        await update.message.reply_text(
            f"No HTML file named “{file_name}” was found. Use /files to see available names."
        )
        return

    try:
        file_id, media_type, catalog_name = catalog_file
        if media_type != "document":
            await update.message.reply_text("That catalog entry is not an HTML document.")
            return
        preview_url = await open_public_html_preview(context.bot, file_id, catalog_name)
    except (TelegramError, OSError, RuntimeError) as error:
        logger.exception("Could not prepare requested HTML preview.")
        await update.message.reply_text(
            f"Could not prepare the HTML preview: {error}"
        )
        return

    await update.message.reply_text(
        f"Open “{catalog_name}” on this phone using the temporary public HTTPS link. "
        "Anyone with the link can view this page while the bot is running; share it "
        "only with intended viewers.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Open HTML preview", url=preview_url)]]
        ),
    )


async def view_html_file_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_chat is None:
        return

    file_name = " ".join(context.args).strip()
    if not file_name:
        try:
            file_names = [
                (name, callback_key)
                for name, callback_key in get_catalog_files()
                if Path(name).suffix.lower() in {".html", ".htm"}
            ]
        except sqlite3.Error:
            logger.exception("Could not list HTML catalog files.")
            await update.message.reply_text(
                "The HTML file list is temporarily unavailable. Please try again later."
            )
            return
        if not file_names:
            await update.message.reply_text("There are no HTML files available yet.")
            return
        try:
            await send_html_browser_buttons(update.message.reply_text, file_names, context.bot)
        except (TelegramError, OSError, RuntimeError, sqlite3.Error) as error:
            logger.exception("Could not prepare LAN browser links for HTML files.")
            await update.message.reply_text(
                f"Could not prepare the HTML links. Check that this computer is connected "
                f"to the same Wi-Fi as the phone. Details: {error}"
            )
        return

    if Path(file_name).suffix.lower() not in {".html", ".htm"}:
        await update.message.reply_text(
            "Only .html or .htm files can be sent with this command."
        )
        return

    try:
        catalog_file = get_catalog_file(file_name)
    except sqlite3.Error:
        logger.exception("Could not look up requested HTML catalog file.")
        await update.message.reply_text(
            "The HTML file is temporarily unavailable. Please try again later."
        )
        return

    if catalog_file is None:
        await update.message.reply_text(
            f"No HTML file named “{file_name}” was found. Use /files to see available names."
        )
        return

    file_id, media_type, catalog_name = catalog_file
    if media_type != "document":
        await update.message.reply_text("That catalog entry is not an HTML document.")
        return

    try:
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=file_id,
            filename=Path(catalog_name).name,
            caption=(
                "After downloading, open this file from your phone's Downloads app "
                "and choose a browser or SPCK Editor to use the page controls."
            ),
        )
    except TelegramError as error:
        logger.warning("Could not send HTML catalog file %r: %s", file_name, error)
        await update.message.reply_text(
            "Telegram could not send that HTML file. Please contact the support team."
        )
        return

    await update.message.reply_text(
        f"Sent “{catalog_name}”. On your phone, download it, open it from Downloads, "
        "then choose a browser or SPCK Editor. Telegram cannot launch a local HTML file "
        "directly in another app."
    )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    user = update.effective_user
    username = f"@{user.username}" if user.username else "not set"
    await update.message.reply_text(
        f"Your Telegram username: {username}\nYour Telegram ID: {user.id}"
    )


async def message_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    if not is_admin(update, context):
        await update.message.reply_text("This command is available to the support team only.")
        return

    if update.effective_chat is None or update.effective_chat.type != "private":
        await update.message.reply_text("For privacy, use /message in a private chat with this bot.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /message <customer_telegram_id> <message>")
        return

    try:
        customer_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("The customer Telegram ID must be a number.")
        return

    if customer_id <= 0:
        await update.message.reply_text("Enter a positive customer Telegram user ID.")
        return

    text = " ".join(context.args[1:])
    try:
        await context.bot.send_message(chat_id=customer_id, text=text)
    except TelegramError as error:
        logger.warning("Could not send a message to customer %s: %s", customer_id, error)
        await update.message.reply_text(
            f"Telegram could not send a message to customer {customer_id}. "
            "Ask the customer to open the bot with /start and try again."
        )
        return

    await update.message.reply_text(f"Message sent to customer {customer_id}.")


async def send_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    if not is_admin(update, context):
        await update.message.reply_text("This command is available to the support team only.")
        return

    if update.effective_chat is None or update.effective_chat.type != "private":
        await update.message.reply_text("For privacy, use /send in a private chat with this bot.")
        return

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /send <customer_telegram_id>")
        return

    try:
        customer_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("The customer Telegram ID must be a number.")
        return

    if customer_id <= 0:
        await update.message.reply_text("Enter a positive customer Telegram user ID.")
        return

    context.user_data.pop(PENDING_CATALOG_UPLOAD_KEY, None)
    context.user_data[PENDING_CUSTOMER_KEY] = customer_id
    await update.message.reply_text(
        f"Ready to send to customer {customer_id}. Upload a photo or document, "
        "or use /cancel."
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    if context.user_data.pop(PENDING_CATALOG_UPLOAD_KEY, None) is not None:
        await update.message.reply_text("Your pending upload was cancelled.")
        return

    if not is_admin(update, context):
        await update.message.reply_text("This command is available to the support team only.")
        return

    had_pending_delivery = context.user_data.pop(PENDING_CUSTOMER_KEY, None) is not None
    if not had_pending_delivery:
        await update.message.reply_text("There is no pending delivery to cancel.")
        return

    await update.message.reply_text("Delivery cancelled.")


async def deliver_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    document = update.message.document
    photo = update.message.photo[-1] if update.message.photo else None
    if document is None and photo is None:
        return

    if update.effective_user is None:
        return

    if update.effective_chat is None or update.effective_chat.type != "private":
        await update.message.reply_text("For privacy, upload media in a private chat with this bot.")
        return

    if context.user_data.get(PENDING_CATALOG_UPLOAD_KEY):
        if document is not None:
            if not document.file_name:
                await update.message.reply_text(
                    "This document has no filename. Rename it and upload it again."
                )
                return
            file_name = document.file_name
            file_id = document.file_id
            media_type = "document"
        else:
            file_name = (update.message.caption or "").strip()
            if not file_name:
                await update.message.reply_text(
                    "Add a caption to this photo to use as its name in /files, then send it again."
                )
                return
            file_id = photo.file_id
            media_type = "photo"
        try:
            uploader_id = update.effective_user.id
            approved = uploader_id == context.application.bot_data["admin_id"]
            save_catalog_file(file_name, file_id, media_type, uploader_id, approved)
        except sqlite3.IntegrityError:
            await update.message.reply_text(
                "A file with that name already exists or is waiting for approval. "
                "Rename the file or change the photo caption, then try again."
            )
            return
        except sqlite3.Error:
            logger.exception("Could not save an uploaded file to the customer catalog.")
            await update.message.reply_text(
                "Could not save this file to the catalog. Please try again or use /cancel."
            )
            return

        if approved:
            await update.message.reply_text(
                f"Added “{file_name}” to the customer file list. Customers can use "
                f"/file {file_name} to receive it. Send another file or /done when finished."
            )
        else:
            await update.message.reply_text(
                f"Submitted “{file_name}” for owner approval. It will appear in /files "
                "after it is approved. Send another file or /done when finished."
            )
        return

    if not is_admin(update, context):
        await update.message.reply_text(
            "Use /upload before sending a file for owner approval."
        )
        return

    customer_id = context.user_data.get(PENDING_CUSTOMER_KEY)
    if customer_id is None:
        await update.message.reply_text("Choose a customer first with /send <customer_telegram_id>.")
        return

    try:
        if photo is not None:
            await context.bot.send_photo(
                chat_id=customer_id,
                photo=photo.file_id,
                caption="A photo from customer support.",
            )
        else:
            await context.bot.send_document(
                chat_id=customer_id,
                document=document.file_id,
                caption="A file from customer support.",
            )
    except TelegramError as error:
        logger.warning("Could not deliver media to customer %s: %s", customer_id, error)
        await update.message.reply_text(
            f"Telegram could not deliver the media to customer {customer_id}. "
            "Ask the customer to open the bot with /start, then try again or use /cancel."
        )
        return

    context.user_data.pop(PENDING_CUSTOMER_KEY, None)
    await update.message.reply_text(f"Media delivered to customer {customer_id}.")


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled bot error while processing update %r", update, exc_info=context.error)


async def configure_commands(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Start the bot"),
            BotCommand("help", "Show help"),
            BotCommand("id", "Show your Telegram ID"),
            BotCommand("files", "List available files"),
            BotCommand("file", "Request a file by name"),
            BotCommand("view", "Choose an HTML file to download"),
            BotCommand("viewht", "Download an HTML file by name"),
            BotCommand("upload", "Submit a file for owner approval"),
            BotCommand("done", "Finish adding files"),
        ],
        scope=BotCommandScopeDefault(),
    )
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Start the bot"),
            BotCommand("help", "Show help"),
            BotCommand("id", "Show your Telegram ID"),
            BotCommand("files", "List available files"),
            BotCommand("file", "Request a file by name"),
            BotCommand("view", "Choose an HTML file to download"),
            BotCommand("previewht", "Preview HTML on this phone over Wi-Fi or 4G"),
            BotCommand("viewht", "Download an HTML file for your device"),
            BotCommand("message", "Send a message to a customer"),
            BotCommand("send", "Send a file to a customer"),
            BotCommand("upload", "Submit a file for owner approval"),
            BotCommand("done", "Finish adding files"),
            BotCommand("pending", "Review submitted files"),
            BotCommand("delete", "Delete one or all catalog files"),
            BotCommand("cancel", "Cancel a pending delivery"),
        ],
        scope=BotCommandScopeChat(chat_id=application.bot_data["admin_id"]),
    )
    logger.info("Telegram command menus configured.")
    try:
        await ensure_public_tunnel()
    except (OSError, RuntimeError, asyncio.TimeoutError) as error:
        logger.error("Public HTML tunnel could not start: %s", error)


async def drain_cloudflared_output(
    process: asyncio.subprocess.Process,
) -> None:
    global _public_tunnel_url
    if process.stdout is None:
        return

    async for raw_line in process.stdout:
        line = raw_line.decode("utf-8", errors="replace").strip()
        match = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if match and _public_tunnel_url is None:
            _public_tunnel_url = match.group(0)
            _public_tunnel_ready.set()
            logger.info("Temporary public HTML tunnel is ready.")
        elif "ERR" in line or "error" in line.lower():
            logger.warning("Cloudflare tunnel: %s", line)

    if not _public_tunnel_ready.is_set():
        logger.error("Cloudflare tunnel process exited before creating a public URL.")


async def ensure_public_tunnel() -> str:
    global _cloudflared_process, _cloudflared_output_task
    global _public_tunnel_url, _public_tunnel_ready

    if _public_tunnel_url:
        if _cloudflared_process is not None and _cloudflared_process.returncode is None:
            return _public_tunnel_url
        _public_tunnel_url = None
        _public_tunnel_ready = asyncio.Event()

    if _cloudflared_process is None or _cloudflared_process.returncode is not None:
        executable = os.environ.get("CLOUDFLARED_PATH", "").strip()
        executable = executable or shutil.which("cloudflared") or shutil.which("cloudflared.exe")
        if not executable:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if local_app_data:
                package_candidates = sorted(
                    (Path(local_app_data) / "Microsoft" / "WinGet" / "Packages").glob(
                        "Cloudflare.cloudflared_*"
                    )
                )
                for package_path in package_candidates:
                    candidate = package_path / "cloudflared.exe"
                    if candidate.is_file():
                        executable = str(candidate)
                        break
        if not executable:
            raise RuntimeError(
                "cloudflared is not installed. Install Cloudflare cloudflared, then restart the bot."
            )

        local_origin = f"http://{get_lan_ip()}:{HTML_PREVIEW_PORT}"
        _cloudflared_process = await asyncio.create_subprocess_exec(
            executable,
            "tunnel",
            "--no-autoupdate",
            "--url",
            local_origin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        _public_tunnel_ready = asyncio.Event()
        _cloudflared_output_task = asyncio.create_task(
            drain_cloudflared_output(_cloudflared_process)
        )

    try:
        await asyncio.wait_for(_public_tunnel_ready.wait(), timeout=60)
    except asyncio.TimeoutError as error:
        process = _cloudflared_process
        if process is not None and process.returncode is None:
            process.terminate()
            await process.wait()
        _cloudflared_process = None
        raise RuntimeError(
            "cloudflared did not provide a public HTTPS URL within 60 seconds."
        ) from error

    if _public_tunnel_url is None:
        raise RuntimeError("cloudflared exited without creating a public HTTPS URL.")
    return _public_tunnel_url


async def stop_public_tunnel(application: Application) -> None:
    global _cloudflared_process, _cloudflared_output_task, _public_tunnel_url
    process = _cloudflared_process
    if process is not None and process.returncode is None:
        process.terminate()
        await process.wait()
    if _cloudflared_output_task is not None:
        await asyncio.gather(_cloudflared_output_task, return_exceptions=True)
    _cloudflared_process = None
    _cloudflared_output_task = None
    _public_tunnel_url = None


def main() -> None:
    token, admin_id = get_config()
    initialize_catalog()
    ensure_preview_server()
    application = (
        Application.builder()
        .token(token)
        .post_init(configure_commands)
        .post_shutdown(stop_public_tunnel)
        .build()
    )
    application.bot_data["admin_id"] = admin_id

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("files", files_command))
    application.add_handler(
        CallbackQueryHandler(
            catalog_file_callback,
            pattern=(
                r"^(catalog|html|preview|approve|reject|delete-select|delete-confirm|"
                r"delete-cancel|delete-all-select|delete-all-confirm|delete-all-cancel):"
            ),
        )
    )
    application.add_handler(CommandHandler("file", file_command))
    application.add_handler(CommandHandler("view", view_html_file_command))
    application.add_handler(CommandHandler("previewht", preview_html_command))
    application.add_handler(CommandHandler("viewht", view_html_file_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("message", message_command))
    application.add_handler(CommandHandler("send", send_command))
    application.add_handler(CommandHandler("upload", upload_command))
    application.add_handler(CommandHandler("done", done_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.ALL, deliver_media)
    )
    application.add_error_handler(handle_error)

    logger.info("Starting Telegram file delivery bot.")
    application.run_polling()


if __name__ == "__main__":
    main()
