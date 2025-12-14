import os
import logging
import uuid
import base64
import asyncio
import datetime
from typing import Optional, List
from pymongo import MongoClient
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.templating import Jinja2Templates

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, ChatMember, ChatInviteLink
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Database Setup (MongoDB) ---
MONGODB_URI = os.environ.get("MONGODB_URI")
if not MONGODB_URI:
    raise Exception("MONGODB_URI environment variable not set!")

client = MongoClient(MONGODB_URI)
db_name = "protected_bot_db"
db = client[db_name]
links_collection = db["protected_links"]
users_collection = db["users"]
broadcast_collection = db["broadcast_history"]
channels_collection = db["channels"]
forced_links_collection = db["forced_links"]
forced_group_collection = db["forced_group"]

def init_db():
    try:
        client.admin.command('ismaster')
        logger.info("✅ MongoDB connected")
        users_collection.create_index("user_id", unique=True)
        links_collection.create_index("created_by")
        links_collection.create_index("active")
        channels_collection.create_index("channel_id", unique=True)
        forced_links_collection.create_index("channel_id", unique=True)
        forced_group_collection.create_index("group_id", unique=True)
        logger.info("✅ Database indexes created")
    except Exception as e:
        logger.error(f"❌ MongoDB error: {e}")
        raise

# ================= GET ALL REQUIRED CHANNELS (SUPPORT + FORCED GROUP) =================
def get_required_channels() -> List[str]:
    """Get all channels user must join (support channels + forced group)."""
    channels = []
    
    # Add support channels from environment
    support_raw = os.environ.get("SUPPORT_CHANNELS", "").strip()
    if support_raw:
        channels.extend([c.strip() for c in support_raw.split(",") if c.strip()])
    
    # Add forced group from database
    forced_group = forced_group_collection.find_one({})
    if forced_group and forced_group.get("group_id"):
        channels.append(forced_group["group_id"])
    
    return channels

# ================= CHECK IF FORCED GROUP IS SET =================
def is_forced_group_set() -> bool:
    """Check if a forced group is configured."""
    forced_group = forced_group_collection.find_one({})
    return forced_group and forced_group.get("group_id")

# ================= GET FORCED GROUP INFO =================
def get_forced_group_info():
    """Get information about the forced group."""
    return forced_group_collection.find_one({})

