import os
import logging
import sqlite3
import uuid
import base64
from fastapi import FastAPI, Request, Response
from fastapi.templating import Jinja2Templates

# --- Corrected Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Database Setup ---
DB_NAME = "links.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    # Create table if it doesn't exist
    with get_db_connection() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS protected_links (
                id TEXT PRIMARY KEY,
                group_link TEXT NOT NULL
            )"""
        )
        conn.commit()

# --- Telegram Bot Logic ---
# This is a standard PTB application setup
telegram_bot_app = Application.builder().token(os.environ.get("TELEGRAM_TOKEN")).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command, both general and with a payload."""
    if context.args:
        encoded_id = context.args[0]
        with get_db_connection() as conn:
            link_data = conn.execute("SELECT group_link FROM protected_links WHERE id = ?", (encoded_id,)).fetchone()

        if link_data:
            group_link = link_data["group_link"]
            # Use RENDER_EXTERNAL_URL for the web app URL
            web_app_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/join?token={encoded_id}"
            
            keyboard = [[InlineKeyboardButton("Join Group", web_app=WebAppInfo(url=web_app_url))]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text("Click the button below to join the group.", reply_markup=reply_markup)
        else:
            await update.message.reply_text("Sorry, this link is invalid or has expired.")
    else:
        await update.message.reply_text(
            "Welcome! Use /protect <group_link> to create a protected link."
        )

async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generates a protected link for a given group link."""
    if not context.args or not context.args[0].startswith("https://t.me/"):
        await update.message.reply_text("Usage: `/protect https://t.me/yourgroupname`", parse_mode="Markdown")
        return

    group_link = context.args[0]
    unique_id = str(uuid.uuid4())
    encoded_id = base64.urlsafe_b64encode(unique_id.encode()).decode().rstrip("=")

    with get_db_connection() as conn:
        conn.execute("INSERT INTO protected_links (id, group_link) VALUES (?, ?)", (encoded_id, group_link))
        conn.commit()

    bot_username = (await context.bot.get_me()).username
    protected_link = f"https://t.me/{bot_username}?start={encoded_id}"
    
    await update.message.reply_text(f"✅ **Protected Link Generated!**\n\n`{protected_link}`", parse_mode="Markdown")

# Register handlers with the PTB application
telegram_bot_app.add_handler(CommandHandler("start", start))
telegram_bot_app.add_handler(CommandHandler("protect", protect_command))


# --- FastAPI Web Server Setup ---
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    """Initializes the database and sets the Telegram webhook."""
    init_db()
    webhook_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/{os.environ.get('TELEGRAM_TOKEN')}"
    await telegram_bot_app.bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook set to {webhook_url}")

@app.post("/{token}")
async def telegram_webhook(request: Request, token: str):
    """Receives updates from Telegram and passes them to the PTB application."""
    if token != os.environ.get("TELEGRAM_TOKEN"):
        return Response(status_code=403)
    
    update_data = await request.json()
    update = Update.de_json(update_data, telegram_bot_app.bot)
    await telegram_bot_app.process_update(update)
    
    return Response(status_code=200)

@app.get("/join")
async def join_page(request: Request, token: str):
    """Serves the HTML for the Web App."""
    templates = Jinja2Templates(directory="templates")
    return templates.TemplateResponse("join.html", {"request": request, "token": token})

@app.get("/getgrouplink/{token}")
async def get_group_link(token: str):
    """API endpoint for the Web App to fetch the real group link."""
    with get_db_connection() as conn:
        link_data = conn.execute("SELECT group_link FROM protected_links WHERE id = ?", (token,)).fetchone()
    
    if link_data:
        return {"url": link_data["group_link"]}
    else:
        return {"error": "Link not found"}, 404
