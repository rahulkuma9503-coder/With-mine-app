import os
import logging
import uuid
import base64
from pymongo import MongoClient
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.templating import Jinja2Templates

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Database Setup (MongoDB) ---
MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    raise Exception("MONGODB_URI environment variable not set!")

# ...
# Initialize MongoDB client and select database/collection
client = MongoClient(MONGODB_URI)

# FIX: Use a hardcoded database name instead of parsing it from the URI.
# The connection string is for connecting to the server, and the app
# should define its own database name.
db_name = "protected_bot_db" 
db = client[db_name]
links_collection = db["protected_links"]
# ...

def init_db():
    """In MongoDB, collections are created lazily on first insert.
    This function can be used to verify the connection."""
    try:
        # The ismaster command is cheap and does not require auth.
        client.admin.command('ismaster')
        logger.info("MongoDB connection successful.")
    except Exception as e:
        logger.error(f"MongoDB connection failed: {e}")
        raise

# --- Telegram Bot Logic ---
telegram_bot_app = Application.builder().token(os.environ.get("TELEGRAM_TOKEN")).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    if not context.args:
        await update.message.reply_text(
            "Welcome! I can create protected links for your Telegram groups.\n\n"
            "Use /protect <group_link> to create one."
        )
        return

    encoded_id = context.args[0]
    
    # Find the link in MongoDB
    link_data = links_collection.find_one({"_id": encoded_id})

    if link_data:
        group_link = link_data["group_link"]
        web_app_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/join?token={encoded_id}"
        
        keyboard = [[InlineKeyboardButton("Join Group", web_app=WebAppInfo(url=web_app_url))]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text("Click the button below to join the group.", reply_markup=reply_markup)
    else:
        await update.message.reply_text("Sorry, this link is invalid or has expired.")

async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generates a protected link for a given group link."""
    if not context.args or not context.args[0].startswith("https://t.me/"):
        await update.message.reply_text("Usage: `/protect https://t.me/yourgroupname`", parse_mode="Markdown")
        return

    group_link = context.args[0]
    unique_id = str(uuid.uuid4())
    encoded_id = base64.urlsafe_b64encode(unique_id.encode()).decode().rstrip("=")

    # Insert the new link into MongoDB
    links_collection.insert_one({"_id": encoded_id, "group_link": group_link})

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
    if not os.environ.get("TELEGRAM_TOKEN") or not os.environ.get("RENDER_EXTERNAL_URL"):
        logger.critical("TELEGRAM_TOKEN or RENDER_EXTERNAL_URL is not set. Exiting.")
        return

    init_db()
    webhook_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/{os.environ.get('TELEGRAM_TOKEN')}"
    await telegram_bot_app.bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook set to {webhook_url}")

@app.post("/{token}")
async def telegram_webhook(request: Request, token: str):
    """Receives updates from Telegram and passes them to the PTB application."""
    if token != os.environ.get("TELEGRAM_TOKEN"):
        raise HTTPException(status_code=403, detail="Invalid token")
    
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
    link_data = links_collection.find_one({"_id": token})
    
    if link_data:
        return {"url": link_data["group_link"]}
    else:
        raise HTTPException(status_code=404, detail="Link not found")
        
