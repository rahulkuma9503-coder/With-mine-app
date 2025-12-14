import os
import logging
import uuid
import base64
import asyncio
import datetime
from typing import Optional, List, Dict, Any
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
forced_groups_collection = db["forced_groups"]

def init_db():
    try:
        client.admin.command('ismaster')
        logger.info("✅ MongoDB connected")
        users_collection.create_index("user_id", unique=True)
        links_collection.create_index("created_by")
        links_collection.create_index("active")
        channels_collection.create_index("channel_id", unique=True)
        forced_links_collection.create_index("channel_id", unique=True)
        forced_groups_collection.create_index("group_id", unique=True)
        logger.info("✅ Database indexes created")
    except Exception as e:
        logger.error(f"❌ MongoDB error: {e}")
        raise

# ================= GET ALL REQUIRED CHANNELS (SUPPORT + FORCED GROUPS) =================
def get_required_channels() -> List[Dict[str, Any]]:
    """Get all channels user must join (support channels + forced groups)."""
    channels = []
    
    # Add support channels from environment
    support_raw = os.environ.get("SUPPORT_CHANNELS", "").strip()
    if support_raw:
        for channel in support_raw.split(","):
            if channel.strip():
                channels.append({
                    "id": channel.strip(),
                    "type": "support",
                    "is_public": True  # Assume support channels are public
                })
    
    # Add forced groups from database
    forced_groups = list(forced_groups_collection.find({}))
    for group in forced_groups:
        if group.get("group_id"):
            channels.append({
                "id": group["group_id"],
                "type": "forced",
                "is_public": group.get("is_public", False),
                "invite_link": group.get("group_link"),
                "name": group.get("group_name", "Required Group")
            })
    
    return channels

# ================= CHECK IF FORCED GROUPS ARE SET =================
def has_forced_groups() -> bool:
    """Check if any forced groups are configured."""
    return forced_groups_collection.count_documents({}) > 0

# ================= GET ALL FORCED GROUPS INFO =================
def get_all_forced_groups():
    """Get information about all forced groups."""
    return list(forced_groups_collection.find({}))

# ================= DETECT IF GROUP IS PUBLIC =================
async def is_group_public(context: ContextTypes.DEFAULT_TYPE, group_id: str) -> bool:
    """Check if a group/channel is public (has username)."""
    try:
        try:
            chat_id = int(group_id)
        except ValueError:
            if group_id.startswith('@'):
                return True  # Has username, so public
            chat_id = group_id
        
        chat = await context.bot.get_chat(chat_id)
        return chat.username is not None
    except Exception as e:
        logger.error(f"Error checking if group is public {group_id}: {e}")
        return False

