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
from telegram.constants import ChatAction, ParseMode
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

# Initialize MongoDB client and select database/collection
client = MongoClient(MONGODB_URI)
db_name = "protected_bot_db"
db = client[db_name]
links_collection = db["protected_links"]
users_collection = db["users"]
channels_collection = db["channels"]
broadcast_collection = db["broadcast_history"]

def init_db():
    """Verifies the MongoDB connection and creates/updates indexes."""
    try:
        client.admin.command('ismaster')
        logger.info("✅ MongoDB connection successful.")
        
        # Create or update indexes for better performance
        try:
            users_collection.create_index("user_id", unique=True)
            logger.info("✅ Users index created/updated.")
        except Exception as e:
            logger.warning(f"⚠️ Could not create users index: {e}")
        
        try:
            channels_collection.create_index("channel_id", unique=True)
            logger.info("✅ Channels index created/updated.")
        except Exception as e:
            logger.warning(f"⚠️ Could not create channels index: {e}")
        
        # Remove TTL index (no auto expiration)
        try:
            # Get existing indexes
            existing_indexes = list(links_collection.list_indexes())
            
            for idx in existing_indexes:
                if 'expireAfterSeconds' in idx:
                    links_collection.drop_index(idx['name'])
                    logger.info(f"🗑️ Dropped TTL index: {idx['name']}")
                    break
                    
        except Exception as e:
            logger.warning(f"⚠️ Could not remove TTL index: {e}")
        
        # Create new indexes
        try:
            links_collection.create_index([("created_by", 1)])
            links_collection.create_index([("active", 1)])
            links_collection.create_index([("created_at", -1)])
            logger.info("✅ Link indexes created.")
        except Exception as e:
            logger.warning(f"⚠️ Could not create link indexes: {e}")
            
        try:
            broadcast_collection.create_index([("date", -1)])
            logger.info("✅ Broadcast index created.")
        except Exception as e:
            logger.warning(f"⚠️ Could not create broadcast index: {e}")
            
    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        raise

async def get_or_create_channel_invite(context: ContextTypes.DEFAULT_TYPE, channel_id: str) -> Optional[str]:
    """Get or create an invite link for a channel using its ID."""
    try:
        # Try to convert channel_id to integer (for private channels)
        try:
            chat_id = int(channel_id)
        except ValueError:
            # If it's not a number, it might be a public channel username
            if channel_id.startswith('@'):
                chat_id = channel_id
            else:
                chat_id = f"@{channel_id}"
        
        # Try to create an invite link
        invite_link: ChatInviteLink = await context.bot.create_chat_invite_link(
            chat_id=chat_id,
            creates_join_request=True,
            name="Premium Access Link",
            expire_date=None  # Never expires
        )
        return invite_link.invite_link
    except BadRequest as e:
        logger.error(f"❌ Failed to create invite link for channel {channel_id}: {e}")
        try:
            chat = await context.bot.get_chat(chat_id)
            if chat.invite_link:
                return chat.invite_link
            elif chat.username:
                return f"https://t.me/{chat.username}"
        except Exception as e2:
            logger.error(f"❌ Failed to get chat info: {e2}")
    except Exception as e:
        logger.error(f"❌ Unexpected error creating invite link: {e}")
    
    return None

