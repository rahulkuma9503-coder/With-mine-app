import os
import logging
import uuid
import base64
import asyncio
import datetime
import re
from typing import Optional, List
from pymongo import MongoClient
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.templating import Jinja2Templates

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, ChatMember
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
force_join_collection = db["force_join_channels"]

def init_db():
    try:
        client.admin.command('ismaster')
        logger.info("✅ MongoDB connected")
        users_collection.create_index("user_id", unique=True)
        links_collection.create_index("created_by")
        links_collection.create_index("active")
        links_collection.create_index("short_id", unique=True)
        force_join_collection.create_index("channel_id", unique=True)
        logger.info("✅ Database indexes created")
    except Exception as e:
        logger.error(f"❌ MongoDB error: {e}")
        raise

# ================= LINK EXTRACTION FUNCTIONS =================
def extract_channel_info(link: str):
    """
    Extract channel/group info from various link formats
    Returns: (chat_id, invite_link, is_public)
    """
    try:
        # Check if it's already a direct invite link
        if "https://t.me/+" in link:
            # Direct invite link: https://t.me/+AbCdEfGhIjKlMnOp
            invite_code = link.replace("https://t.me/+", "")
            return None, link, False
        
        elif "https://t.me/joinchat/" in link or "https://t.me/join/" in link:
            # Old invite link format
            return None, link, False
        
        elif "https://t.me/c/" in link:
            # Private channel link: https://t.me/c/1234567890/123
            parts = link.split('/')
            if len(parts) >= 4:
                channel_id = f"-100{parts[3]}"
                # For private channels, we can't generate links, need existing invite
                return channel_id, None, False
        
        elif "https://t.me/" in link:
            # Public channel/group: https://t.me/username or https://t.me/s/username
            username = link.replace("https://t.me/", "").replace("s/", "")
            if username.startswith('@'):
                username = username[1:]
            
            # Check if it's a public username
            if re.match(r'^[a-zA-Z0-9_]{5,}$', username):
                return f"@{username}", f"https://t.me/{username}", True
            else:
                # Might be a private group with custom link
                return None, link, False
        
        # Handle @username format
        elif link.startswith('@'):
            if re.match(r'^@[a-zA-Z0-9_]{5,}$', link[1:]):
                return link, f"https://t.me/{link[1:]}", True
            else:
                return link, None, False
        
        # Handle numeric IDs
        elif link.startswith('-100'):
            return link, None, False
        
        # Handle numeric ID without -100 prefix
        elif link.isdigit() and len(link) > 5:
            return f"-100{link}", None, False
        
        else:
            # Assume it's an invite link
            if link.startswith('+') or '/' in link:
                if not link.startswith('http'):
                    link = f"https://t.me/{link}"
                return None, link, False
    
    except Exception as e:
        logger.error(f"Error extracting channel info from {link}: {e}")
    
    return None, None, False

async def get_or_create_invite_link(context: ContextTypes.DEFAULT_TYPE, channel_info: str) -> str:
    """
    Get existing invite link or use provided link
    Returns a working invite link
    """
    try:
        chat_id, provided_link, is_public = extract_channel_info(channel_info)
        
        # If we already have a direct invite link, use it
        if provided_link and ("https://t.me/+" in provided_link or 
                            "https://t.me/joinchat/" in provided_link or 
                            "https://t.me/join/" in provided_link):
            return provided_link
        
        # If it's a public channel/group with username
        if is_public and provided_link:
            return provided_link
        
        # Try to get from database first
        if chat_id:
            channel_data = force_join_collection.find_one({"channel_id": chat_id})
            if channel_data and channel_data.get("invite_link"):
                return channel_data["invite_link"]
        
        # If no existing link, try to create one (requires admin)
        if chat_id:
            try:
                # First try to get chat to check permissions
                chat = await context.bot.get_chat(chat_id)
                
                # Try to create invite link
                try:
                    invite = await context.bot.create_chat_invite_link(
                        chat_id=chat_id,
                        creates_join_request=True,
                        name="Bot Access Link",
                        expire_date=None,
                        member_limit=None
                    )
                    invite_url = invite.invite_link
                    
                    # Store in database
                    force_join_collection.update_one(
                        {"channel_id": chat_id},
                        {"$set": {
                            "invite_link": invite_url,
                            "last_updated": datetime.datetime.now(),
                            "is_public": is_public
                        }},
                        upsert=True
                    )
                    return invite_url
                except Exception as e:
                    logger.warning(f"Cannot create invite link for {chat_id}: {e}")
                    
                    # Try to get existing invite link
                    if hasattr(chat, 'invite_link') and chat.invite_link:
                        return chat.invite_link
                    elif hasattr(chat, 'username') and chat.username:
                        return f"https://t.me/{chat.username}"
                    
                    return f"https://t.me/{chat_id}"
            
            except Exception as e:
                logger.error(f"Error getting chat {chat_id}: {e}")
        
        # Return the original link if all else fails
        return channel_info if channel_info.startswith('http') else f"https://t.me/{channel_info}"
    
    except Exception as e:
        logger.error(f"Error in get_or_create_invite_link: {e}")
        return channel_info if channel_info.startswith('http') else f"https://t.me/{channel_info}"

