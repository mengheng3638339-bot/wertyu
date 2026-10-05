# Telegram Customer File Delivery Bot

A Telegram bot for sending messages, photos, and documents from an authorized support account to customers. The owner can also publish photos and documents in a shared catalog for customers to browse and request. The catalog stores Telegram file IDs and names in a local SQLite database; file contents are kept by Telegram.

## Setup

1. Create a bot with [BotFather](https://t.me/BotFather) and copy its token.
2. Find the numeric Telegram user ID of the support account that may send files.
3. Install Python 3.10 or newer.
4. Create and activate a virtual environment, then install dependencies:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

5. Copy `.env.example` to `.env`, then set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ADMIN_ID` to the real values. Keep the real values only in `.env`; do not put them in `.env.example`.
6. Start the bot:

   ```powershell
   py bot.py
   ```

   The bot loads `.env` from the same folder as `bot.py`, regardless of the current working directory. Values in `.env` take precedence over stale environment variables from another bot configuration.

## Deliver a file

1. The customer must open the bot and send `/start` before the bot can message them.
2. The customer can send `/id` to get their Telegram username (if set) and numeric ID, then share them with support. Use the numeric ID for delivery.
3. To send a text message, the authorized admin sends `/message <customer_telegram_id> <message>` in a private chat with the bot.
4. To send media, the admin sends `/send <customer_telegram_id>`, then uploads one photo or document.
5. The bot reports whether Telegram accepted the delivery. Send `/cancel` to cancel a pending media delivery.

The bot command menu shows `/start`, `/help`, `/id`, `/files`, `/file`, `/upload`, `/done`, `/view`, and `/viewht` to all users. `/upload` starts a session that accepts multiple files; send `/done` when finished or `/cancel` to stop. `/files` shows clickable buttons for approved catalog items; `/view` creates temporary HTTPS browser links for approved HTML files; `/viewht` sends HTML files as downloads. The owner also has `/pending` to review submissions and `/previewht` to open an approved HTML preview on a phone over Wi-Fi or mobile data.

## Shared customer file catalog

1. Any user sends `/upload` in a private chat, then uploads one or more Telegram **documents** (each filename is its catalog name) or **photos with captions** (each caption is its name). Send `/done` to finish the upload session, or `/cancel` to stop.
2. The upload stays out of the shared catalog until the owner approves it. The owner uses `/pending`, then taps **Approve** or **Reject**. Users can cancel an upload with `/cancel` before sending the media.
3. Approved files appear in `/files`. Users tap a file button to receive it, or use `/file <file_name>` to request it by name. For names with spaces, include the full name, for example `/file Product Guide.pdf`.
4. The owner can upload directly; owner uploads are approved immediately and replace a catalog entry with the same name.
5. The owner can use `/delete`, select a catalog entry, and confirm to remove it from the bot's catalog. The **Delete all files** button removes every entry, including pending submissions, after a separate confirmation. These actions do not remove copies already sent in Telegram chats.
6. Any user can use `/view` and tap an approved `.html` or `.htm` button to open the page in a phone browser, including over 4G. The bot creates temporary public HTTPS links using Cloudflare Quick Tunnels while it runs. Anyone with a link can access that HTML page; share links only with intended viewers. Links stop working when the bot or tunnel stops, and new links should be requested after restarting.
7. `/viewht <file_name>` sends a specific approved HTML file as a download. The owner can use `/previewht` or `/previewht <file_name>` to open an approved HTML file on the phone through a temporary public HTTPS link. It works over Wi-Fi or mobile data while the bot and Cloudflare Quick Tunnel are running. Anyone with the link can view the page, so share it only with intended viewers. Telegram does not run interactive HTML inside a chat; a Telegram Web App still requires a public HTTPS URL.

Catalog entries persist in `file_catalog.sqlite3` beside `bot.py`; this file is ignored by Git. Keep it when moving or backing up the bot if the catalog should be preserved.

Only the configured admin user ID can start a delivery. Customer IDs must be positive numeric Telegram user IDs. Telegram may reject files that exceed its current bot API limits or chats that have not started the bot.

# ffgghh
