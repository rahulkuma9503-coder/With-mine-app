import os
import logging
import uuid
import base64
import asyncio
import datetime
from typing import Optional
from pymongo import MongoClient
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.templating import Jinja2Templates

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, ChatMember
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Database Setup (MongoDB) ---
MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    raise Exception("MONGODB_URI environment variable not set!")

# Initialize MongoDB client and select database/collection
client = MongoClient(MONGODB_URI)
db_name = "protected_bot_db"
db = client[db_name]
links_collection = db["protected_links"]
users_collection = db["users"]
broadcast_collection = db["broadcast_history"]

def init_db():
    """Verifies the MongoDB connection."""
    try:
        client.admin.command('ismaster')
        logger.info("✅ MongoDB connected")
        
        # Create indexes
        users_collection.create_index("user_id", unique=True)
        links_collection.create_index("created_by")
        links_collection.create_index("active")
        logger.info("✅ Database indexes created")
    except Exception as e:
        logger.error(f"❌ MongoDB error: {e}")
        raise

async def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is member of the support channel."""
    support_channel = os.environ.get("SUPPORT_CHANNEL", "").strip()
    if not support_channel:
        return True
    
    try:
        try:
            chat_id = int(support_channel)
        except ValueError:
            if support_channel.startswith('@'):
                chat_id = support_channel
            else:
                chat_id = f"@{support_channel}"
        
        chat_member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return chat_member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]
    except Exception as e:
        logger.error(f"❌ Channel check error: {e}")
        return False

async def require_channel_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check and enforce channel membership."""
    user_id = update.effective_user.id
    
    # Store user
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {
            "username": update.effective_user.username,
            "first_name": update.effective_user.first_name,
            "last_active": datetime.datetime.now()
        }},
        upsert=True
    )
    
    # Check membership
    if await check_channel_membership(user_id, context):
        return True
    
    # Not in channel
    support_channel = os.environ.get("SUPPORT_CHANNEL", "").strip()
    if support_channel:
        # Create simple invite link
        if support_channel.startswith('-100'):
            invite_link = f"https://t.me/c/{support_channel[4:]}"
        elif support_channel.startswith('@'):
            invite_link = f"https://t.me/{support_channel[1:]}"
        else:
            invite_link = f"https://t.me/{support_channel}"
        
        keyboard = [
            [InlineKeyboardButton("📢 Join Channel", url=invite_link)],
            [InlineKeyboardButton("✅ Check", callback_data="check_join")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        # Get user's first name for personalized message
        user_name = update.effective_user.first_name or "there"
        await update.message.reply_text(
            f"Hi {user_name}! 👋\n\n"
            "Join our channel first to use this bot.\n"
            "Then click 'Check' below.",
            reply_markup=reply_markup
        )
    else:
        return True
    
    return False

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button callbacks."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "check_join":
        if await check_channel_membership(query.from_user.id, context):
            user_name = query.from_user.first_name or "User"
            await query.message.edit_text(
                f"Welcome {user_name}! 👋\n"
                "✅ Verified!\n\n"
                "You can now use the bot.\n"
                "Use /help for commands."
            )
        else:
            await query.answer("❌ Not joined yet. Please join first.", show_alert=True)
    
    elif query.data == "confirm_broadcast":
        await handle_broadcast_confirmation(update, context)
    
    elif query.data == "cancel_broadcast":
        await query.message.edit_text("❌ Broadcast cancelled")
    
    elif query.data.startswith("revoke_"):
        link_id = query.data.replace("revoke_", "")
        await handle_revoke_link(update, context, link_id)

# --- Telegram Bot Logic ---
telegram_bot_app = Application.builder().token(os.environ.get("TELEGRAM_TOKEN")).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    # Check channel membership
    if not await require_channel_membership(update, context):
        return
    
    if context.args:
        # Handle protected link
        encoded_id = context.args[0]
        link_data = links_collection.find_one({"_id": encoded_id, "active": True})

        if link_data:
            web_app_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/join?token={encoded_id}"
            
            keyboard = [[InlineKeyboardButton("🔗 Join Group", web_app=WebAppInfo(url=web_app_url))]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Get user's first name for personalized message
            user_name = update.effective_user.first_name or "User"
            
            await update.message.reply_text(
                f"Hi {user_name}! 👋\n\n"
                "🔐 This is a Protected Link\n\n"
                "Click the button below to proceed.",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text("❌ Link expired or revoked")
        return
    
    # Simple welcome message with username
    user_name = update.effective_user.first_name or "there"
    username = update.effective_user.username
    
    # Create personalized welcome message
    welcome_msg = f"Hi {user_name}! 👋\n\n"
    
    if username:
        welcome_msg += f"(@{username})\n\n"
    
    welcome_msg += (
        "🔐 *LinkShield Pro*\n\n"
        "Create protected Telegram group links.\n\n"
        "📋 Commands:\n"
        "• /protect <link> - Create link\n"
        "• /revoke - Remove link\n"
        "• /help - Show help\n\n"
        "Example: `/protect https://t.me/group`"
    )
    
    await update.message.reply_text(
        welcome_msg,
        parse_mode=ParseMode.MARKDOWN
    )

async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create protected link."""
    # Check channel membership
    if not await require_channel_membership(update, context):
        return
    
    if not context.args or not context.args[0].startswith("https://t.me/"):
        await update.message.reply_text(
            "Usage: `/protect https://t.me/group`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    group_link = context.args[0]
    unique_id = str(uuid.uuid4())
    encoded_id = base64.urlsafe_b64encode(unique_id.encode()).decode().rstrip("=")
    
    short_id = encoded_id[:8].upper()

    links_collection.insert_one({
        "_id": encoded_id,
        "short_id": short_id,
        "group_link": group_link,
        "created_by": update.effective_user.id,
        "created_by_name": update.effective_user.first_name,
        "created_by_username": update.effective_user.username,
        "created_at": datetime.datetime.now(),
        "active": True,
        "clicks": 0
    })

    bot_username = (await context.bot.get_me()).username
    protected_link = f"https://t.me/{bot_username}?start={encoded_id}"
    
    # Get user's name for personalized message
    user_name = update.effective_user.first_name or "User"
    
    # Simple buttons
    keyboard = [
        [
            InlineKeyboardButton("📤 Share", url=f"https://t.me/share/url?url={protected_link}"),
            InlineKeyboardButton("❌ Revoke", callback_data=f"revoke_{encoded_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"Hi {user_name}! 👋\n\n"
        f"✅ Link created!\n\n"
        f"ID: `{short_id}`\n\n"
        f"Protected link:\n"
        f"`{protected_link}`\n\n"
        f"To revoke: `/revoke {short_id}`",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revoke a link."""
    # Check channel membership
    if not await require_channel_membership(update, context):
        return
    
    if not context.args:
        # Show user's active links
        user_id = update.effective_user.id
        active_links = list(links_collection.find(
            {"created_by": user_id, "active": True},
            sort=[("created_at", -1)],
            limit=10
        ))
        
        if not active_links:
            await update.message.reply_text("📭 No active links")
            return
        
        # Get user's name
        user_name = update.effective_user.first_name or "User"
        
        message = f"Hi {user_name}! 👋\n\n🔐 Your links:\n\n"
        keyboard = []
        
        for link in active_links:
            short_id = link.get('short_id', link['_id'][:8])
            clicks = link.get('clicks', 0)
            
            message += f"• `{short_id}` - {clicks} clicks\n"
            keyboard.append([InlineKeyboardButton(
                f"❌ Revoke {short_id}",
                callback_data=f"revoke_{link['_id']}"
            )])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Revoke by ID
    link_id = context.args[0].upper()
    
    # Find link
    query = {
        "$or": [
            {"short_id": link_id},
            {"_id": link_id}
        ],
        "created_by": update.effective_user.id,
        "active": True
    }
    
    link_data = links_collection.find_one(query)
    
    if not link_data:
        await update.message.reply_text("❌ Link not found")
        return
    
    # Revoke
    links_collection.update_one(
        {"_id": link_data['_id']},
        {
            "$set": {
                "active": False,
                "revoked_at": datetime.datetime.now()
            }
        }
    )
    
    user_name = update.effective_user.first_name or "User"
    await update.message.reply_text(f"Hi {user_name}! 👋\n\n✅ Link `{link_id}` revoked")

async def handle_revoke_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link_id: str):
    """Handle revoke button."""
    query = update.callback_query
    await query.answer()
    
    link_data = links_collection.find_one({"_id": link_id, "active": True})
    
    if not link_data:
        await query.message.edit_text("❌ Link already revoked")
        return
    
    if link_data['created_by'] != query.from_user.id:
        await query.message.edit_text("❌ Not your link")
        return
    
    # Revoke
    links_collection.update_one(
        {"_id": link_id},
        {
            "$set": {
                "active": False,
                "revoked_at": datetime.datetime.now()
            }
        }
    )
    
    user_name = query.from_user.first_name or "User"
    await query.message.edit_text(f"Hi {user_name}! 👋\n\n✅ Link revoked")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin broadcast."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text("❌ Admin only")
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a message with /broadcast")
        return
    
    total_users = users_collection.count_documents({})
    keyboard = [
        [InlineKeyboardButton("✅ Send", callback_data="confirm_broadcast")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_broadcast")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"Send to {total_users} users?",
        reply_markup=reply_markup
    )
    
    context.user_data['broadcast_message'] = update.message.reply_to_message

async def handle_broadcast_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle broadcast."""
    query = update.callback_query
    await query.answer()
    
    await query.message.edit_text("📤 Sending...")
    
    users = list(users_collection.find({}))
    total_users = len(users)
    successful = 0
    failed = 0
    
    message_to_broadcast = context.user_data.get('broadcast_message')
    
    for user in users:
        try:
            await message_to_broadcast.copy(chat_id=user['user_id'])
            successful += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Failed: {user['user_id']}: {e}")
            failed += 1
    
    broadcast_collection.insert_one({
        "admin_id": query.from_user.id,
        "date": datetime.datetime.now(),
        "total_users": total_users,
        "successful": successful,
        "failed": failed
    })
    
    await query.message.edit_text(
        f"✅ Sent\n"
        f"Success: {successful}\n"
        f"Failed: {failed}"
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show stats."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text("❌ Admin only")
        return
    
    total_users = users_collection.count_documents({})
    total_links = links_collection.count_documents({})
    active_links = links_collection.count_documents({"active": True})
    
    today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    new_users_today = users_collection.count_documents({"last_active": {"$gte": today}})
    
    await update.message.reply_text(
        f"📊 Stats\n\n"
        f"Users: {total_users}\n"
        f"New today: {new_users_today}\n\n"
        f"Links: {total_links}\n"
        f"Active: {active_links}"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help."""
    if not await require_channel_membership(update, context):
        return
    
    user_name = update.effective_user.first_name or "there"
    
    await update.message.reply_text(
        f"Hi {user_name}! 👋\n\n"
        "📋 *Commands*\n\n"
        "/protect <link> - Create protected link\n"
        "/revoke - Revoke a link\n"
        "/help - This message\n\n"
        "*How to:*\n"
        "1. Use /protect with group link\n"
        "2. Share the generated link\n"
        "3. Use /revoke to remove access\n\n"
        "Example: `/protect https://t.me/group`",
        parse_mode=ParseMode.MARKDOWN
    )

async def store_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Store user activity."""
    if update.message and update.message.chat.type == "private":
        users_collection.update_one(
            {"user_id": update.effective_user.id},
            {"$set": {"last_active": update.message.date}},
            upsert=True
        )

# Register handlers
telegram_bot_app.add_handler(CommandHandler("start", start))
telegram_bot_app.add_handler(CommandHandler("protect", protect_command))
telegram_bot_app.add_handler(CommandHandler("revoke", revoke_command))
telegram_bot_app.add_handler(CommandHandler("broadcast", broadcast_command))
telegram_bot_app.add_handler(CommandHandler("stats", stats_command))
telegram_bot_app.add_handler(CommandHandler("help", help_command))
telegram_bot_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, store_message))

# Add callback handler
from telegram.ext import CallbackQueryHandler
telegram_bot_app.add_handler(CallbackQueryHandler(button_callback))

# --- FastAPI Setup ---
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    """Start bot."""
    logger.info("Starting bot...")
    
    required_vars = ["TELEGRAM_TOKEN", "RENDER_EXTERNAL_URL"]
    for var in required_vars:
        if not os.environ.get(var):
            logger.critical(f"Missing {var}")
            raise Exception(f"Missing {var}")
    
    init_db()
    
    await telegram_bot_app.initialize()
    await telegram_bot_app.start()
    
    webhook_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/{os.environ.get('TELEGRAM_TOKEN')}"
    await telegram_bot_app.bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook: {webhook_url}")
    
    bot_info = await telegram_bot_app.bot.get_me()
    logger.info(f"Bot: @{bot_info.username}")

@app.on_event("shutdown")
async def on_shutdown():
    """Stop bot."""
    logger.info("Stopping bot...")
    await telegram_bot_app.stop()
    await telegram_bot_app.shutdown()
    client.close()
    logger.info("Bot stopped")

@app.post("/{token}")
async def telegram_webhook(request: Request, token: str):
    """Telegram webhook."""
    if token != os.environ.get("TELEGRAM_TOKEN"):
        raise HTTPException(status_code=403, detail="Invalid token")
    
    update_data = await request.json()
    update = Update.de_json(update_data, telegram_bot_app.bot)
    await telegram_bot_app.process_update(update)
    
    return Response(status_code=200)

@app.get("/join")
async def join_page(request: Request, token: str):
    """Web app page with updated message."""
    templates = Jinja2Templates(directory="templates")
    
    # Get link data to show creator info
    link_data = links_collection.find_one({"_id": token})
    context = {
        "request": request, 
        "token": token,
        "link_data": link_data
    }
    
    return templates.TemplateResponse("join.html", context)

@app.get("/getgrouplink/{token}")
async def get_group_link(token: str):
    """Get real group link."""
    link_data = links_collection.find_one({"_id": token, "active": True})
    
    if link_data:
        links_collection.update_one(
            {"_id": token},
            {"$inc": {"clicks": 1}}
        )
        return {"url": link_data["group_link"]}
    else:
        raise HTTPException(status_code=404, detail="Link not found")

@app.get("/")
async def root():
    """Health check."""
    return {
        "status": "ok",
        "service": "LinkShield",
        "time": datetime.datetime.now().isoformat()
    }