# ================= GET GROUP INVITE LINK (WORKS FOR BOTH PUBLIC AND PRIVATE) =================
async def get_group_invite_link(context: ContextTypes.DEFAULT_TYPE, group_info: Dict[str, Any]) -> str:
    """Get invite link for a group/channel, handling both public and private groups."""
    group_id = group_info["id"]
    
    # If we already have a stored invite link, use it
    if group_info.get("invite_link"):
        return group_info["invite_link"]
    
    # Check forced links collection
    forced_link_data = forced_links_collection.find_one({"channel_id": group_id})
    if forced_link_data and forced_link_data.get("forced_link"):
        logger.info(f"Using forced link for group {group_id}")
        return forced_link_data["forced_link"]
    
    # Try to get from channels collection
    channel_data = channels_collection.find_one({"channel_id": group_id})
    if channel_data and channel_data.get("invite_link"):
        if channel_data.get("created_at") and \
           (datetime.datetime.now() - channel_data["created_at"]).days < 1:
            return channel_data["invite_link"]
    
    try:
        # Try to parse chat_id
        try:
            chat_id = int(group_id)
        except ValueError:
            chat_id = group_id
        
        # Check if group is public
        try:
            chat = await context.bot.get_chat(chat_id)
            
            # If group has username, it's public
            if chat.username:
                return f"https://t.me/{chat.username}"
            
            # For private groups, try to create invite link
            try:
                # Bot needs to be admin in private group to create invite link
                invite_link = await context.bot.create_chat_invite_link(
                    chat_id=chat_id,
                    creates_join_request=True,
                    name="Bot Access Link",
                    expire_date=None,
                    member_limit=None
                )
                invite_url = invite_link.invite_link
                
                # Store the link
                channels_collection.update_one(
                    {"channel_id": group_id},
                    {"$set": {
                        "invite_link": invite_url,
                        "created_at": datetime.datetime.now(),
                        "last_updated": datetime.datetime.now(),
                        "is_public": False
                    }},
                    upsert=True
                )
                return invite_url
            except BadRequest as e:
                logger.error(f"Cannot create invite link for {group_id}: {e}")
                
                # Try to get existing invite link
                try:
                    chat = await context.bot.get_chat(chat_id)
                    if chat.invite_link:
                        return chat.invite_link
                except Exception:
                    pass
                
                # For private groups, we need a pre-existing invite link
                # Return a placeholder that admin must fix
                return "https://t.me/+PRIVATE_GROUP_NEEDS_INVITE_LINK"
                
        except Exception as e:
            logger.error(f"Error getting chat info for {group_id}: {e}")
    
    except Exception as e:
        logger.error(f"❌ Error getting group invite link for {group_id}: {e}")
    
    # Fallback for private groups
    if group_id.startswith('-100'):
        return f"https://t.me/c/{group_id[4:]}"
    elif group_id.startswith('@'):
        return f"https://t.me/{group_id[1:]}"
    else:
        return f"https://t.me/{group_id}"