# ================= FORCE JOIN FUNCTIONS =================
async def get_force_join_channels() -> List[dict]:
    """Get all force join channels with invite links from database"""
    channels = list(force_join_collection.find({}))
    return channels

async def add_force_join_channel(channel_link: str, added_by: int, context: ContextTypes.DEFAULT_TYPE) -> dict:
    """Add a new force join channel"""
    try:
        # Extract channel info
        chat_id, _, is_public = extract_channel_info(channel_link)
        
        if not chat_id and not channel_link.startswith('http'):
            return {"success": False, "message": "Invalid channel link format"}
        
        # Get or create invite link
        invite_link = await get_or_create_invite_link(context, channel_link)
        
        # Store channel info
        channel_data = {
            "channel_id": chat_id or channel_link,
            "invite_link": invite_link,
            "added_by": added_by,
            "added_at": datetime.datetime.now(),
            "is_public": is_public,
            "original_link": channel_link
        }
        
        # Check if already exists
        existing = force_join_collection.find_one({"channel_id": channel_data["channel_id"]})
        if existing:
            # Update existing
            force_join_collection.update_one(
                {"channel_id": channel_data["channel_id"]},
                {"$set": {
                    "invite_link": invite_link,
                    "last_updated": datetime.datetime.now()
                }}
            )
            return {"success": True, "message": "Channel updated", "data": channel_data}
        
        # Insert new
        force_join_collection.insert_one(channel_data)
        return {"success": True, "message": "Channel added", "data": channel_data}
        
    except Exception as e:
        logger.error(f"Error adding force join channel: {e}")
        return {"success": False, "message": f"Error: {str(e)}"}

async def remove_force_join_channel(channel_identifier: str) -> bool:
    """Remove a force join channel"""
    try:
        result = force_join_collection.delete_one({"channel_id": channel_identifier})
        return result.deleted_count > 0
    except Exception as e:
        logger.error(f"Error removing force join channel: {e}")
        return False