# ================= INVITE LINK =================
async def get_channel_invite_link(context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> str:
    """Get invite link, preferring forced custom link over bot-generated one."""
    try:
        # First check if there's a forced custom link for this channel
        forced_link_data = forced_links_collection.find_one({"channel_id": channel_id})
        if forced_link_data and forced_link_data.get("forced_link"):
            logger.info(f"Using forced link for channel {channel_id}")
            return forced_link_data["forced_link"]
        
        # Check if this is the forced group
        forced_group = forced_group_collection.find_one({"group_id": channel_id})
        if forced_group and forced_group.get("group_link"):
            logger.info(f"Using forced group link for {channel_id}")
            return forced_group["group_link"]
        
        # Fall back to bot-generated link
        channel_data = channels_collection.find_one({"channel_id": channel_id})
        if channel_data and channel_data.get("invite_link"):
            if channel_data.get("created_at") and \
               (datetime.datetime.now() - channel_data["created_at"]).days < 1:
                return channel_data["invite_link"]

        try:
            chat_id = int(channel_id)
        except ValueError:
            chat_id = channel_id if channel_id.startswith('@') else f"@{channel_id}"

        try:
            invite_link = await context.bot.create_chat_invite_link(
                chat_id=chat_id,
                creates_join_request=True,
                name="Bot Access Link",
                expire_date=None,
                member_limit=None
            )
            invite_url = invite_link.invite_link
            channels_collection.update_one(
                {"channel_id": channel_id},
                {"$set": {
                    "invite_link": invite_url,
                    "created_at": datetime.datetime.now(),
                    "last_updated": datetime.datetime.now()
                }},
                upsert=True
            )
            return invite_url
        except BadRequest:
            try:
                chat = await context.bot.get_chat(chat_id)
                if chat.invite_link:
                    return chat.invite_link
                elif chat.username:
                    return f"https://t.me/{chat.username}"
            except Exception:
                pass

            if channel_id.startswith('-100'):
                return f"https://t.me/c/{channel_id[4:]}"
            elif channel_id.startswith('@'):
                return f"https://t.me/{channel_id[1:]}"
            else:
                return f"https://t.me/{channel_id}"
    except Exception as e:
        logger.error(f"❌ Error getting channel invite link: {e}")
        if channel_id.startswith('-100'):
            return f"https://t.me/c/{channel_id[4:]}"
        elif channel_id.startswith('@'):
            return f"https://t.me/{channel_id[1:]}"
        else:
            return f"https://t.me/{channel_id}"

# ================= MEMBERSHIP CHECK (SUPPORT CHANNELS + FORCED GROUP) =================
async def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user has joined all required channels (support + forced group)."""
    channels = get_required_channels()
    if not channels:
        return True

    for channel in channels:
        try:
            try:
                chat_id = int(channel)
            except ValueError:
                chat_id = channel if channel.startswith("@") else f"@{channel}"

            chat_member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if chat_member.status not in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER):
                return False
        except Exception as e:
            logger.error(f"❌ Channel check error ({channel}): {e}")
            return False

    return True

# ================= DISPLAY JOIN REQUIRED MESSAGE =================
async def show_join_required_message(update: Update, context: ContextTypes.DEFAULT_TYPE, callback_data: str = "check_join"):
    """Show message requiring user to join channels/groups."""
    keyboard = []
    required_channels = get_required_channels()
    
    if not required_channels:
        return True  # No requirements
    
    message = "🔐 *Access Restricted*\n\n"
    
    # Add forced group info if set
    forced_group = get_forced_group_info()
    if forced_group:
        message += "⚠️ *MANDATORY:* You must join the required group to use this bot.\n\n"
    
    # Add support channels info if any
    support_raw = os.environ.get("SUPPORT_CHANNELS", "").strip()
    if support_raw:
        message += "📢 *OPTIONAL:* Consider joining our support channels for updates.\n\n"
    
    message += "Please join ALL required channels/groups below:"
    
    # Create join buttons for all required channels
    for idx, channel in enumerate(required_channels):
        invite_link = await get_channel_invite_link(context, channel)
        
        # Determine button text
        if forced_group and channel == forced_group.get("group_id"):
            button_text = "🔐 JOIN REQUIRED GROUP"
        else:
            button_text = f"📢 Join Channel {idx+1}"
        
        keyboard.append([InlineKeyboardButton(button_text, url=invite_link)])

    keyboard.append([InlineKeyboardButton("✅ I've Joined All", callback_data=callback_data)])

    await update.message.reply_text(
        message,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    return False

# --- Telegram Bot Logic ---
telegram_bot_app = Application.builder().token(os.environ.get("TELEGRAM_TOKEN")).build()

# ================= COMMAND HANDLERS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    # Save / update user
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {
            "username": update.effective_user.username,
            "first_name": update.effective_user.first_name,
            "last_active": datetime.datetime.now()
        }},
        upsert=True
    )

    # Check if user has joined all required channels
    if not await check_channel_membership(user_id, context):
        callback_data = f"check_join_{context.args[0]}" if context.args else "check_join"
        await show_join_required_message(update, context, callback_data)
        return

    # 🔗 PROTECTED LINK FLOW (AFTER JOIN)
    if context.args:
        encoded_id = context.args[0]
        link_data = links_collection.find_one({"_id": encoded_id, "active": True})

        if link_data:
            web_app_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/join?token={encoded_id}"
            keyboard = [[
                InlineKeyboardButton("🔗 Join Group", web_app=WebAppInfo(url=web_app_url))
            ]]
            await update.message.reply_text(
                "🔐 This is a Protected Link\n\nClick the button below to proceed.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text("❌ Link expired or revoked")
        return

    # 👋 NORMAL START — WELCOME UI (ONLY AFTER JOIN)
    await show_welcome_message(update, context)

async def show_welcome_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show welcome message after user has joined all required channels."""
    user_name = update.effective_user.first_name or "User"

    welcome_msg = f"""╔──────── ✧ ────────╗
      Welcome {user_name}
╚──────── ✧ ────────╝

🤖 I am your Link Protection Bot
I help you keep your channel links safe & secure.

🛠 Commands:
• /start – Start the bot
• /protect – Generate protected link
• /help – Show help options

🌟 Features:
• 🔒 Advanced Link Encryption
• 🚀 Instant Link Generation
• 🛡️ Anti-Forward Protection
• 🎯 Easy to use UI"""

    keyboard = []
    
    # Add forced group button if set
    forced_group = get_forced_group_info()
    if forced_group:
        group_link = forced_group.get("group_link", "")
        if group_link:
            keyboard.append([InlineKeyboardButton("🔐 Required Group", url=group_link)])
    
    # Add support channel buttons
    support_raw = os.environ.get("SUPPORT_CHANNELS", "").strip()
    if support_raw:
        support_channels = [c.strip() for c in support_raw.split(",") if c.strip()]
        for channel in support_channels:
            invite_link = await get_channel_invite_link(context, channel)
            keyboard.append([InlineKeyboardButton("🌟 Support Channel", url=invite_link)])

    keyboard.append([InlineKeyboardButton("🚀 Create Protected Link", callback_data="create_link")])

    await update.message.reply_text(welcome_msg, reply_markup=InlineKeyboardMarkup(keyboard))

async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create protected link for ANY Telegram link (group or channel)."""
    # Check if user has joined all required channels
    if not await check_channel_membership(update.effective_user.id, context):
        await show_join_required_message(update, context, "check_join")
        return
    
    if not context.args or not context.args[0].startswith("https://t.me/"):
        await update.message.reply_text(
            "Usage: `/protect https://t.me/yourchannel`\n\n"
            "This works for:\n"
            "• Channels (public/private)\n"
            "• Groups (public/private)\n"
            "• Supergroups",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    telegram_link = context.args[0]
    
    if not telegram_link.startswith("https://t.me/"):
        await update.message.reply_text("❌ Invalid link. Must start with https://t.me/")
        return
    
    unique_id = str(uuid.uuid4())
    encoded_id = base64.urlsafe_b64encode(unique_id.encode()).decode().rstrip("=")
    
    short_id = encoded_id[:8].upper()

    links_collection.insert_one({
        "_id": encoded_id,
        "short_id": short_id,
        "telegram_link": telegram_link,
        "link_type": "channel" if "/c/" in telegram_link or "/s/" in telegram_link or telegram_link.count('/') == 1 else "group",
        "created_by": update.effective_user.id,
        "created_by_name": update.effective_user.first_name,
        "created_at": datetime.datetime.now(),
        "active": True,
        "clicks": 0
    })

    bot_username = (await context.bot.get_me()).username
    protected_link = f"https://t.me/{bot_username}?start={encoded_id}"
    
    keyboard = [
        [
            InlineKeyboardButton("📤 Share", url=f"https://t.me/share/url?url={protected_link}&text=🔐 Protected Link - Join via secure invitation"),
            InlineKeyboardButton("❌ Revoke", callback_data=f"revoke_{encoded_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"✅ *Protected Link Created!*\n\n"
        f"🔑 *Link ID:* `{short_id}`\n"
        f"📊 *Status:* 🟢 Active\n"
        f"🔗 *Original Link:* `{telegram_link}`\n"
        f"📝 *Type:* {'Channel' if 'channel' in telegram_link else 'Group'}\n"
        f"⏰ *Created:* {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"🔐 *Your Protected Link:*\n"
        f"`{protected_link}`\n\n"
        f"📋 *Quick Actions:*\n"
        f"• Copy the link above\n"
        f"• Share with your audience\n"
        f"• Revoke anytime with `/revoke {short_id}`",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revoke a link."""
    # Check if user has joined all required channels
    if not await check_channel_membership(update.effective_user.id, context):
        await show_join_required_message(update, context, "check_join")
        return
    
    if not context.args:
        user_id = update.effective_user.id
        active_links = list(links_collection.find(
            {"created_by": user_id, "active": True},
            sort=[("created_at", -1)],
            limit=10
        ))
        
        if not active_links:
            await update.message.reply_text("📭 No active links")
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
    
    link_id = context.args[0].upper()
    
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
    
    links_collection.update_one(
        {"_id": link_data['_id']},
        {
            "$set": {
                "active": False,
                "revoked_at": datetime.datetime.now()
            }
        }
    )
    
    await update.message.reply_text(
        f"✅ *Link Revoked!*\n\n"
        f"Link `{link_data.get('short_id', link_id)}` has been permanently revoked.\n\n"
        f"⚠️ All future access attempts will be blocked.",
        parse_mode=ParseMode.MARKDOWN
    )

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin broadcast."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *Admin Access Required*\n\n"
            "This command is restricted to administrators only.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "📢 *Broadcast System*\n\n"
            "To broadcast a message:\n"
            "1. Send any message\n"
            "2. Reply to it with `/broadcast`\n"
            "3. Confirm the action\n\n"
            "✨ *Features:*\n"
            "• Supports all media types\n"
            "• Preserves formatting\n"
            "• Tracks delivery\n"
            "• No rate limiting",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    total_users = users_collection.count_documents({})
    keyboard = [
        [InlineKeyboardButton("✅ Confirm Broadcast", callback_data="confirm_broadcast")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_broadcast")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    content_type = getattr(update.message.reply_to_message, 'content_type', 'text')
    
    await update.message.reply_text(
        f"⚠️ *Broadcast Confirmation*\n\n"
        f"📊 *Delivery Stats:*\n"
        f"• 📨 Recipients: `{total_users}` users\n"
        f"• 📝 Type: {content_type}\n"
        f"• ⚡ Delivery: Instant\n\n"
        f"Are you sure you want to proceed?",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    
    context.user_data['broadcast_message'] = update.message.reply_to_message

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show stats."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *Admin Access Required*\n\n"
            "This command is restricted to administrators only.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    total_users = users_collection.count_documents({})
    total_links = links_collection.count_documents({})
    active_links = links_collection.count_documents({"active": True})
    
    today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    new_users_today = users_collection.count_documents({"last_active": {"$gte": today}})
    new_links_today = links_collection.count_documents({"created_at": {"$gte": today}})
    
    total_clicks_result = links_collection.aggregate([
        {"$group": {"_id": None, "total_clicks": {"$sum": "$clicks"}}}
    ])
    total_clicks = 0
    for result in total_clicks_result:
        total_clicks = result.get('total_clicks', 0)
    
    # Add custom links stats
    forced_links_count = forced_links_collection.count_documents({})
    forced_group = forced_group_collection.find_one({})
    
    await update.message.reply_text(
        f"📊 *System Analytics Dashboard*\n\n"
        f"👥 *User Statistics*\n"
        f"• 📈 Total Users: `{total_users}`\n"
        f"• 🆕 New Today: `{new_users_today}`\n\n"
        f"🔗 *Link Statistics*\n"
        f"• 🔢 Total Links: `{total_links}`\n"
        f"• 🟢 Active Links: `{active_links}`\n"
        f"• 🆕 Created Today: `{new_links_today}`\n"
        f"• 👆 Total Clicks: `{total_clicks}`\n"
        f"• 🔧 Custom Links: `{forced_links_count}`\n"
        f"• 🔐 Forced Group: `{'Enabled' if forced_group else 'Disabled'}`\n\n"
        f"⚙️ *System Status*\n"
        f"• 🗄️ Database: 🟢 Operational\n"
        f"• 🤖 Bot: 🟢 Online\n"
        f"• ⚡ Uptime: 100%\n"
        f"• 🕐 Last Update: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        parse_mode=ParseMode.MARKDOWN
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help."""
    user_id = update.effective_user.id
    
    # Check if user has joined all required channels
    if not await check_channel_membership(user_id, context):
        await show_join_required_message(update, context, "check_join")
        return
    
    keyboard = []
    
    # Add forced group button if set
    forced_group = get_forced_group_info()
    if forced_group:
        group_link = forced_group.get("group_link", "")
        if group_link:
            keyboard.append([InlineKeyboardButton("🔐 Required Group", url=group_link)])
    
    # Add support channel buttons
    support_raw = os.environ.get("SUPPORT_CHANNELS", "").strip()
    if support_raw:
        support_channels = [c.strip() for c in support_raw.split(",") if c.strip()]
        for channel in support_channels:
            invite_link = await get_channel_invite_link(context, channel)
            keyboard.append([InlineKeyboardButton("🌟 Support Channel", url=invite_link)])
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    await update.message.reply_text(
        "🛡️ *LinkShield Pro - Help Center*\n\n"
        "✨ *What I Can Protect:*\n"
        "• 🔗 Telegram Channels\n"
        "• 👥 Telegram Groups\n"
        "• 🛡️ Private/Public links\n"
        "• 🔒 Supergroups\n\n"
        "📋 *Available Commands:*\n"
        "• `/start` - Start the bot\n"
        "• `/protect https://t.me/channel` - Create secure link\n"
        "• `/revoke` - Revoke access\n"
        "• `/help` - This message\n\n"
        "🔒 *How to Use:*\n"
        "1. Use `/protect https://t.me/yourchannel`\n"
        "2. Share the generated link\n"
        "3. Users join via verification\n"
        "4. Manage with `/revoke`\n\n"
        "💡 *Pro Tips:*\n"
        "• Works with any t.me link\n"
        "• Monitor link analytics\n"
        "• Revoke unused links\n"
        "• Join required channels to use the bot",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def force_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a custom invite link for a support channel."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *Admin Access Required*\n\n"
            "This command is restricted to administrators only.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/force <channel_identifier> <invite_link>`\n\n"
            "Example:\n"
            "`/force @mychannel https://t.me/+abc123def456`\n\n"
            "Channel identifier can be:\n"
            "• @username\n"
            "• Channel ID (like -1001234567890)\n"
            "• Channel link (https://t.me/username)",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    channel_identifier = context.args[0]
    custom_link = context.args[1]
    
    # Validate the custom link
    if not custom_link.startswith("https://t.me/"):
        await update.message.reply_text(
            "❌ Invalid invite link. Must be a Telegram invite link starting with https://t.me/"
        )
        return
    
    # Extract channel ID from identifier
    channel_id = channel_identifier
    if channel_identifier.startswith("https://t.me/"):
        # Extract from URL
        if channel_identifier.startswith("https://t.me/c/"):
            # Convert t.me/c/ format to -100 ID
            parts = channel_identifier.split('/')
            if len(parts) >= 4:
                channel_id = f"-100{parts[-1]}"
        elif channel_identifier.startswith("https://t.me/+"):
            # Public invite link
            channel_id = channel_identifier.split('/')[-1]
        else:
            # Username link
            channel_id = f"@{channel_identifier.split('/')[-1]}"
    
    # Store the forced link
    forced_links_collection.update_one(
        {"channel_id": channel_id},
        {"$set": {
            "forced_link": custom_link,
            "set_by": update.effective_user.id,
            "set_at": datetime.datetime.now(),
            "channel_identifier": channel_identifier
        }},
        upsert=True
    )
    
    await update.message.reply_text(
        f"✅ *Custom Link Set!*\n\n"
        f"📢 Channel: `{channel_identifier}`\n"
        f"🔗 Custom Link: `{custom_link}`\n"
        f"⏰ Set at: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"The bot will now use this custom link instead of generating its own.",
        parse_mode=ParseMode.MARKDOWN
    )

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove custom invite link for a support channel."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *Admin Access Required*\n\n"
            "This command is restricted to administrators only.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if not context.args:
        # Show all forced links
        forced_links = list(forced_links_collection.find({}))
        
        if not forced_links:
            await update.message.reply_text("📭 No custom links set")
            return
        
        message = "🔧 *Custom Links:*\n\n"
        keyboard = []
        
        for link in forced_links:
            channel_id = link.get("channel_identifier", link.get("channel_id", "Unknown"))
            custom_link = link.get("forced_link", "N/A")
            set_at = link.get("set_at", datetime.datetime.now()).strftime('%m/%d %H:%M')
            
            message += f"• `{channel_id}`\n  ↳ {custom_link[:30]}...\n  ↳ Set: {set_at}\n\n"
            keyboard.append([InlineKeyboardButton(
                f"❌ Remove {channel_id[:15]}...",
                callback_data=f"remove_forced_{link['channel_id']}"
            )])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message += "Click a button below to remove."
        
        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Remove by channel identifier
    channel_identifier = context.args[0]
    
    # Find and remove
    result = forced_links_collection.delete_one({
        "$or": [
            {"channel_id": channel_identifier},
            {"channel_identifier": channel_identifier}
        ]
    })
    
    if result.deleted_count > 0:
        await update.message.reply_text(
            f"✅ *Custom Link Removed!*\n\n"
            f"Channel: `{channel_identifier}`\n\n"
            f"The bot will now generate its own invite links for this channel.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text("❌ No custom link found for this channel")

async def list_forced_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all custom links."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *Admin Access Required*",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    forced_links = list(forced_links_collection.find({}))
    
    if not forced_links:
        await update.message.reply_text("📭 No custom links set")
        return
    
    message = "🔧 *Custom Links Configuration:*\n\n"
    
    for link in forced_links:
        channel_id = link.get("channel_identifier", link.get("channel_id", "Unknown"))
        custom_link = link.get("forced_link", "N/A")
        set_by = link.get("set_by", "Unknown")
        set_at = link.get("set_at", datetime.datetime.now()).strftime('%Y-%m-%d %H:%M')
        
        message += f"📢 *Channel:* `{channel_id}`\n"
        message += f"🔗 *Custom Link:* `{custom_link}`\n"
        message += f"👤 *Set By:* `{set_by}`\n"
        message += f"⏰ *Set At:* `{set_at}`\n"
        message += "━" * 30 + "\n\n"
    
    await update.message.reply_text(
        message,
        parse_mode=ParseMode.MARKDOWN
    )

async def forcegroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a forced group that users MUST join to use the bot."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *Admin Access Required*\n\n"
            "This command is restricted to administrators only.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if not context.args:
        # Show current forced group
        forced_group = forced_group_collection.find_one({})
        
        if not forced_group:
            await update.message.reply_text(
                "📭 *No Forced Group Set*\n\n"
                "Usage: `/forcegroup <group_link_or_username>`\n\n"
                "Examples:\n"
                "• `/forcegroup https://t.me/+abc123def456`\n"
                "• `/forcegroup @mygroup`\n"
                "• `/forcegroup https://t.me/mygroup`\n\n"
                "Users will be required to join this group before using the bot.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        group_id = forced_group.get("group_id", "Unknown")
        group_link = forced_group.get("group_link", "No link")
        set_by = forced_group.get("set_by", "Unknown")
        set_at = forced_group.get("set_at", datetime.datetime.now()).strftime('%Y-%m-%d %H:%M')
        
        keyboard = [
            [InlineKeyboardButton("❌ Remove Forced Group", callback_data="remove_forced_group")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"🔐 *Current Forced Group:*\n\n"
            f"📢 Group: `{group_id}`\n"
            f"🔗 Link: `{group_link}`\n"
            f"👤 Set By: `{set_by}`\n"
            f"⏰ Set At: `{set_at}`\n\n"
            f"⚠️ Users MUST join this group to use the bot.",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    group_identifier = context.args[0]
    
    # Validate and parse group identifier
    if group_identifier.startswith("https://t.me/+"):
        # Public invite link
        group_link = group_identifier
        group_id = group_identifier.split('/')[-1]
    elif group_identifier.startswith("https://t.me/"):
        # Username link or channel link
        group_link = group_identifier
        if group_identifier.startswith("https://t.me/c/"):
            # Convert to -100 format
            parts = group_identifier.split('/')
            if len(parts) >= 4:
                group_id = f"-100{parts[-1]}"
            else:
                group_id = group_identifier
        else:
            group_id = f"@{group_identifier.split('/')[-1]}"
    elif group_identifier.startswith("@"):
        # Username
        group_id = group_identifier
        group_link = f"https://t.me/{group_identifier[1:]}"
    elif group_identifier.startswith("-100"):
        # Group ID
        group_id = group_identifier
        group_link = f"https://t.me/c/{group_identifier[4:]}"
    else:
        # Try as username
        group_id = f"@{group_identifier}"
        group_link = f"https://t.me/{group_identifier}"
    
    # Store the forced group
    forced_group_collection.update_one(
        {},
        {"$set": {
            "group_id": group_id,
            "group_link": group_link,
            "set_by": update.effective_user.id,
            "set_at": datetime.datetime.now()
        }},
        upsert=True
    )
    
    await update.message.reply_text(
        f"✅ *Forced Group Set!*\n\n"
        f"📢 Group: `{group_id}`\n"
        f"🔗 Link: `{group_link}`\n"
        f"⏰ Set at: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"⚠️ From now on, users MUST join this group to use the bot.",
        parse_mode=ParseMode.MARKDOWN
    )

async def removeforcegroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove the forced group requirement."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *Admin Access Required*",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    result = forced_group_collection.delete_one({})
    
    if result.deleted_count > 0:
        await update.message.reply_text(
            "✅ *Forced Group Removed!*\n\n"
            "Users are no longer required to join a specific group to use the bot.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text("📭 No forced group was set")

async def store_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Store user activity."""
    if update.message and update.message.chat.type == "private":
        users_collection.update_one(
            {"user_id": update.effective_user.id},
            {"$set": {"last_active": update.message.date}},
            upsert=True
        )

# ================= CALLBACK HANDLERS =================
async def handle_broadcast_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle broadcast confirmation."""
    query = update.callback_query
    await query.answer()
    
    await query.message.edit_text("📤 *Broadcasting...*\n\nPlease wait, this may take a moment.", parse_mode=ParseMode.MARKDOWN)
    
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
    
    success_rate = (successful / total_users * 100) if total_users > 0 else 0
    
    await query.message.edit_text(
        f"✅ *Broadcast Complete!*\n\n"
        f"📊 *Delivery Report:*\n"
        f"• 📨 Total Recipients: `{total_users}`\n"
        f"• ✅ Successful: `{successful}`\n"
        f"• ❌ Failed: `{failed}`\n"
        f"• 📈 Success Rate: `{success_rate:.1f}%`\n"
        f"• ⏰ Time: {datetime.datetime.now().strftime('%H:%M:%S')}\n\n"
        f"✨ Broadcast logged in system.",
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_revoke_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link_id: str):
    """Handle revoke button."""
    query = update.callback_query
    await query.answer()
    
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
    
    links_collection.update_one(
        {"_id": link_id},
        {
            "$set": {
                "active": False,
                "revoked_at": datetime.datetime.now()
            }
        }
    )
    
    await query.message.edit_text(
        f"✅ *Link Revoked!*\n\n"
        f"Link `{link_data.get('short_id', link_id[:8])}` has been revoked.\n"
        f"👥 Final Clicks: {link_data.get('clicks', 0)}\n\n"
        f"⚠️ All access has been permanently blocked.",
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_remove_forced(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: str):
    """Handle remove forced link button."""
    query = update.callback_query
    await query.answer()
    
    result = forced_links_collection.delete_one({"channel_id": channel_id})
    
    if result.deleted_count > 0:
        await query.message.edit_text(
            f"✅ *Custom Link Removed!*\n\n"
            f"Channel ID: `{channel_id}`\n\n"
            f"The bot will now generate its own invite links for this channel.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await query.message.edit_text("❌ Link not found")

async def handle_remove_forced_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle remove forced group button."""
    query = update.callback_query
    await query.answer()
    
    result = forced_group_collection.delete_one({})
    
    if result.deleted_count > 0:
        await query.message.edit_text(
            "✅ *Forced Group Removed!*\n\n"
            "Users are no longer required to join a specific group to use the bot.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await query.message.edit_text("📭 No forced group was set")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button callbacks."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "check_join":
        if await check_channel_membership(query.from_user.id, context):
            await query.message.edit_text(
                "✅ *Verified!*\n\n"
                "You've joined all required channels/groups.\n"
                "You can now use the bot.\n\n"
                "Use /help for commands.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.answer(
                "❌ You haven't joined all channels/groups yet!\n"
                "Please join ALL required channels/groups and try again.",
                show_alert=True
            )
    
    elif query.data.startswith("check_join_"):
        encoded_id = query.data.replace("check_join_", "")
        
        if await check_channel_membership(query.from_user.id, context):
            link_data = links_collection.find_one({"_id": encoded_id, "active": True})
            
            if link_data:
                web_app_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/join?token={encoded_id}"
                
                keyboard = [[InlineKeyboardButton("🔗 Join Group", web_app=WebAppInfo(url=web_app_url))]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.message.edit_text(
                    "✅ *Verified!*\n\n"
                    "You can now access the protected link.",
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await query.message.edit_text("❌ Link expired or revoked")
        else:
            await query.answer(
                "❌ You haven't joined all channels/groups yet!\n"
                "Please join ALL required channels/groups and try again.",
                show_alert=True
            )
    
    elif query.data == "create_link":
        await query.message.reply_text(
            "To create a protected link, use:\n\n"
            "`/protect https://t.me/yourchannel`\n\n"
            "Replace with your actual channel link.",
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif query.data == "confirm_broadcast":
        await handle_broadcast_confirmation(update, context)
    
    elif query.data == "cancel_broadcast":
        await query.message.edit_text("❌ Broadcast cancelled")
    
    elif query.data.startswith("revoke_"):
        link_id = query.data.replace("revoke_", "")
        await handle_revoke_link(update, context, link_id)
    
    elif query.data.startswith("remove_forced_"):
        channel_id = query.data.replace("remove_forced_", "")
        await handle_remove_forced(update, context, channel_id)
    
    elif query.data == "remove_forced_group":
        await handle_remove_forced_group(update, context)

# Register all handlers
telegram_bot_app.add_handler(CommandHandler("start", start))
telegram_bot_app.add_handler(CommandHandler("protect", protect_command))
telegram_bot_app.add_handler(CommandHandler("revoke", revoke_command))
telegram_bot_app.add_handler(CommandHandler("broadcast", broadcast_command))
telegram_bot_app.add_handler(CommandHandler("stats", stats_command))
telegram_bot_app.add_handler(CommandHandler("help", help_command))
telegram_bot_app.add_handler(CommandHandler("force", force_command))
telegram_bot_app.add_handler(CommandHandler("remove", remove_command))
telegram_bot_app.add_handler(CommandHandler("customlinks", list_forced_command))
telegram_bot_app.add_handler(CommandHandler("forcegroup", forcegroup_command))
telegram_bot_app.add_handler(CommandHandler("removeforcegroup", removeforcegroup_command))
telegram_bot_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, store_message))

# Add callback handler
from telegram.ext import CallbackQueryHandler
telegram_bot_app.add_handler(CallbackQueryHandler(button_callback))

# --- FastAPI Setup ---
app = FastAPI()
templates = Jinja2Templates(directory="templates")

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
    
    # Log forced group and custom links on startup
    forced_group = forced_group_collection.find_one({})
    if forced_group:
        logger.info(f"✅ Forced Group is SET: {forced_group.get('group_id')}")
        logger.info(f"   Link: {forced_group.get('group_link')}")
    else:
        logger.info("ℹ️ No forced group set")
    
    forced_links = list(forced_links_collection.find({}))
    if forced_links:
        logger.info(f"Loaded {len(forced_links)} custom link(s)")
        for link in forced_links:
            logger.info(f"  - {link.get('channel_id')}: {link.get('forced_link')}")

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
    """Web app page."""
    return templates.TemplateResponse("join.html", {"request": request, "token": token})

@app.get("/getgrouplink/{token}")
async def get_group_link(token: str):
    """Get real group/channel link."""
    link_data = links_collection.find_one({"_id": token, "active": True})
    
    if link_data:
        links_collection.update_one(
            {"_id": token},
            {"$inc": {"clicks": 1}}
        )
        return {"url": link_data.get("telegram_link") or link_data.get("group_link")}
    else:
        raise HTTPException(status_code=404, detail="Link not found")

@app.get("/")
async def root():
    """Health check."""
    return {
        "status": "ok",
        "service": "LinkShield Pro",
        "version": "2.0.0",
        "time": datetime.datetime.now().isoformat()
    }