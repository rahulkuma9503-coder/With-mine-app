import os
import logging
import uuid
import base64
from typing import Optional
from pymongo import MongoClient
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.templating import Jinja2Templates

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Message, ChatMember
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ChatAction, ParseMode

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
users_collection = db["users"]  # Store user data for broadcasting
broadcast_collection = db["broadcast_history"]  # Store broadcast history

def init_db():
    """Verifies the MongoDB connection and creates indexes."""
    try:
        client.admin.command('ismaster')
        logger.info("MongoDB connection successful.")
        
        # Create indexes for better performance
        users_collection.create_index("user_id", unique=True)
        links_collection.create_index("created_at", expireAfterSeconds=86400)  # Auto expire after 24h
        logger.info("Database indexes created.")
    except Exception as e:
        logger.error(f"MongoDB connection failed: {e}")
        raise

async def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is member of the support channel."""
    support_channel = os.environ.get("SUPPORT_CHANNEL", "").replace("@", "")
    if not support_channel:
        return True  # Skip check if channel not configured
    
    try:
        chat_member = await context.bot.get_chat_member(f"@{support_channel}", user_id)
        return chat_member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]
    except Exception as e:
        logger.error(f"Error checking channel membership: {e}")
        return False

async def require_channel_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check and enforce channel membership with a button."""
    user_id = update.effective_user.id
    
    # Store user in database for broadcasting
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {
            "username": update.effective_user.username,
            "first_name": update.effective_user.first_name,
            "last_name": update.effective_user.last_name,
            "last_active": update.message.date if update.message else None
        }},
        upsert=True
    )
    
    # Check if user is in channel
    if await check_channel_membership(user_id, context):
        return True
    
    # User not in channel, show join button
    support_channel = os.environ.get("SUPPORT_CHANNEL", "").replace("@", "")
    if support_channel:
        keyboard = [
            [InlineKeyboardButton("📢 Join Our Channel", url=f"https://t.me/{support_channel}")],
            [InlineKeyboardButton("✅ I've Joined", callback_data="check_join")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "👋 Welcome! Before using this bot, please join our official channel to stay updated.\n\n"
            "After joining, click 'I've Joined' below.",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text("Welcome to the bot!")
    
    return False

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button callbacks."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "check_join":
        if await check_channel_membership(query.from_user.id, context):
            await query.message.edit_text(
                "✅ Thank you for joining! You can now use all bot features.\n\n"
                "Use /help to see available commands."
            )
        else:
            await query.answer("❌ You haven't joined the channel yet!", show_alert=True)

# --- Telegram Bot Logic ---
# Create the PTB application object
telegram_bot_app = Application.builder().token(os.environ.get("TELEGRAM_TOKEN")).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    # Check channel membership first
    if not await require_channel_membership(update, context):
        return
    
    if not context.args:
        # Welcome message with channel button
        support_channel = os.environ.get("SUPPORT_CHANNEL", "")
        if support_channel:
            keyboard = [
                [InlineKeyboardButton("📢 Join Our Channel", url=f"https://t.me/{support_channel.replace('@', '')}")]
            ]
            if os.environ.get("BROADCAST_CHANNEL"):
                keyboard.append([InlineKeyboardButton("📢 Broadcast Channel", 
                          url=f"https://t.me/{os.environ.get('BROADCAST_CHANNEL').replace('@', '')}")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "👋 Welcome to Protected Link Bot!\n\n"
                "I can create protected links for your Telegram groups.\n\n"
                "📋 **Available Commands:**\n"
                "/protect <group_link> - Create a protected link\n"
                "/help - Show help message\n"
                "/stats - Bot statistics (Admin only)\n"
                "/broadcast - Broadcast message (Admin only)\n\n"
                "Join our channel for updates and announcements:",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                "Welcome! I can create protected links for your Telegram groups.\n\n"
                "Use /protect <group_link> to create one."
            )
        return

    encoded_id = context.args[0]
    
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
    # Check channel membership
    if not await require_channel_membership(update, context):
        return
    
    if not context.args or not context.args[0].startswith("https://t.me/"):
        await update.message.reply_text("Usage: `/protect https://t.me/yourgroupname`", parse_mode="Markdown")
        return

    group_link = context.args[0]
    unique_id = str(uuid.uuid4())
    encoded_id = base64.urlsafe_b64encode(unique_id.encode()).decode().rstrip("=")

    links_collection.insert_one({
        "_id": encoded_id, 
        "group_link": group_link,
        "created_by": update.effective_user.id,
        "created_at": update.message.date
    })

    bot_username = (await context.bot.get_me()).username
    protected_link = f"https://t.me/{bot_username}?start={encoded_id}"
    
    await update.message.reply_text(
        f"✅ **Protected Link Generated!**\n\n"
        f"`{protected_link}`\n\n"
        f"⚠️ This link will expire in 24 hours.",
        parse_mode="Markdown"
    )

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Broadcast message to all bot users (Admin only)."""
    # Check if user is admin
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text("❌ This command is for admins only.")
        return
    
    # Check if replying to a message
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "📢 **Broadcast Command**\n\n"
            "Reply to any message (text, photo, video, sticker, etc.) with /broadcast to send it to all users.\n\n"
            "⚠️ The message will be forwarded as-is without modifications.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Ask for confirmation
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Broadcast", callback_data="confirm_broadcast")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_broadcast")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"⚠️ **Confirm Broadcast**\n\n"
        f"This will be sent to all {users_collection.count_documents({})} users.\n"
        f"Are you sure you want to continue?",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Store the message to broadcast in context
    context.user_data['broadcast_message'] = update.message.reply_to_message

async def handle_broadcast_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle broadcast confirmation."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "confirm_broadcast":
        await query.message.edit_text("📤 Broadcasting started... This may take a while.")
        
        # Get all users
        users = list(users_collection.find({}))
        total_users = len(users)
        successful = 0
        failed = 0
        
        # Get the message to broadcast
        message_to_broadcast = context.user_data.get('broadcast_message')
        
        for user in users:
            try:
                # Forward the message as-is
                await message_to_broadcast.forward(chat_id=user['user_id'])
                successful += 1
                
                # Small delay to avoid rate limiting
                await asyncio.sleep(0.05)
                
            except Exception as e:
                logger.error(f"Failed to broadcast to user {user['user_id']}: {e}")
                failed += 1
        
        # Save broadcast to history
        broadcast_collection.insert_one({
            "admin_id": query.from_user.id,
            "date": query.message.date,
            "message_type": message_to_broadcast.content_type,
            "total_users": total_users,
            "successful": successful,
            "failed": failed
        })
        
        await query.message.edit_text(
            f"✅ **Broadcast Complete**\n\n"
            f"📊 **Statistics:**\n"
            f"• Total Users: {total_users}\n"
            f"• Successful: {successful}\n"
            f"• Failed: {failed}\n"
            f"• Success Rate: {(successful/total_users*100):.1f}%",
            parse_mode=ParseMode.MARKDOWN
        )
        
    elif query.data == "cancel_broadcast":
        await query.message.edit_text("❌ Broadcast cancelled.")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show bot statistics (Admin only)."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text("❌ This command is for admins only.")
        return
    
    total_users = users_collection.count_documents({})
    total_links = links_collection.count_documents({})
    active_links = links_collection.count_documents({"created_at": {"$gte": update.message.date - datetime.timedelta(hours=24)}})
    
    await update.message.reply_text(
        f"📊 **Bot Statistics**\n\n"
        f"• Total Users: {total_users}\n"
        f"• Total Links Created: {total_links}\n"
        f"• Active Links (24h): {active_links}\n"
        f"• Database: MongoDB\n"
        f"• Uptime: Online",
        parse_mode=ParseMode.MARKDOWN
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help message."""
    # Check channel membership
    if not await require_channel_membership(update, context):
        return
    
    support_channel = os.environ.get("SUPPORT_CHANNEL", "")
    keyboard = []
    
    if support_channel:
        keyboard.append([InlineKeyboardButton("📢 Join Our Channel", url=f"https://t.me/{support_channel.replace('@', '')}")])
    
    if keyboard:
        reply_markup = InlineKeyboardMarkup(keyboard)
    else:
        reply_markup = None
    
    help_text = (
        "🤖 **Protected Link Bot Help**\n\n"
        "📋 **Available Commands:**\n"
        "/start - Start the bot\n"
        "/protect <link> - Create a protected link\n"
        "/help - Show this message\n\n"
        "🔗 **How to use:**\n"
        "1. Use /protect with your Telegram group link\n"
        "2. Share the generated protected link\n"
        "3. Users click the link and go through verification\n"
        "4. They join your group via Web App\n\n"
        "⚙️ **Features:**\n"
        "• Link protection with verification\n"
        "• 24-hour link expiration\n"
        "• Anti-spam protection\n"
        "• Channel membership requirement\n\n"
        "For support, join our channel:"
    )
    
    await update.message.reply_text(
        help_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def store_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Store user messages for statistics."""
    if update.message and update.message.chat.type == "private":
        users_collection.update_one(
            {"user_id": update.effective_user.id},
            {"$set": {"last_active": update.message.date}},
            upsert=True
        )

# Register handlers with the PTB application
telegram_bot_app.add_handler(CommandHandler("start", start))
telegram_bot_app.add_handler(CommandHandler("protect", protect_command))
telegram_bot_app.add_handler(CommandHandler("broadcast", broadcast_command))
telegram_bot_app.add_handler(CommandHandler("stats", stats_command))
telegram_bot_app.add_handler(CommandHandler("help", help_command))
telegram_bot_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, store_message))

# Add callback query handler for buttons
from telegram.ext import CallbackQueryHandler
telegram_bot_app.add_handler(CallbackQueryHandler(button_callback, pattern="^check_join$"))
telegram_bot_app.add_handler(CallbackQueryHandler(handle_broadcast_confirmation, pattern="^(confirm_broadcast|cancel_broadcast)$"))

# --- FastAPI Web Server Setup ---
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    """Initializes the database, starts the PTB app, and sets the Telegram webhook."""
    logger.info("Application startup...")
    
    # Check for critical environment variables
    required_vars = ["TELEGRAM_TOKEN", "RENDER_EXTERNAL_URL"]
    for var in required_vars:
        if not os.environ.get(var):
            logger.critical(f"{var} is not set. Exiting.")
            raise Exception(f"{var} environment variable not set!")
    
    # Initialize database connection
    init_db()
    
    # --- CORRECTED PTB LIFECYCLE MANAGEMENT ---
    # Initialize and start the PTB application
    await telegram_bot_app.initialize()
    await telegram_bot_app.start()
    
    # Set the webhook
    webhook_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/{os.environ.get('TELEGRAM_TOKEN')}"
    await telegram_bot_app.bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook set to {webhook_url}")
    
    # Log bot info
    bot_info = await telegram_bot_app.bot.get_me()
    logger.info(f"Bot started: @{bot_info.username}")
    logger.info("Application startup complete.")

@app.on_event("shutdown")
async def on_shutdown():
    """Stops the PTB application and closes the database connection."""
    logger.info("Application shutdown...")
    # --- CORRECTED PTB LIFECYCLE MANAGEMENT ---
    # Stop and shutdown the PTB application
    await telegram_bot_app.stop()
    await telegram_bot_app.shutdown()
    # Close the MongoDB connection
    client.close()
    logger.info("Application shutdown complete.")

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

@app.get("/")
async def root():
    """Root endpoint for health check."""
    return {"status": "ok", "service": "Protected Link Bot"}