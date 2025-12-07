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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Message, ChatMember, ChatInviteLink
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
from telegram.error import BadRequest

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
        logger.info("✅ MongoDB connection successful.")
        
        # Create indexes
        users_collection.create_index("user_id", unique=True)
        links_collection.create_index("created_by")
        links_collection.create_index("active")
        links_collection.create_index("created_at")
        logger.info("✅ Database indexes created.")
    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        raise

async def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is member of the support channel using channel ID."""
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
        logger.error(f"❌ Error checking channel membership: {e}")
        return False

async def require_channel_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check and enforce channel membership."""
    user_id = update.effective_user.id
    
    # Store user in database
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {
            "username": update.effective_user.username,
            "first_name": update.effective_user.first_name,
            "last_name": update.effective_user.last_name,
            "last_active": update.message.date if update.message else datetime.datetime.now()
        }},
        upsert=True
    )
    
    # Check if user is in channel
    if await check_channel_membership(user_id, context):
        return True
    
    # User not in channel, show join button
    support_channel = os.environ.get("SUPPORT_CHANNEL", "").strip()
    if support_channel:
        try:
            try:
                chat_id = int(support_channel)
            except ValueError:
                chat_id = support_channel
            
            # Try to create invite link
            invite_link_obj = await context.bot.create_chat_invite_link(
                chat_id=chat_id,
                creates_join_request=True,
                name="Bot Access",
                expire_date=None
            )
            invite_link = invite_link_obj.invite_link
        except:
            # Fallback to t.me link
            if support_channel.startswith('@'):
                invite_link = f"https://t.me/{support_channel[1:]}"
            elif support_channel.startswith('-100'):
                invite_link = f"https://t.me/c/{support_channel[4:]}"
            else:
                invite_link = f"https://t.me/{support_channel}"
        
        keyboard = [
            [InlineKeyboardButton("📢 Join Our Channel", url=invite_link)],
            [InlineKeyboardButton("✅ I've Joined", callback_data="check_join")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "👋 *Welcome!*\n\n"
            "Please join our official channel first to use this bot.\n"
            "After joining, click 'I've Joined' below.",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        return True  # No channel requirement
    
    return False

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button callbacks."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "check_join":
        if await check_channel_membership(query.from_user.id, context):
            await query.message.edit_text(
                "✅ *Verification Successful!*\n\n"
                "You can now use all bot features.\n"
                "Use /help to see available commands.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.answer("❌ Please join the channel first!", show_alert=True)
    
    elif query.data == "confirm_broadcast":
        await handle_broadcast_confirmation(update, context)
    
    elif query.data == "cancel_broadcast":
        await query.message.edit_text("❌ Broadcast cancelled.")
    
    elif query.data.startswith("revoke_"):
        link_id = query.data.replace("revoke_", "")
        await handle_revoke_link(update, context, link_id)

# --- Telegram Bot Logic ---
telegram_bot_app = Application.builder().token(os.environ.get("TELEGRAM_TOKEN")).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    # Check channel membership first
    if not await require_channel_membership(update, context):
        return
    
    if context.args:
        # Handle protected link
        encoded_id = context.args[0]
        link_data = links_collection.find_one({"_id": encoded_id, "active": True})

        if link_data:
            group_link = link_data["group_link"]
            web_app_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/join?token={encoded_id}"
            
            keyboard = [[InlineKeyboardButton("🔗 Join Group", web_app=WebAppInfo(url=web_app_url))]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "🛡️ *Protected Link Access*\n\n"
                "Click the button below to join the group.",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                "❌ *Invalid Link*\n\n"
                "This link has expired or been revoked.",
                parse_mode=ParseMode.MARKDOWN
            )
        return
    
    # No args - show welcome message
    support_channel = os.environ.get("SUPPORT_CHANNEL", "")
    
    if support_channel:
        try:
            if support_channel.startswith('-100'):
                invite_link = f"https://t.me/c/{support_channel[4:]}"
            elif support_channel.startswith('@'):
                invite_link = f"https://t.me/{support_channel[1:]}"
            else:
                invite_link = f"https://t.me/{support_channel}"
        except:
            invite_link = ""
        
        if invite_link:
            keyboard = [[InlineKeyboardButton("🌟 Join Channel", url=invite_link)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
        else:
            reply_markup = None
    else:
        reply_markup = None
    
    await update.message.reply_text(
        "🌟 *Welcome to LinkShield Pro!*\n\n"
        "I create secure, protected links for your Telegram groups.\n\n"
        "📋 *Available Commands:*\n"
        "• /protect <link> - Create protected link\n"
        "• /revoke <id> - Revoke a link\n"
        "• /help - Show help\n\n"
        "💡 *Example:*\n"
        "`/protect https://t.me/yourgroup`",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generates a protected link for a given group link."""
    # Check channel membership
    if not await require_channel_membership(update, context):
        return
    
    if not context.args or not context.args[0].startswith("https://t.me/"):
        await update.message.reply_text(
            "📝 *Usage:*\n`/protect https://t.me/yourgroup`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    group_link = context.args[0]
    unique_id = str(uuid.uuid4())
    encoded_id = base64.urlsafe_b64encode(unique_id.encode()).decode().rstrip("=")
    
    # Generate short ID
    short_id = encoded_id[:8]

    links_collection.insert_one({
        "_id": encoded_id,
        "short_id": short_id,
        "group_link": group_link,
        "created_by": update.effective_user.id,
        "created_by_name": update.effective_user.first_name,
        "created_at": datetime.datetime.now(),
        "active": True,
        "clicks": 0
    })

    bot_username = (await context.bot.get_me()).username
    protected_link = f"https://t.me/{bot_username}?start={encoded_id}"
    
    await update.message.reply_text(
        f"✅ *Protected Link Created!*\n\n"
        f"🔗 *Link ID:* `{short_id}`\n"
        f"📊 *Status:* 🟢 Active\n\n"
        f"🔒 *Protected Link:*\n"
        f"`{protected_link}`\n\n"
        f"📋 *To revoke:* `/revoke {short_id}`",
        parse_mode=ParseMode.MARKDOWN
    )

async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revoke a protected link."""
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
            await update.message.reply_text(
                "📭 *No Active Links*\n\n"
                "You don't have any active links to revoke.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        message = "🔐 *Your Active Links:*\n\n"
        keyboard = []
        
        for link in active_links:
            short_id = link.get('short_id', link['_id'][:8])
            clicks = link.get('clicks', 0)
            created = link.get('created_at', datetime.datetime.now()).strftime('%m/%d')
            
            message += f"• `{short_id}` - {clicks} clicks - {created}\n"
            keyboard.append([InlineKeyboardButton(
                f"❌ Revoke {short_id}",
                callback_data=f"revoke_{link['_id']}"
            )])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message += "\nClick a button below to revoke."
        
        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Try to revoke by short ID
    link_id = context.args[0]
    
    # Search by short_id or _id
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
        await update.message.reply_text(
            "❌ *Link Not Found*\n\n"
            "Either the link doesn't exist or you don't own it.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Update the link
    result = links_collection.update_one(
        {"_id": link_data['_id']},
        {
            "$set": {
                "active": False,
                "revoked_at": datetime.datetime.now(),
                "revoked_by": update.effective_user.id
            }
        }
    )
    
    if result.modified_count > 0:
        await update.message.reply_text(
            f"✅ *Link Revoked!*\n\n"
            f"Link `{link_data.get('short_id', link_id)}` has been permanently revoked.\n\n"
            f"⚠️ Users can no longer access this link.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            "❌ *Failed to revoke link*",
            parse_mode=ParseMode.MARKDOWN
        )

async def handle_revoke_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link_id: str):
    """Handle revoke button callback."""
    query = update.callback_query
    await query.answer()
    
    # Find and revoke the link
    link_data = links_collection.find_one({"_id": link_id, "active": True})
    
    if not link_data:
        await query.message.edit_text(
            "❌ Link not found or already revoked.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if link_data['created_by'] != query.from_user.id:
        await query.message.edit_text(
            "❌ You don't have permission to revoke this link.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Revoke the link
    links_collection.update_one(
        {"_id": link_id},
        {
            "$set": {
                "active": False,
                "revoked_at": datetime.datetime.now(),
                "revoked_by": query.from_user.id
            }
        }
    )
    
    await query.message.edit_text(
        f"✅ *Link Revoked!*\n\n"
        f"Link `{link_data.get('short_id', link_id[:8])}` has been revoked.\n"
        f"Users can no longer access this link.",
        parse_mode=ParseMode.MARKDOWN
    )

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Broadcast message to all bot users (Admin only)."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "❌ Admin only command.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "📢 *Broadcast Command*\n\n"
            "Reply to a message with /broadcast to send it to all users.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    total_users = users_collection.count_documents({})
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Broadcast", callback_data="confirm_broadcast")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_broadcast")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"⚠️ *Confirm Broadcast*\n\n"
        f"This will be sent to {total_users} users.\n"
        f"Are you sure?",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    
    context.user_data['broadcast_message'] = update.message.reply_to_message

async def handle_broadcast_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle broadcast confirmation."""
    query = update.callback_query
    await query.answer()
    
    await query.message.edit_text("📤 Broadcasting...")
    
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
            logger.error(f"Failed to broadcast to user {user['user_id']}: {e}")
            failed += 1
    
    # Save broadcast history
    broadcast_collection.insert_one({
        "admin_id": query.from_user.id,
        "date": datetime.datetime.now(),
        "total_users": total_users,
        "successful": successful,
        "failed": failed
    })
    
    await query.message.edit_text(
        f"✅ *Broadcast Complete!*\n\n"
        f"📊 *Stats:*\n"
        f"• Total: {total_users}\n"
        f"• ✅ Success: {successful}\n"
        f"• ❌ Failed: {failed}",
        parse_mode=ParseMode.MARKDOWN
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show bot statistics (Admin only)."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text("❌ Admin only command.")
        return
    
    total_users = users_collection.count_documents({})
    total_links = links_collection.count_documents({})
    active_links = links_collection.count_documents({"active": True})
    
    today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    new_users_today = users_collection.count_documents({"last_active": {"$gte": today}})
    new_links_today = links_collection.count_documents({"created_at": {"$gte": today}})
    
    await update.message.reply_text(
        f"📊 *Bot Statistics*\n\n"
        f"👥 *Users:* {total_users}\n"
        f"📈 *Today:* {new_users_today}\n\n"
        f"🔗 *Links:* {total_links}\n"
        f"🟢 *Active:* {active_links}\n"
        f"📈 *Today:* {new_links_today}",
        parse_mode=ParseMode.MARKDOWN
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help message."""
    if not await require_channel_membership(update, context):
        return
    
    await update.message.reply_text(
        "🛡️ *LinkShield Pro Help*\n\n"
        "📋 *Commands:*\n"
        "• /start - Start the bot\n"
        "• /protect <link> - Create protected link\n"
        "• /revoke - Revoke a link\n"
        "• /help - This message\n\n"
        "🔒 *How to use:*\n"
        "1. Use `/protect https://t.me/yourgroup`\n"
        "2. Share the generated link\n"
        "3. Users can join through verification\n"
        "4. Use `/revoke` to remove access\n\n"
        "💡 *Note:* Links never expire automatically.",
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

# Register handlers
telegram_bot_app.add_handler(CommandHandler("start", start))
telegram_bot_app.add_handler(CommandHandler("protect", protect_command))
telegram_bot_app.add_handler(CommandHandler("revoke", revoke_command))
telegram_bot_app.add_handler(CommandHandler("broadcast", broadcast_command))
telegram_bot_app.add_handler(CommandHandler("stats", stats_command))
telegram_bot_app.add_handler(CommandHandler("help", help_command))
telegram_bot_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, store_message))

# Add callback query handler for buttons
from telegram.ext import CallbackQueryHandler
telegram_bot_app.add_handler(CallbackQueryHandler(button_callback))

# --- FastAPI Web Server Setup ---
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    """Initializes the database, starts the PTB app, and sets the Telegram webhook."""
    logger.info("🌟 Starting bot...")
    
    required_vars = ["TELEGRAM_TOKEN", "RENDER_EXTERNAL_URL"]
    for var in required_vars:
        if not os.environ.get(var):
            logger.critical(f"❌ {var} is not set.")
            raise Exception(f"{var} environment variable not set!")
    
    init_db()
    
    await telegram_bot_app.initialize()
    await telegram_bot_app.start()
    
    webhook_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/{os.environ.get('TELEGRAM_TOKEN')}"
    await telegram_bot_app.bot.set_webhook(url=webhook_url)
    logger.info(f"✅ Webhook set to {webhook_url}")
    
    bot_info = await telegram_bot_app.bot.get_me()
    logger.info(f"🤖 Bot: @{bot_info.username}")
    logger.info("🚀 Bot started successfully.")

@app.on_event("shutdown")
async def on_shutdown():
    """Stops the PTB application and closes the database connection."""
    logger.info("🛑 Stopping bot...")
    await telegram_bot_app.stop()
    await telegram_bot_app.shutdown()
    client.close()
    logger.info("✅ Bot stopped.")

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
    link_data = links_collection.find_one({"_id": token, "active": True})
    
    if link_data:
        # Increment click counter
        links_collection.update_one(
            {"_id": token},
            {"$inc": {"clicks": 1}}
        )
        return {"url": link_data["group_link"]}
    else:
        raise HTTPException(status_code=404, detail="Link not found or revoked")

@app.get("/")
async def root():
    """Root endpoint for health check."""
    return {
        "status": "ok",
        "service": "LinkShield Pro",
        "time": datetime.datetime.now().isoformat()
    }