# ================= MEMBERSHIP CHECK (WITH PRIVATE GROUP SUPPORT) =================
async def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user has joined all required channels (support + forced groups)."""
    channels = get_required_channels()
    if not channels:
        return True

    for channel_info in channels:
        channel_id = channel_info["id"]
        
        # Skip membership check for private groups where bot can't verify
        if not channel_info.get("is_public", True):
            logger.info(f"Skipping membership check for private group {channel_id}")
            continue
        
        try:
            # Try to parse chat_id
            try:
                chat_id = int(channel_id)
            except ValueError:
                chat_id = channel_id if channel_id.startswith("@") else f"@{channel_id}"

            # Try to get chat member
            chat_member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if chat_member.status not in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER):
                logger.info(f"User {user_id} is not a member of {channel_id}")
                return False
                
        except Exception as e:
            logger.error(f"❌ Membership check error for {channel_id}: {e}")
            # If we can't check membership (e.g., bot not in group), we assume user hasn't joined
            # This is a safety measure to ensure forced groups work
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
    
    # Check forced groups
    forced_groups = [c for c in required_channels if c["type"] == "forced"]
    if forced_groups:
        public_groups = [g for g in forced_groups if g.get("is_public", True)]
        private_groups = [g for g in forced_groups if not g.get("is_public", True)]
        
        if public_groups:
            message += f"⚠️ *MANDATORY:* You must join {len(public_groups)} public group(s).\n\n"
        
        if private_groups:
            message += f"🔒 *MANDATORY:* You must join {len(private_groups)} private group(s).\n"
            message += "   (The bot cannot verify private group membership)\n\n"
    
    # Support channels
    support_channels = [c for c in required_channels if c["type"] == "support"]
    if support_channels:
        message += "📢 *OPTIONAL:* Consider joining our support channels for updates.\n\n"
    
    message += "Please join ALL required channels/groups below:"
    
    # Create join buttons
    for idx, channel_info in enumerate(required_channels):
        invite_link = await get_group_invite_link(context, channel_info)
        
        # Determine button text
        if channel_info["type"] == "forced":
            if channel_info.get("is_public", True):
                button_text = f"🔐 JOIN REQUIRED GROUP {idx+1}"
            else:
                button_text = f"🔒 JOIN PRIVATE GROUP {idx+1}"
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
    
    # Add forced group buttons
    forced_groups = get_all_forced_groups()
    for idx, group in enumerate(forced_groups):
        group_link = group.get("group_link", "")
        if group_link:
            group_name = group.get("group_name", f"Required Group {idx+1}")
            keyboard.append([InlineKeyboardButton(f"🔐 {group_name}", url=group_link)])
    
    # Add support channel buttons
    support_raw = os.environ.get("SUPPORT_CHANNELS", "").strip()
    if support_raw:
        support_channels = [c.strip() for c in support_raw.split(",") if c.strip()]
        for channel in support_channels:
            channel_info = {"id": channel, "type": "support", "is_public": True}
            invite_link = await get_group_invite_link(context, channel_info)
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
            "• Supergroups\n"
            "• Private invite links (https://t.me/+abc123)",
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

# ================= FORCED GROUPS MANAGEMENT (ENHANCED FOR PRIVATE GROUPS) =================
async def forcegroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a forced group that users MUST join to use the bot."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *Admin Access Required*\n\n"
            "This command is restricted to administrators only.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if not context.args:
        # Show current forced groups
        forced_groups = list(forced_groups_collection.find({}))
        
        if not forced_groups:
            await update.message.reply_text(
                "📭 *No Forced Groups Set*\n\n"
                "Usage: `/forcegroup <group_link_or_username> [group_name]`\n\n"
                "Examples:\n"
                "• `/forcegroup https://t.me/+abc123def456 My Private Group`\n"
                "• `/forcegroup @mygroup Public Group`\n"
                "• `/forcegroup https://t.me/mygroup Channel Name`\n\n"
                "For private groups:\n"
                "1. Add the bot as admin to the group\n"
                "2. Create an invite link in the group\n"
                "3. Use that link with this command",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        message = "🔐 *Current Forced Groups:*\n\n"
        keyboard = []
        
        for idx, group in enumerate(forced_groups):
            group_id = group.get("group_id", "Unknown")
            group_link = group.get("group_link", "No link")
            group_name = group.get("group_name", f"Group {idx+1}")
            is_public = group.get("is_public", False)
            set_at = group.get("set_at", datetime.datetime.now()).strftime('%Y-%m-%d %H:%M')
            
            message += f"*{idx+1}. {group_name}*\n"
            message += f"  📢 ID: `{group_id}`\n"
            message += f"  🔗 Link: `{group_link}`\n"
            message += f"  📍 Type: {'Public' if is_public else 'Private'}\n"
            message += f"  ⏰ Added: `{set_at}`\n\n"
            
            keyboard.append([
                InlineKeyboardButton(f"❌ Remove {group_name[:15]}", callback_data=f"remove_forced_group_{group['group_id']}")
            ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    group_identifier = context.args[0]
    group_name = " ".join(context.args[1:]) if len(context.args) > 1 else "Required Group"
    
    # Check if it's a valid invite link
    if not group_identifier.startswith("https://t.me/"):
        await update.message.reply_text(
            "❌ *Invalid Link!*\n\n"
            "Must be a Telegram invite link starting with:\n"
            "• `https://t.me/+` (private group)\n"
            "• `https://t.me/@` (public group/channel)\n"
            "• `https://t.me/username` (public)\n\n"
            "For private groups:\n"
            "1. Create an invite link in the group\n"
            "2. Use that link here",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Parse the group identifier
    if group_identifier.startswith("https://t.me/+"):
        # Private group invite link
        group_id = group_identifier.split('/')[-1]
        is_public = False
        group_link = group_identifier
    elif group_identifier.startswith("https://t.me/c/"):
        # Channel link with ID
        parts = group_identifier.split('/')
        if len(parts) >= 4:
            group_id = f"-100{parts[-1]}"
        else:
            group_id = group_identifier
        is_public = False
        group_link = group_identifier
    elif group_identifier.startswith("https://t.me/"):
        # Username link
        username = group_identifier.split('/')[-1]
        if username.startswith('@'):
            group_id = username
        else:
            group_id = f"@{username}"
        is_public = True
        group_link = group_identifier
    else:
        await update.message.reply_text("❌ Invalid group link format")
        return
    
    # Check if group already exists
    existing_group = forced_groups_collection.find_one({"group_id": group_id})
    if existing_group:
        await update.message.reply_text(
            f"⚠️ *Group Already Exists!*\n\n"
            f"This group is already in the forced list.\n\n"
            f"Use `/forcegroup` to see all groups.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Try to verify the group if it's public
    if is_public:
        try:
            chat = await context.bot.get_chat(group_id)
            group_name = chat.title or group_name
        except Exception as e:
            logger.warning(f"Could not get chat info for {group_id}: {e}")
    
    # Store the forced group
    forced_groups_collection.insert_one({
        "group_id": group_id,
        "group_link": group_link,
        "group_name": group_name,
        "is_public": is_public,
        "set_by": update.effective_user.id,
        "set_at": datetime.datetime.now()
    })
    
    total_groups = forced_groups_collection.count_documents({})
    
    await update.message.reply_text(
        f"✅ *Forced Group Added!*\n\n"
        f"📢 Name: *{group_name}*\n"
        f"🔗 Link: `{group_link}`\n"
        f"📍 Type: {'Public' if is_public else 'Private'}\n"
        f"⏰ Added: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"📊 Total: `{total_groups}` forced group(s)\n\n"
        f"⚠️ Users must now join ALL {total_groups} group(s) to use the bot.\n"
        f"{'✅ Bot can verify membership' if is_public else '⚠️ Bot cannot verify private group membership'}",
        parse_mode=ParseMode.MARKDOWN
    )

async def testgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Test if the bot can check membership in a group."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *Admin Access Required*",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if not context.args:
        await update.message.reply_text(
            "Usage: `/testgroup <group_link_or_id>`\n\n"
            "Tests if the bot can check membership in the group.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    group_identifier = context.args[0]
    
    try:
        # Parse group identifier
        if group_identifier.startswith("https://t.me/+"):
            group_id = group_identifier.split('/')[-1]
        elif group_identifier.startswith("https://t.me/c/"):
            parts = group_identifier.split('/')
            group_id = f"-100{parts[-1]}" if len(parts) >= 4 else group_identifier
        elif group_identifier.startswith("https://t.me/"):
            username = group_identifier.split('/')[-1]
            group_id = f"@{username}" if not username.startswith('@') else username
        elif group_identifier.startswith('-100') or group_identifier.startswith('@'):
            group_id = group_identifier
        else:
            group_id = f"@{group_identifier}"
        
        # Try to get chat info
        chat = await context.bot.get_chat(group_id)
        
        # Check if bot is in the group
        try:
            bot_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=context.bot.id)
            bot_in_group = bot_member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]
            bot_is_admin = bot_member.status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]
        except Exception:
            bot_in_group = False
            bot_is_admin = False
        
        # Try to get bot's own member status
        try:
            test_member = await context.bot.get_chat_member(chat_id=chat.id, user_id=update.effective_user.id)
            can_check_membership = True
        except Exception:
            can_check_membership = False
        
        message = f"🔍 *Group Test Results:*\n\n"
        message += f"📢 *Title:* {chat.title}\n"
        message += f"🆔 *ID:* `{chat.id}`\n"
        message += f"📍 *Type:* {chat.type}\n"
        message += f"🌐 *Public:* {'Yes' if chat.username else 'No'}\n"
        message += f"🤖 *Bot in Group:* {'✅ Yes' if bot_in_group else '❌ No'}\n"
        message += f"👑 *Bot is Admin:* {'✅ Yes' if bot_is_admin else '❌ No'}\n"
        message += f"🔐 *Can Check Membership:* {'✅ Yes' if can_check_membership else '❌ No'}\n\n"
        
        if chat.username:
            message += f"🔗 *Public Link:* https://t.me/{chat.username}\n"
        
        if not can_check_membership:
            message += "\n⚠️ *Warning:* The bot cannot check membership in this group.\n"
            message += "Users will be required to join but membership won't be verified."
        
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        logger.error(f"Error testing group {group_identifier}: {e}")
        await update.message.reply_text(
            f"❌ *Error Testing Group:*\n\n"
            f"Error: `{str(e)}`\n\n"
            f"Make sure:\n"
            f"1. The group exists\n"
            f"2. The bot is added to the group (for private groups)\n"
            f"3. You're using the correct link/ID",
            parse_mode=ParseMode.MARKDOWN
        )

async def fixgrouplink_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fix/update invite link for a forced group."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *Admin Access Required*",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/fixgrouplink <group_id_or_name> <new_invite_link>`\n\n"
            "Updates the invite link for a forced group.\n"
            "Use `/forcegroup` to see group IDs.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    group_identifier = context.args[0]
    new_link = context.args[1]
    
    if not new_link.startswith("https://t.me/"):
        await update.message.reply_text("❌ New link must be a Telegram invite link starting with https://t.me/")
        return
    
    # Find the group
    query = {
        "$or": [
            {"group_id": group_identifier},
            {"group_name": {"$regex": group_identifier, "$options": "i"}}
        ]
    }
    
    group = forced_groups_collection.find_one(query)
    
    if not group:
        await update.message.reply_text("❌ No forced group found with that identifier")
        return
    
    # Update the link
    forced_groups_collection.update_one(
        {"_id": group["_id"]},
        {"$set": {
            "group_link": new_link,
            "last_updated": datetime.datetime.now()
        }}
    )
    
    await update.message.reply_text(
        f"✅ *Group Link Updated!*\n\n"
        f"📢 Group: *{group.get('group_name', 'Unknown')}*\n"
        f"🔗 New Link: `{new_link}`\n"
        f"⏰ Updated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        parse_mode=ParseMode.MARKDOWN
    )

# ================= PRIVATE GROUP WORKAROUND =================
async def privategroup_workaround(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Instructions for setting up private groups."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        return
    
    message = "🔒 *Private Group Setup Guide*\n\n"
    message += "For *private groups*, follow these steps:\n\n"
    message += "1. *Add the bot as admin* to your private group:\n"
    message += "   - Go to group settings\n"
    message += "   - Add @bot_username as administrator\n"
    message += "   - Grant at least 'Invite Users' permission\n\n"
    message += "2. *Create an invite link* in the group:\n"
    message += "   - Go to group settings > Invite Links\n"
    message += "   - Create a new link (no expiration)\n"
    message += "   - Copy the link (looks like: https://t.me/+abc123)\n\n"
    message += "3. *Add the group to forced list*:\n"
    message += "   - Use `/forcegroup https://t.me/+abc123 Group Name`\n\n"
    message += "4. *Test the setup*:\n"
    message += "   - Use `/testgroup https://t.me/+abc123`\n\n"
    message += "⚠️ *Important:*\n"
    message += "• The bot cannot verify membership in private groups\n"
    message += "• Users must manually click 'I've Joined' after joining\n"
    message += "• Keep the invite link active\n"
    
    await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

# Rest of the code remains the same (revoke_command, broadcast_command, stats_command, help_command, etc.)
# Just need to update the imports and handlers

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
telegram_bot_app.add_handler(CommandHandler("clearforcegroups", clearforcegroups_command))
telegram_bot_app.add_handler(CommandHandler("testgroup", testgroup_command))  # New
telegram_bot_app.add_handler(CommandHandler("fixgrouplink", fixgrouplink_command))  # New
telegram_bot_app.add_handler(CommandHandler("privateguide", privategroup_workaround))  # New
telegram_bot_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, store_message))

# Add callback handler (keep the existing button_callback function)
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
    
    # Log forced groups
    forced_groups = get_all_forced_groups()
    if forced_groups:
        logger.info(f"✅ {len(forced_groups)} Forced Group(s):")
        for idx, group in enumerate(forced_groups):
            logger.info(f"   {idx+1}. {group.get('group_name')} ({'Public' if group.get('is_public') else 'Private'})")
            logger.info(f"      Link: {group.get('group_link')}")
    else:
        logger.info("ℹ️ No forced groups set")

# Rest of FastAPI endpoints remain the same
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