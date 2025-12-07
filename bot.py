import os
import logging
import sqlite3
import uuid
import base64
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database setup
DB_NAME = "links.db"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db_connection() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS protected_links (
                id TEXT PRIMARY KEY,
                group_link TEXT NOT NULL
            )"""
        )
        conn.commit()

# Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command, both general and with a payload."""
    if context.args:
        # This is a user clicking a protected link
        encoded_id = context.args[0]
        
        with get_db_connection() as conn:
            link_data = conn.execute("SELECT group_link FROM protected_links WHERE id = ?", (encoded_id,)).fetchone()

        if link_data:
            group_link = link_data["group_link"]
            
            # URL for our mini app, passing the token as a query parameter
            # Replace 'your-app-name.onrender.com' with your actual Render app URL
            web_app_url = f"https://your-app-name.onrender.com/join?token={encoded_id}"
            
            keyboard = [
                [InlineKeyboardButton("Join Group", web_app=WebAppInfo(url=web_app_url))]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "Click the button below to join the group.",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text("Sorry, this link is invalid or has expired.")
    else:
        # A regular /start command
        await update.message.reply_text(
            "Welcome! I am a bot that creates protected links for Telegram groups.\n\n"
            "Use /protect <group_link> to create one."
        )

async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generates a protected link for a given group link."""
    if not context.args or not context.args[0].startswith("https://t.me/"):
        await update.message.reply_text(
            "Please provide a valid group link.\nUsage: `/protect https://t.me/yourgroupname`"
        )
        return

    group_link = context.args[0]
    
    # Generate a unique ID and encode it for URL safety
    unique_id = str(uuid.uuid4())
    encoded_id = base64.urlsafe_b64encode(unique_id.encode()).decode().rstrip("=")

    # Store in the database
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO protected_links (id, group_link) VALUES (?, ?)",
            (encoded_id, group_link)
        )
        conn.commit()

    bot_username = (await context.bot.get_me()).username
    protected_link = f"https://t.me/{bot_username}?start={encoded_id}"
    
    await update.message.reply_text(
        f"✅ **Protected Link Generated!**\n\nShare this link:\n`{protected_link}`",
        parse_mode="Markdown"
    )

def main() -> None:
    """Start the bot."""
    init_db()
    
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        logger.error("TELEGRAM_TOKEN environment variable not set!")
        return

    # Create the Application and pass it your bot's token.
    application = Application.builder().token(token).build()

    # on different commands - answer in Telegram
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("protect", protect_command))

    # Run the bot until the user presses Ctrl-C
    # We will start the bot via webhook in the start.sh script
    application.run_webhook(
        listen="0.0.0.0",
        port=int(os.environ.get("PORT", 8443)),
        url_path=token,
        webhook_url=f"https://your-app-name.onrender.com/{token}"
    )

if __name__ == "__main__":
    main()