# ================= MEMBERSHIP CHECK =================
async def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is member of all force join channels"""
    channels = await get_force_join_channels()
    if not channels:
        return True  # No force join channels set

    for channel in channels:
        try:
            chat_id = channel.get("channel_id")
            if not chat_id:
                continue
                
            # Skip check for channels without proper IDs (just invite links)
            if chat_id.startswith('http'):
                continue
                
            try:
                # Try to get chat member
                chat_member = await context.bot.get_chat_member(
                    chat_id=chat_id, 
                    user_id=user_id
                )
                if chat_member.status not in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER):
                    return False
            except Exception as e:
                logger.warning(f"Can't check membership for {chat_id}: {e}")
                # For private groups or if bot isn't admin, we can't verify
                # So we assume user hasn't joined
                return False
                
        except Exception as e:
            logger.error(f"Channel check error ({channel}): {e}")
            return False

    return True

# --- Telegram Bot Logic ---
telegram_bot_app = Application.builder().token(os.environ.get("TELEGRAM_TOKEN")).build()

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

    # 🔐 FORCE JOIN CHECK
    if not await check_channel_membership(user_id, context):
        callback_data = f"check_join_{context.args[0]}" if context.args else "check_join"

        channels = await get_force_join_channels()
        keyboard = []
        for ch in channels:
            invite_link = ch.get("invite_link")
            if invite_link:
                # Show "Join" on button instead of channel name
                keyboard.append(
                    [InlineKeyboardButton("📢 Join Channel", url=invite_link)]
                )

        if keyboard:  # Only add check button if there are channels to join
            keyboard.append(
                [InlineKeyboardButton("✅ I Have Joined", callback_data=callback_data)]
            )

            await update.message.reply_text(
                "🔐 *Access Restricted*\n\n"
                "Please join all required channels/groups to use this bot.\n"
                "After joining, click ✅ I Have Joined.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            return
        else:
            # No force join channels set, proceed normally
            pass

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

    channels = await get_force_join_channels()
    keyboard = []
    for ch in channels:
        invite_link = ch.get("invite_link")
        if invite_link:
            # Show "Support Channel" on button
            keyboard.append(
                [InlineKeyboardButton("🌟 Support Channel", url=invite_link)]
            )

    keyboard.append(
        [InlineKeyboardButton("🚀 Create Protected Link", callback_data="create_link")]
    )

    await update.message.reply_text(welcome_msg, reply_markup=InlineKeyboardMarkup(keyboard))

async def force_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a force join channel/group (Admin only)"""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *Admin Access Required*\n\n"
            "This command is restricted to administrators only.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if not context.args:
        # Show current force join channels
        channels = await get_force_join_channels()
        if not channels:
            message = "📋 *Force Join Channels*\n\nNo channels set.\n\n"
        else:
            message = "📋 *Force Join Channels*\n\n"
            for idx, ch in enumerate(channels, 1):
                channel_id = ch.get("channel_id", "Unknown")
                invite_link = ch.get("invite_link", "No link")
                message += f"{idx}. `{channel_id}`\n   Link: `{invite_link}`\n\n"
        
        message += "✨ *How to add channels:*\n\n"
        message += "1. *Public Channel/Group* (with username):\n"
        message += "   `/force https://t.me/username`\n"
        message += "   `/force @username`\n\n"
        message += "2. *Private Group* (with invite link):\n"
        message += "   `/force https://t.me/+AbCdEfGhIjKlMnOp`\n\n"
        message += "3. *Private Channel* (with invite link):\n"
        message += "   `/force https://t.me/joinchat/AbCdEfGhIjKlMnOp`\n\n"
        message += "💡 *Tip:* Use existing invite links for private groups!"
        
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        return
    
    channel_link = ' '.join(context.args)
    
    # Validate the link
    valid_formats = [
        "https://t.me/+",  # Private group invite
        "https://t.me/joinchat/",  # Old private group format
        "https://t.me/join/",  # New private group format
        "https://t.me/",  # Public channels/groups
        "@",  # Username format
        "-100",  # Channel ID format
    ]
    
    if not any(channel_link.startswith(fmt) for fmt in valid_formats):
        await update.message.reply_text(
            "❌ *Invalid channel link format.*\n\n"
            "✨ *Supported formats:*\n\n"
            "• *Public Channels/Groups:*\n"
            "  `https://t.me/username`\n"
            "  `@username`\n\n"
            "• *Private Groups:*\n"
            "  `https://t.me/+AbCdEfGhIjKlMnOp`\n"
            "  `https://t.me/joinchat/AbCdEfGhIjKlMnOp`\n\n"
            "• *Private Channels:*\n"
            "  `https://t.me/c/1234567890`\n"
            "  `-1001234567890`\n\n"
            "💡 *For private groups, use existing invite links!*",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Add the channel
    result = await add_force_join_channel(channel_link, update.effective_user.id, context)
    
    if result["success"]:
        channel_data = result["data"]
        invite_link = channel_data.get("invite_link", "No link generated")
        
        await update.message.reply_text(
            f"✅ *Force Join Channel Added!*\n\n"
            f"📌 *Channel ID:* `{channel_data['channel_id']}`\n"
            f"🔗 *Invite Link:* `{invite_link}`\n"
            f"👤 *Added by:* {update.effective_user.first_name}\n\n"
            f"📊 *Status:* 🟢 Active\n"
            f"👥 *Membership Check:* {'🟢 Enabled' if not channel_data.get('is_public', False) else '⚠️ Public (Limited)'}\n\n"
            f"New users will need to join this channel to use the bot.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"❌ *Failed to add channel*\n\n"
            f"Error: {result['message']}\n\n"
            f"💡 *Tips:*\n"
            f"• For private groups, use existing invite links\n"
            f"• Make sure bot is added to the group/channel\n"
            f"• For public channels, use the @username format",
            parse_mode=ParseMode.MARKDOWN
        )

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a force join channel/group (Admin only)"""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *Admin Access Required*\n\n"
            "This command is restricted to administrators only.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if not context.args:
        # Show current channels
        channels = await get_force_join_channels()
        if not channels:
            await update.message.reply_text("📭 No force join channels set.")
            return
        
        message = "🗑️ *Remove Force Join Channel*\n\n"
        for idx, ch in enumerate(channels, 1):
            channel_id = ch.get("channel_id", "Unknown")
            message += f"{idx}. `{channel_id}`\n"
        
        message += "\nTo remove a channel:\n"
        message += "`/remove @username`\n"
        message += "or\n"
        message += "`/remove 1` (by number from list)"
        
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        return
    
    channel_identifier = ' '.join(context.args)
    channels = await get_force_join_channels()
    
    # Check if identifier is a number (index)
    if channel_identifier.isdigit():
        idx = int(channel_identifier) - 1
        if 0 <= idx < len(channels):
            channel_to_remove = channels[idx].get("channel_id")
        else:
            await update.message.reply_text("❌ Invalid channel number.")
            return
    else:
        channel_to_remove = channel_identifier
    
    # Remove the channel
    if await remove_force_join_channel(channel_to_remove):
        await update.message.reply_text(
            f"✅ *Channel Removed!*\n\n"
            f"Force join channel has been removed:\n"
            f"`{channel_to_remove}`\n\n"
            f"New users will no longer need to join this channel.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"❌ Channel not found:\n`{channel_to_remove}`",
            parse_mode=ParseMode.MARKDOWN
        )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button callbacks."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "check_join":
        if await check_channel_membership(query.from_user.id, context):
            await query.message.edit_text(
                "✅ *Verified!*\n\n"
                "You can now use the bot.\n\n"
                "✨ *Available Commands:*\n"
                "• `/protect` - Create protected link\n"
                "• `/help` - Show all commands\n"
                "• `/start` - Main menu",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.answer("❌ You haven't joined all required channels yet.", show_alert=True)
    
    elif query.data.startswith("check_join_"):
        # Handle check join for protected links
        encoded_id = query.data.replace("check_join_", "")
        
        if await check_channel_membership(query.from_user.id, context):
            # User has joined, show protected link
            link_data = links_collection.find_one({"_id": encoded_id, "active": True})
            
            if link_data:
                web_app_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/join?token={encoded_id}"
                
                keyboard = [[InlineKeyboardButton("🔗 Join Group", web_app=WebAppInfo(url=web_app_url))]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.message.edit_text(
                    "✅ *Verified!*\n\n"
                    "You can now access the protected link.",
                    reply_markup=reply_markup
                )
            else:
                await query.message.edit_text("❌ Link expired or revoked")
        else:
            await query.answer("❌ You haven't joined all required channels yet.", show_alert=True)
    
    elif query.data == "create_link":
        await query.message.reply_text(
            "✨ *Create Protected Link*\n\n"
            "To protect any Telegram link:\n\n"
            "`/protect https://t.me/yourchannel`\n\n"
            "✨ *Supports:*\n"
            "• Public/Private Channels\n"
            "• Public/Private Groups\n"
            "• Supergroups\n"
            "• Any t.me link\n\n"
            "💡 *Tip:* Works with invite links too!",
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif query.data == "confirm_broadcast":
        await handle_broadcast_confirmation(update, context)
    
    elif query.data == "cancel_broadcast":
        await query.message.edit_text("❌ Broadcast cancelled")
    
    elif query.data.startswith("revoke_"):
        link_id = query.data.replace("revoke_", "")
        await handle_revoke_link(update, context, link_id)

async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create protected link for ANY Telegram link (group or channel)."""
    # Check channel membership
    if not await check_channel_membership(update.effective_user.id, context):
        channels = await get_force_join_channels()
        if channels:
            keyboard = []
            for ch in channels:
                invite_link = ch.get("invite_link")
                if invite_link:
                    # Show "Join" on button
                    keyboard.append([InlineKeyboardButton("📢 Join Channel", url=invite_link)])
            
            keyboard.append([InlineKeyboardButton("✅ I Have Joined", callback_data="check_join")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "🔐 *Access Required*\n\n"
                "Please join our channels to use this bot.\n"
                "After joining, click ✅ I Have Joined.",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        return
    
    if not context.args:
        await update.message.reply_text(
            "✨ *Create Protected Link*\n\n"
            "Usage: `/protect https://t.me/yourchannel`\n\n"
            "✨ *What I Can Protect:*\n"
            "• 🔗 Telegram Channels (public/private)\n"
            "• 👥 Telegram Groups (public/private)\n"
            "• 🛡️ Supergroups\n"
            "• 🔒 Private invite links\n\n"
            "💡 *Examples:*\n"
            "`/protect https://t.me/mychannel`\n"
            "`/protect https://t.me/+AbCdEfGhIjKlMnOp`\n"
            "`/protect https://t.me/joinchat/AbCdEfGhIjKlMnOp`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    telegram_link = context.args[0]
    
    # Validate the link
    if not (telegram_link.startswith("https://t.me/") or 
            telegram_link.startswith("http://t.me/") or
            telegram_link.startswith("t.me/")):
        await update.message.reply_text(
            "❌ *Invalid link format.*\n\n"
            "Links must start with:\n"
            "• `https://t.me/`\n"
            "• `t.me/`\n\n"
            "✨ *Valid examples:*\n"
            "• `https://t.me/mychannel`\n"
            "• `https://t.me/+invitecode`\n"
            "• `t.me/joinchat/invitecode`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Generate unique ID
    unique_id = str(uuid.uuid4())
    encoded_id = base64.urlsafe_b64encode(unique_id.encode()).decode().rstrip("=")
    
    # Create short ID (first 8 chars uppercase)
    short_id = encoded_id[:8].upper()

    # Determine link type
    if "/c/" in telegram_link:
        link_type = "private_channel"
    elif "/+" in telegram_link or "/joinchat/" in telegram_link or "/join/" in telegram_link:
        link_type = "private_group"
    elif telegram_link.count('/') == 3:  # https://t.me/username/123
        link_type = "public_message"
    else:
        link_type = "public_channel"

    links_collection.insert_one({
        "_id": encoded_id,
        "short_id": short_id,
        "telegram_link": telegram_link,
        "link_type": link_type,
        "created_by": update.effective_user.id,
        "created_by_name": update.effective_user.first_name,
        "created_at": datetime.datetime.now(),
        "active": True,
        "clicks": 0
    })

    bot_username = (await context.bot.get_me()).username
    protected_link = f"https://t.me/{bot_username}?start={encoded_id}"
    
    # Simple buttons
    keyboard = [
        [
            InlineKeyboardButton("📤 Share", url=f"https://t.me/share/url?url={protected_link}&text=🔐 Protected Link - Join via secure invitation"),
            InlineKeyboardButton("❌ Revoke", callback_data=f"revoke_{encoded_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Get link type emoji
    type_emoji = {
        "private_channel": "🔒",
        "private_group": "👥",
        "public_channel": "📢",
        "public_message": "📝"
    }.get(link_type, "🔗")
    
    await update.message.reply_text(
        f"✅ *Protected Link Created!*\n\n"
        f"🔑 *Link ID:* `{short_id}`\n"
        f"📊 *Status:* 🟢 Active\n"
        f"{type_emoji} *Type:* {link_type.replace('_', ' ').title()}\n"
        f"🔗 *Original Link:* `{telegram_link[:50]}{'...' if len(telegram_link) > 50 else ''}`\n"
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

# [Keep the rest of the functions the same: revoke_command, handle_revoke_link, 
# broadcast_command, handle_broadcast_confirmation, stats_command, help_command, 
# store_message - they remain unchanged from previous version]

# Register handlers
telegram_bot_app.add_handler(CommandHandler("start", start))
telegram_bot_app.add_handler(CommandHandler("protect", protect_command))
telegram_bot_app.add_handler(CommandHandler("revoke", revoke_command))
telegram_bot_app.add_handler(CommandHandler("broadcast", broadcast_command))
telegram_bot_app.add_handler(CommandHandler("stats", stats_command))
telegram_bot_app.add_handler(CommandHandler("help", help_command))
telegram_bot_app.add_handler(CommandHandler("force", force_command))
telegram_bot_app.add_handler(CommandHandler("remove", remove_command))
telegram_bot_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, store_message))

# Add callback handler
from telegram.ext import CallbackQueryHandler
telegram_bot_app.add_handler(CallbackQueryHandler(button_callback))

# [Keep the FastAPI setup and other functions unchanged from previous version]

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
    
    # Check force join channels
    force_channels = await get_force_join_channels()
    if force_channels:
        logger.info(f"Force join channels: {len(force_channels)}")
        for ch in force_channels:
            logger.info(f"  - {ch.get('channel_id')}: {ch.get('invite_link')}")

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
        "version": "2.1.0",
        "force_join": "Dynamic System",
        "time": datetime.datetime.now().isoformat()
    }