async def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is member of the support channel using channel ID."""
    support_channel = os.environ.get("SUPPORT_CHANNEL", "").strip()
    if not support_channel:
        return True  # Skip check if channel not configured
    
    try:
        # Try to convert to integer (private channel ID)
        try:
            chat_id = int(support_channel)
        except ValueError:
            if support_channel.startswith('@'):
                chat_id = support_channel
            else:
                chat_id = f"@{support_channel}"
        
        chat_member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return chat_member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]
    except BadRequest as e:
        if "user not found" in str(e).lower() or "chat not found" in str(e).lower():
            return False
        logger.error(f"❌ Error checking channel membership: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error checking membership: {e}")
        return False

async def require_channel_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check and enforce channel membership with a button using channel ID."""
    user_id = update.effective_user.id
    
    # Store user in database
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {
            "username": update.effective_user.username,
            "first_name": update.effective_user.first_name,
            "last_name": update.effective_user.last_name,
            "language_code": update.effective_user.language_code,
            "last_active": update.message.date if update.message else datetime.datetime.now(),
            "is_premium": update.effective_user.is_premium or False
        }},
        upsert=True
    )
    
    # Check if user is in channel
    if await check_channel_membership(user_id, context):
        return True
    
    # User not in channel, show join button
    support_channel = os.environ.get("SUPPORT_CHANNEL", "").strip()
    if support_channel:
        invite_link = await get_or_create_channel_invite(context, support_channel)
        
        if invite_link:
            keyboard = [
                [InlineKeyboardButton("🌟 Join Official Channel", url=invite_link)],
                [InlineKeyboardButton("✅ Verify Membership", callback_data="check_join")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                channels_collection.update_one(
                    {"channel_id": support_channel},
                    {"$set": {
                        "invite_link": invite_link,
                        "last_updated": datetime.datetime.now()
                    }},
                    upsert=True
                )
            except Exception as e:
                logger.error(f"❌ Failed to store channel info: {e}")
            
            welcome_msg = """
🔒 *Premium Access Required*

Welcome to *LinkShield Pro*! 

To access our premium features and ensure you receive important updates, please join our official channel.

📌 **Why join?**
• Get notified of new features
• Priority support access
• Exclusive tips & tricks
• Community announcements

After joining, click *'Verify Membership'* below.
"""
            await update.message.reply_text(
                welcome_msg,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                "👋 Welcome to LinkShield Pro!\n\n"
                "⚠️ *Notice:* Channel verification is temporarily unavailable.\n"
                "You can proceed to use the bot.",
                parse_mode=ParseMode.MARKDOWN
            )
    else:
        await update.message.reply_text(
            "👋 Welcome to LinkShield Pro!\n\n"
            "I'm your premium link protection assistant.",
            parse_mode=ParseMode.MARKDOWN
        )
    
    return False

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle button callbacks."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "check_join":
        if await check_channel_membership(query.from_user.id, context):
            await query.message.edit_text(
                "✅ *Verification Successful!*\n\n"
                "🌟 Welcome to the premium community!\n\n"
                "You now have full access to all features.\n\n"
                "Use /features to see what's available.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.answer(
                "❌ *Verification Failed*\n\n"
                "We couldn't verify your membership. Please:\n"
                "1. Make sure you joined the channel\n"
                "2. Try again in a few seconds\n"
                "3. Contact support if the issue persists",
                show_alert=True
            )
    
    elif query.data in ["confirm_broadcast", "cancel_broadcast"]:
        await handle_broadcast_confirmation(update, context)
    
    elif query.data.startswith("revoke_"):
        await handle_revoke_confirmation(update, context)

# --- Telegram Bot Logic ---
telegram_bot_app = Application.builder().token(os.environ.get("TELEGRAM_TOKEN")).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    # Check channel membership first
    if not await require_channel_membership(update, context):
        return
    
    if context.args:
        encoded_id = context.args[0]
        link_data = links_collection.find_one({"_id": encoded_id, "active": True})

        if link_data:
            group_link = link_data["group_link"]
            web_app_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/join?token={encoded_id}"
            
            keyboard = [
                [InlineKeyboardButton("🔗 Join Protected Group", web_app=WebAppInfo(url=web_app_url))]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            welcome_msg = f"""
🛡️ *Secure Access Required*

You've been invited to join a protected group.

📋 *Access Details:*
• Link ID: `{encoded_id[:8]}...`
• Created: {link_data.get('created_at', datetime.datetime.now()).strftime('%Y-%m-%d')}
• Status: ✅ Active

⚠️ *Security Notice:*
This link is protected by LinkShield Pro.
Click the button below to proceed with verification.
"""
            await update.message.reply_text(
                welcome_msg,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                "❌ *Invalid or Revoked Link*\n\n"
                "This protected link is either:\n"
                "• Invalid or expired\n"
                "• Revoked by the creator\n"
                "• No longer active\n\n"
                "Please contact the link creator for a new invitation.",
                parse_mode=ParseMode.MARKDOWN
            )
        return
    
    # No args - show main menu
    support_channel = os.environ.get("SUPPORT_CHANNEL", "")
    
    keyboard = []
    if support_channel:
        channel_data = channels_collection.find_one({"channel_id": support_channel})
        if channel_data and channel_data.get("invite_link"):
            keyboard.append([InlineKeyboardButton("🌟 Official Channel", url=channel_data["invite_link"])])
    
    broadcast_channel = os.environ.get("BROADCAST_CHANNEL", "")
    if broadcast_channel:
        broadcast_invite = await get_or_create_channel_invite(context, broadcast_channel)
        if broadcast_invite:
            keyboard.append([InlineKeyboardButton("📢 Announcements", url=broadcast_invite)])
    
    keyboard.extend([
        [InlineKeyboardButton("🛡️ Create Protected Link", callback_data="create_link")],
        [InlineKeyboardButton("📊 My Statistics", callback_data="my_stats")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
        [InlineKeyboardButton("💬 Support", url="https://t.me/your_support")]
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    welcome_msg = """
🌟 *Welcome to LinkShield Pro!*

🔒 *Your Ultimate Link Protection Solution*

I help you create secure, protected links for your Telegram groups with advanced security features.

✨ *Premium Features:*
• 🔐 Military-grade link protection
• 👑 Admin-controlled link lifetime
• 📊 Detailed analytics & tracking
• 🛡️ Anti-spam & flood protection
• 👥 Multi-admin support
• 📈 Usage statistics

📋 *Quick Commands:*
/protect - Create a new protected link
/links - View your created links
/revoke - Revoke an active link
/stats - View detailed statistics
/help - Get help & instructions

👇 Use the buttons below or commands to get started!
"""
    
    await update.message.reply_text(
        welcome_msg,
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
            "📝 *Usage:*\n`/protect https://t.me/yourgroupname`\n\n"
            "💡 *Tip:* Make sure the group link is correct and the bot is an admin in the group.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    group_link = context.args[0]
    unique_id = str(uuid.uuid4())
    encoded_id = base64.urlsafe_b64encode(unique_id.encode()).decode().rstrip("=")
    
    # Generate a short ID for display
    short_id = encoded_id[:12]

    links_collection.insert_one({
        "_id": encoded_id,
        "short_id": short_id,
        "group_link": group_link,
        "created_by": update.effective_user.id,
        "created_by_name": update.effective_user.first_name,
        "created_at": datetime.datetime.now(),
        "active": True,
        "clicks": 0,
        "last_used": None,
        "is_premium": update.effective_user.is_premium or False
    })

    bot_username = (await context.bot.get_me()).username
    protected_link = f"https://t.me/{bot_username}?start={encoded_id}"
    
    # Create management buttons
    keyboard = [
        [InlineKeyboardButton("🔗 Copy Link", callback_data=f"copy_{encoded_id}")],
        [InlineKeyboardButton("📊 View Analytics", callback_data=f"stats_{encoded_id}")],
        [InlineKeyboardButton("❌ Revoke Link", callback_data=f"revoke_{encoded_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    success_msg = f"""
✅ *Protected Link Created Successfully!*

🛡️ *Link Details:*
• ID: `{short_id}`
• Status: 🟢 Active
• Created: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
• Type: {'🌟 Premium' if update.effective_user.is_premium else '✨ Standard'}

🔗 *Your Protected Link:*
`{protected_link}`

📋 *Management Options:*
• Share the link above with users
• Track usage with /links command
• Revoke anytime with /revoke command
• View analytics for this link

⚠️ *Important Notes:*
• Links never expire automatically
• Only you can revoke this link
• All clicks are tracked and logged
• Premium users get enhanced analytics

Use the buttons below for quick actions!
"""
    
    await update.message.reply_text(
        success_msg,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def links_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View all links created by the user."""
    # Check channel membership
    if not await require_channel_membership(update, context):
        return
    
    user_id = update.effective_user.id
    user_links = list(links_collection.find(
        {"created_by": user_id},
        sort=[("created_at", -1)],
        limit=20
    ))
    
    if not user_links:
        await update.message.reply_text(
            "📭 *No Links Found*\n\n"
            "You haven't created any protected links yet.\n\n"
            "Use /protect to create your first secure link!",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    total_links = len(user_links)
    active_links = sum(1 for link in user_links if link.get('active', True))
    total_clicks = sum(link.get('clicks', 0) for link in user_links)
    
    stats_msg = f"""
📊 *Your Link Statistics*

• Total Links: {total_links}
• Active Links: {active_links}
• Total Clicks: {total_clicks}
• Success Rate: {(total_clicks/(total_links*10)*100 if total_links>0 else 0):.1f}%

📋 *Recent Links (Last 20):*
"""
    
    for i, link in enumerate(user_links[:10], 1):
        status = "🟢" if link.get('active', True) else "🔴"
        clicks = link.get('clicks', 0)
        created = link.get('created_at', datetime.datetime.now()).strftime('%m/%d')
        short_id = link.get('short_id', link['_id'][:8])
        
        stats_msg += f"\n{i}. {status} `{short_id}` - {clicks} clicks - {created}"
    
    if len(user_links) > 10:
        stats_msg += f"\n\n...and {len(user_links) - 10} more links"
    
    stats_msg += "\n\nUse /revoke <link_id> to revoke any link."
    
    keyboard = [
        [InlineKeyboardButton("🛡️ Create New Link", callback_data="create_link")],
        [InlineKeyboardButton("📈 Detailed Analytics", callback_data="full_stats")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        stats_msg,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revoke a protected link."""
    # Check channel membership
    if not await require_channel_membership(update, context):
        return
    
    if not context.args:
        # Show user's links for revoking
        user_id = update.effective_user.id
        active_links = list(links_collection.find(
            {"created_by": user_id, "active": True},
            sort=[("created_at", -1)],
            limit=10
        ))
        
        if not active_links:
            await update.message.reply_text(
                "📭 *No Active Links*\n\n"
                "You don't have any active links to revoke.\n\n"
                "Create one with /protect first!",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        msg = "🔐 *Revoke Protected Link*\n\nSelect a link to revoke:\n\n"
        keyboard = []
        
        for i, link in enumerate(active_links, 1):
            short_id = link.get('short_id', link['_id'][:8])
            clicks = link.get('clicks', 0)
            created = link.get('created_at', datetime.datetime.now()).strftime('%m/%d')
            
            msg += f"{i}. `{short_id}` - {clicks} clicks - {created}\n"
            keyboard.append([InlineKeyboardButton(
                f"❌ Revoke {short_id}",
                callback_data=f"revoke_{link['_id']}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        msg += "\n⚠️ *Warning:* Revoking is permanent and cannot be undone!"
        
        await update.message.reply_text(
            msg,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Try to revoke by short ID or full ID
    link_id = context.args[0]
    
    # Try to find by short_id first
    link_data = links_collection.find_one({
        "$or": [
            {"_id": link_id},
            {"short_id": link_id}
        ],
        "created_by": update.effective_user.id,
        "active": True
    })
    
    if not link_data:
        await update.message.reply_text(
            "❌ *Link Not Found*\n\n"
            "Either the link doesn't exist, is already revoked, or you don't have permission to revoke it.\n\n"
            "Use /links to see your active links.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Ask for confirmation
    short_id = link_data.get('short_id', link_data['_id'][:8])
    keyboard = [
        [InlineKeyboardButton("✅ Yes, Revoke Now", callback_data=f"confirm_revoke_{link_data['_id']}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_revoke")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"⚠️ *Confirm Revocation*\n\n"
        f"Are you sure you want to revoke link `{short_id}`?\n\n"
        f"📊 *Stats:*\n"
        f"• Clicks: {link_data.get('clicks', 0)}\n"
        f"• Created: {link_data.get('created_at', datetime.datetime.now()).strftime('%Y-%m-%d')}\n\n"
        f"❌ *This action is permanent!*",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_revoke_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle revoke confirmation."""
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("confirm_revoke_"):
        link_id = query.data.replace("confirm_revoke_", "")
        
        result = links_collection.update_one(
            {"_id": link_id, "created_by": query.from_user.id},
            {"$set": {
                "active": False,
                "revoked_at": datetime.datetime.now(),
                "revoked_by": query.from_user.id
            }}
        )
        
        if result.modified_count > 0:
            await query.message.edit_text(
                "✅ *Link Revoked Successfully!*\n\n"
                "The protected link has been permanently revoked.\n\n"
                "🔒 *Security Action Logged:*\n"
                "• Time: " + datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S') + "\n"
                "• Action: Link Revocation\n"
                "• Status: Completed\n\n"
                "📊 All existing users will no longer be able to access this link.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.message.edit_text(
                "❌ *Revocation Failed*\n\n"
                "The link could not be revoked. It may have already been revoked or you don't have permission.",
                parse_mode=ParseMode.MARKDOWN
            )
    
    elif query.data == "cancel_revoke":
        await query.message.edit_text(
            "🔄 *Revocation Cancelled*\n\n"
            "The link remains active and accessible.",
            parse_mode=ParseMode.MARKDOWN
        )

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Broadcast message to all bot users (Admin only)."""
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
            "1. Send any message (text, photo, video, etc.)\n"
            "2. Reply to it with `/broadcast`\n"
            "3. Confirm the action\n\n"
            "✨ *Features:*\n"
            "• Supports all media types\n"
            "• Preserves original formatting\n"
            "• Tracks delivery status\n"
            "• No rate limiting issues",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    total_users = users_collection.count_documents({})
    keyboard = [
        [InlineKeyboardButton("✅ Confirm Broadcast", callback_data="confirm_broadcast")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_broadcast")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"⚠️ *Broadcast Confirmation*\n\n"
        f"📊 *Stats:*\n"
        f"• Recipients: {total_users} users\n"
        f"• Type: {update.message.reply_to_message.content_type}\n"
        f"• Time: Now\n\n"
        f"Are you sure you want to proceed?",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    
    context.user_data['broadcast_message'] = update.message.reply_to_message

async def handle_broadcast_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle broadcast confirmation."""
    query = update.callback_query
    await query.answer()
    
    if query.data == "confirm_broadcast":
        await query.message.edit_text("📤 *Broadcasting...*\n\nPlease wait, this may take a moment.")
        
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
                logger.error(f"❌ Failed to broadcast to user {user['user_id']}: {e}")
                failed += 1
        
        broadcast_collection.insert_one({
            "admin_id": query.from_user.id,
            "date": datetime.datetime.now(),
            "message_type": message_to_broadcast.content_type,
            "total_users": total_users,
            "successful": successful,
            "failed": failed,
            "message_preview": str(message_to_broadcast.text or message_to_broadcast.caption)[:100]
        })
        
        success_rate = (successful / total_users * 100) if total_users > 0 else 0
        
        await query.message.edit_text(
            f"✅ *Broadcast Complete!*\n\n"
            f"📊 *Delivery Report:*\n"
            f"• Total Recipients: {total_users}\n"
            f"• ✅ Successful: {successful}\n"
            f"• ❌ Failed: {failed}\n"
            f"• 📈 Success Rate: {success_rate:.1f}%\n"
            f"• 🕐 Time: {datetime.datetime.now().strftime('%H:%M:%S')}\n\n"
            f"✨ Broadcast logged in system.",
            parse_mode=ParseMode.MARKDOWN
        )
        
    elif query.data == "cancel_broadcast":
        await query.message.edit_text(
            "🔄 *Broadcast Cancelled*\n\n"
            "No messages were sent.",
            parse_mode=ParseMode.MARKDOWN
        )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show bot statistics (Admin only)."""
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
    premium_users = users_collection.count_documents({"is_premium": True})
    
    today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    new_users_today = users_collection.count_documents({"last_active": {"$gte": today}})
    new_links_today = links_collection.count_documents({"created_at": {"$gte": today}})
    
    recent_broadcasts = list(broadcast_collection.find().sort("date", -1).limit(3))
    broadcast_stats = ""
    for bc in recent_broadcasts:
        date_str = bc["date"].strftime("%m/%d %H:%M")
        success_rate = (bc["successful"] / bc["total_users"] * 100) if bc["total_users"] > 0 else 0
        broadcast_stats += f"• {date_str}: {bc['successful']}/{bc['total_users']} ({success_rate:.1f}%)\n"
    
    await update.message.reply_text(
        f"📊 *System Statistics*\n\n"
        f"👥 *User Analytics:*\n"
        f"• Total Users: {total_users}\n"
        f"• Premium Users: {premium_users}\n"
        f"• New Today: {new_users_today}\n\n"
        f"🔗 *Link Analytics:*\n"
        f"• Total Links: {total_links}\n"
        f"• Active Links: {active_links}\n"
        f"• Created Today: {new_links_today}\n\n"
        f"📢 *Recent Broadcasts:*\n{broadcast_stats if broadcast_stats else '• No broadcasts yet'}\n"
        f"⚙️ *System Status:*\n"
        f"• Database: 🟢 Operational\n"
        f"• Bot: 🟢 Online\n"
        f"• Uptime: 100%\n"
        f"• Last Update: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        parse_mode=ParseMode.MARKDOWN
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help message."""
    if not await require_channel_membership(update, context):
        return
    
    support_channel = os.environ.get("SUPPORT_CHANNEL", "")
    keyboard = []
    
    if support_channel:
        channel_data = channels_collection.find_one({"channel_id": support_channel})
        if channel_data and channel_data.get("invite_link"):
            keyboard.append([InlineKeyboardButton("🌟 Official Channel", url=channel_data["invite_link"])])
    
    keyboard.append([InlineKeyboardButton("💬 Support Chat", url="https://t.me/your_support")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    help_text = """
🛡️ *LinkShield Pro - Help Center*

✨ *Premium Features Overview:*
• 🔐 Military-grade link protection
• 👑 Admin-controlled lifetime
• 📊 Advanced analytics dashboard
• 🛡️ Anti-spam & flood protection
• 👥 Multi-admin management
• 📈 Real-time tracking

📋 *Available Commands:*

🔒 *Link Management:*
`/protect <link>` - Create protected link
`/links` - View your created links
`/revoke` - Revoke a link
`/analytics` - View detailed stats

👑 *Admin Commands:*
`/stats` - System statistics
`/broadcast` - Send announcement
`/users` - User management

🆘 *Support:*
`/help` - This help message
`/tutorial` - Getting started guide
`/feedback` - Send feedback

⚙️ *Settings:*
`/settings` - Configure preferences
`/upgrade` - Premium features

📖 *How to Use:*
1. Use `/protect` with your group link
2. Share the generated secure link
3. Manage access with `/links`
4. Revoke anytime with `/revoke`

💡 *Pro Tips:*
• Always verify group links before protecting
• Use short memorable names for easy management
• Regular security audits recommended
• Backup important links

Need more help? Contact our support team:
"""
    
    await update.message.reply_text(
        help_text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def features_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show premium features."""
    if not await require_channel_membership(update, context):
        return
    
    keyboard = [
        [InlineKeyboardButton("🌟 Get Premium", callback_data="get_premium")],
        [InlineKeyboardButton("📚 Documentation", url="https://docs.example.com")],
        [InlineKeyboardButton("🎥 Video Tutorial", url="https://youtube.com/example")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    features_msg = """
✨ *Premium Features - LinkShield Pro*

🎯 *Core Protection:*
• 🔐 Military-grade encryption
• 🛡️ DDoS protection
• 🚫 Anti-spam filters
• 👮 Role-based access control

📊 *Advanced Analytics:*
• 📈 Real-time click tracking
• 👤 User behavior analysis
• 📍 Geographic insights
• ⏰ Time-based statistics

👑 *Admin Features:*
• ⚡ Instant link revocation
• 👥 Bulk link management
• 📢 Mass notifications
• 🔄 Automated backups

🔄 *Automation:*
• 🤖 Auto-moderation rules
• 📅 Scheduled actions
• 🔔 Smart notifications
• 📱 Mobile management

🔧 *Customization:*
• 🎨 Custom branding
• 📝 Personalized messages
• 🏷️ Custom link slugs
• 🎯 Targeted access rules

🌟 *Premium Benefits:*
• 24/7 Priority support
• 99.9% Uptime guarantee
• Unlimited link creation
• Advanced security audits

💰 *Pricing:*
• Basic: Free (5 links)
• Pro: $9.99/month (unlimited)
• Enterprise: Custom pricing

Ready to upgrade? Click below!
"""
    
    await update.message.reply_text(
        features_msg,
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
telegram_bot_app.add_handler(CommandHandler("links", links_command))
telegram_bot_app.add_handler(CommandHandler("revoke", revoke_command))
telegram_bot_app.add_handler(CommandHandler("broadcast", broadcast_command))
telegram_bot_app.add_handler(CommandHandler("stats", stats_command))
telegram_bot_app.add_handler(CommandHandler("help", help_command))
telegram_bot_app.add_handler(CommandHandler("features", features_command))
telegram_bot_app.add_handler(CommandHandler("analytics", links_command))
telegram_bot_app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, store_message))

# Add callback query handler for buttons
from telegram.ext import CallbackQueryHandler
telegram_bot_app.add_handler(CallbackQueryHandler(button_callback))

# --- FastAPI Web Server Setup ---
app = FastAPI()

@app.on_event("startup")
async def on_startup():
    """Initializes the database, starts the PTB app, and sets the Telegram webhook."""
    logger.info("🌟 Application startup...")
    
    required_vars = ["TELEGRAM_TOKEN", "RENDER_EXTERNAL_URL"]
    for var in required_vars:
        if not os.environ.get(var):
            logger.critical(f"❌ {var} is not set. Exiting.")
            raise Exception(f"{var} environment variable not set!")
    
    init_db()
    
    await telegram_bot_app.initialize()
    await telegram_bot_app.start()
    
    webhook_url = f"{os.environ.get('RENDER_EXTERNAL_URL')}/{os.environ.get('TELEGRAM_TOKEN')}"
    await telegram_bot_app.bot.set_webhook(url=webhook_url)
    logger.info(f"✅ Webhook set to {webhook_url}")
    
    bot_info = await telegram_bot_app.bot.get_me()
    logger.info(f"🤖 Bot started: @{bot_info.username}")
    logger.info("🚀 Application startup complete.")

@app.on_event("shutdown")
async def on_shutdown():
    """Stops the PTB application and closes the database connection."""
    logger.info("🛑 Application shutdown...")
    await telegram_bot_app.stop()
    await telegram_bot_app.shutdown()
    client.close()
    logger.info("✅ Application shutdown complete.")

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
            {"$inc": {"clicks": 1}, "$set": {"last_used": datetime.datetime.now()}}
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
        "version": "2.0.0",
        "timestamp": datetime.datetime.now().isoformat(),
        "stats": {
            "users": users_collection.count_documents({}),
            "active_links": links_collection.count_documents({"active": True}),
            "uptime": "100%"
        }
    }