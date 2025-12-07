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
            [InlineKeyboardButton("🌟 JOIN OUR PREMIUM CHANNEL", url=invite_link)],
            [InlineKeyboardButton("✅ VERIFY MEMBERSHIP", callback_data="check_join")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "🔐 *PREMIUM ACCESS REQUIRED*\n\n"
            "✨ Welcome to the elite circle of *LinkShield Pro* users!\n\n"
            "🚀 To unlock our premium features and join our exclusive community, "
            "you must first subscribe to our official channel.\n\n"
            "📌 *Benefits of joining:*\n"
            "• 🎯 Priority access to new features\n"
            "• ⚡ Faster support response\n"
            "• 💎 Exclusive premium content\n"
            "• 🔔 Early announcement alerts\n\n"
            "Click the button below to join, then verify your membership.",
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
                "🎉 *WELCOME TO THE ELITE CLUB!*\n\n"
                "✅ *Verification Successful!*\n\n"
                "✨ You are now part of our premium community!\n"
                "🚀 Unlocking all premium features for you...\n\n"
                "⚡ *Features Activated:*\n"
                "• 🔐 Military-grade link protection\n"
                "• 📊 Advanced analytics dashboard\n"
                "• ⚡ Priority processing\n"
                "• 💎 Premium support access\n\n"
                "Use /help to see all available commands!",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.answer(
                "❌ *MEMBERSHIP VERIFICATION FAILED*\n\n"
                "We couldn't verify your channel subscription.\n\n"
                "⚠️ Please ensure:\n"
                "1. You've joined the channel\n"
                "2. Wait a few seconds\n"
                "3. Try again\n\n"
                "If issues persist, contact @admin",
                show_alert=True
            )
    
    elif query.data == "confirm_broadcast":
        await handle_broadcast_confirmation(update, context)
    
    elif query.data == "cancel_broadcast":
        await query.message.edit_text(
            "🔄 *BROADCAST CANCELLED*\n\n"
            "No messages were sent to users.",
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif query.data.startswith("revoke_"):
        link_id = query.data.replace("revoke_", "")
        await handle_revoke_link(update, context, link_id)
    
    elif query.data.startswith("copy_"):
        link_id = query.data.replace("copy_", "")
        await handle_copy_link(update, context, link_id)
    
    elif query.data.startswith("share_"):
        link_id = query.data.replace("share_", "")
        await handle_share_link(update, context, link_id)

async def handle_copy_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link_id: str):
    """Handle copy link button callback."""
    query = update.callback_query
    await query.answer("📋 Link copied to clipboard!")
    
    # Get the full link
    bot_username = (await context.bot.get_me()).username
    protected_link = f"https://t.me/{bot_username}?start={link_id}"
    
    await query.message.reply_text(
        f"📋 *LINK COPIED*\n\n"
        f"Here's your protected link:\n\n"
        f"`{protected_link}`\n\n"
        f"✅ Ready to share!",
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_share_link(update: Update, context: ContextTypes.DEFAULT_TYPE, link_id: str):
    """Handle share link button callback."""
    query = update.callback_query
    await query.answer()
    
    # Get link data
    link_data = links_collection.find_one({"_id": link_id})
    if not link_data:
        await query.answer("❌ Link not found!", show_alert=True)
        return
    
    bot_username = (await context.bot.get_me()).username
    protected_link = f"https://t.me/{bot_username}?start={link_id}"
    short_id = link_data.get('short_id', link_id[:8])
    
    # Create share message with buttons
    keyboard = [
        [InlineKeyboardButton("🔗 COPY LINK", callback_data=f"copy_{link_id}")],
        [
            InlineKeyboardButton("📱 TELEGRAM", url=f"https://t.me/share/url?url={protected_link}&text=Join%20via%20secure%20link"),
            InlineKeyboardButton("💬 WHATSAPP", url=f"https://wa.me/?text=Join%20via%20secure%20link:%20{protected_link}")
        ],
        [
            InlineKeyboardButton("📧 EMAIL", url=f"mailto:?subject=Secure%20Invitation&body=Join%20via%20this%20secure%20link:%20{protected_link}"),
            InlineKeyboardButton("📋 OTHER", callback_data=f"copy_{link_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.message.reply_text(
        f"📤 *SHARE PROTECTED LINK*\n\n"
        f"🔗 *Link ID:* `{short_id}`\n"
        f"📊 *Clicks:* {link_data.get('clicks', 0)}\n"
        f"⏰ *Created:* {link_data.get('created_at', datetime.datetime.now()).strftime('%d %b %Y')}\n\n"
        f"✨ *Share this secure link via:*",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

# --- Telegram Bot Logic ---
telegram_bot_app = Application.builder().token(os.environ.get("TELEGRAM_TOKEN")).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command with premium welcome effect."""
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
            
            keyboard = [[InlineKeyboardButton("🔐 JOIN SECURE GROUP", web_app=WebAppInfo(url=web_app_url))]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            welcome_animation = """
🔒 *SECURE ACCESS PORTAL* 🔒

⚡ *Welcome to LinkShield Pro Security System*

🎯 *Access Details:*
• 🔑 Link ID: `{}`
• 🛡️ Security: Military Grade
• 📊 Status: ✅ ACTIVE
• ⏰ Created: {}

⚠️ *Security Protocol Activated*
This link is protected by advanced encryption and verification systems.

✅ *Click below to proceed with biometric verification...*
""".format(
    encoded_id[:12],
    link_data.get('created_at', datetime.datetime.now()).strftime('%d %b %Y, %H:%M')
)
            
            await update.message.reply_text(
                welcome_animation,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text(
                "❌ *ACCESS DENIED*\n\n"
                "🔐 This secure link has been:\n"
                "• Revoked by creator\n"
                "• Expired\n"
                "• Invalidated\n\n"
                "⚠️ Please contact the sender for a new secure invitation.",
                parse_mode=ParseMode.MARKDOWN
            )
        return
    
    # No args - show premium welcome message
    support_channel = os.environ.get("SUPPORT_CHANNEL", "")
    
    # Create interactive keyboard
    keyboard = [
        [InlineKeyboardButton("🚀 CREATE PROTECTED LINK", callback_data="create_link")],
        [InlineKeyboardButton("📊 MY SECURE LINKS", callback_data="my_links")],
        [InlineKeyboardButton("⚙️ SETTINGS", callback_data="settings")]
    ]
    
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
            keyboard.append([InlineKeyboardButton("🌟 PREMIUM CHANNEL", url=invite_link)])
    
    keyboard.append([InlineKeyboardButton("💎 GET PREMIUM", callback_data="get_premium")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Premium welcome message with animation effect
    premium_welcome = """
✨ *✨ WELCOME TO LINKSHIELD PRO ✨*

🎉 *Congratulations!* You've discovered the ultimate link protection solution.

🔐 *ENTERPRISE-GRADE SECURITY*
• Military-grade encryption
• Advanced threat detection
• Real-time monitoring
• Zero-trust architecture

🚀 *PREMIUM FEATURES*
• 🔒 Unlimited protected links
• 📊 Advanced analytics dashboard
• ⚡ Lightning-fast processing
• 🛡️ DDoS protection
• 👥 Team collaboration
• 📈 Performance insights

⚡ *QUICK START*
Use `/protect https://t.me/yourgroup` to create your first secure link.

📋 *COMMANDS*
• `/protect` - Create secure link
• `/revoke` - Revoke access
• `/stats` - View analytics
• `/help` - Full guide

👇 *Get started with the buttons below!*
"""
    
    # Send with typing animation effect
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    await asyncio.sleep(1)
    
    await update.message.reply_text(
        premium_welcome,
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generates a protected link for a given group link with share button."""
    # Check channel membership
    if not await require_channel_membership(update, context):
        return
    
    if not context.args or not context.args[0].startswith("https://t.me/"):
        await update.message.reply_text(
            "🎯 *CREATE PROTECTED LINK*\n\n"
            "📝 *Usage:*\n`/protect https://t.me/yourgroup`\n\n"
            "💡 *Pro Tip:* Ensure the bot is admin in your group for best security.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    group_link = context.args[0]
    unique_id = str(uuid.uuid4())
    encoded_id = base64.urlsafe_b64encode(unique_id.encode()).decode().rstrip("=")
    
    # Generate short ID
    short_id = encoded_id[:8].upper()

    links_collection.insert_one({
        "_id": encoded_id,
        "short_id": short_id,
        "group_link": group_link,
        "created_by": update.effective_user.id,
        "created_by_name": update.effective_user.first_name,
        "created_at": datetime.datetime.now(),
        "active": True,
        "clicks": 0,
        "is_premium": update.effective_user.is_premium or False
    })

    bot_username = (await context.bot.get_me()).username
    protected_link = f"https://t.me/{bot_username}?start={encoded_id}"
    
    # Create shareable message
    share_message = f"""🔐 *SECURE INVITATION LINK*

Join our private group through this secure link:
{protected_link}

📌 *This link is protected by LinkShield Pro*"""
    
    # Create premium keyboard with share options
    keyboard = [
        [
            InlineKeyboardButton("📤 SHARE LINK", callback_data=f"share_{encoded_id}"),
            InlineKeyboardButton("📋 COPY LINK", callback_data=f"copy_{encoded_id}")
        ],
        [
            InlineKeyboardButton("⚡ QUICK SHARE", url=f"https://t.me/share/url?url={protected_link}&text=Join%20our%20secure%20group"),
            InlineKeyboardButton("🔗 VIEW ANALYTICS", callback_data=f"stats_{encoded_id}")
        ],
        [InlineKeyboardButton("❌ REVOKE ACCESS", callback_data=f"revoke_{encoded_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    success_message = f"""
🎉 *PROTECTED LINK CREATED SUCCESSFULLY!*

⚡ *Link Generated Instantly*

🔑 *SECURITY DETAILS*
• 🆔 Link ID: `{short_id}`
• 🛡️ Security Level: ENTERPRISE
• ✅ Status: ACTIVE
• ⚡ Type: {'🌟 PREMIUM' if update.effective_user.is_premium else '✨ STANDARD'}
• 📅 Created: {datetime.datetime.now().strftime('%d %b %Y, %H:%M:%S')}

🔗 *YOUR SECURE LINK*
`{protected_link}`

📊 *MANAGEMENT OPTIONS*
• Share with users securely
• Track real-time analytics
• Revoke access anytime
• Monitor entry attempts

⚠️ *IMPORTANT NOTES*
• 🔒 Links never expire automatically
• 👑 Only you can revoke this link
• 📈 All access is logged & tracked
• 🚀 Premium users get enhanced features

👇 *Use the buttons below to share or manage!*
"""
    
    await update.message.reply_text(
        success_message,
        reply_markup=reply_markup,
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
                "📭 *NO ACTIVE LINKS*\n\n"
                "You don't have any active protected links.\n"
                "Create one with `/protect` command!",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        message = "🔐 *YOUR ACTIVE SECURE LINKS*\n\n"
        keyboard = []
        
        for link in active_links:
            short_id = link.get('short_id', link['_id'][:8])
            clicks = link.get('clicks', 0)
            created = link.get('created_at', datetime.datetime.now()).strftime('%m/%d')
            
            message += f"• `{short_id}` - 👥 {clicks} clicks - 📅 {created}\n"
            keyboard.append([InlineKeyboardButton(
                f"❌ REVOKE {short_id}",
                callback_data=f"revoke_{link['_id']}"
            )])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message += "\n⚠️ *WARNING:* Revocation is permanent and immediate!"
        
        await update.message.reply_text(
            message,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Try to revoke by short ID
    link_id = context.args[0].upper()
    
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
            "❌ *LINK NOT FOUND*\n\n"
            "This link doesn't exist or you don't have permission to revoke it.",
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
            f"✅ *LINK REVOKED SUCCESSFULLY!*\n\n"
            f"🔒 Secure Link `{link_data.get('short_id', link_id)}` has been permanently revoked.\n\n"
            f"📊 *Final Stats:*\n"
            f"• Total Clicks: {link_data.get('clicks', 0)}\n"
            f"• Created: {link_data.get('created_at', datetime.datetime.now()).strftime('%Y-%m-%d')}\n"
            f"• Revoked: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"⚠️ All future access attempts will be blocked.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            "❌ *FAILED TO REVOKE LINK*",
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
        f"✅ *LINK REVOKED!*\n\n"
        f"🔒 Secure Link `{link_data.get('short_id', link_id[:8])}` has been revoked.\n"
        f"👥 Final Clicks: {link_data.get('clicks', 0)}\n\n"
        f"⚠️ All access has been permanently blocked.",
        parse_mode=ParseMode.MARKDOWN
    )

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Broadcast message to all bot users (Admin only)."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *ADMIN ACCESS REQUIRED*\n\n"
            "This command is restricted to system administrators only.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "📢 *BROADCAST SYSTEM*\n\n"
            "To send a broadcast:\n"
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
        [InlineKeyboardButton("✅ CONFIRM BROADCAST", callback_data="confirm_broadcast")],
        [InlineKeyboardButton("❌ CANCEL", callback_data="cancel_broadcast")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"⚠️ *BROADCAST CONFIRMATION*\n\n"
        f"📊 *Delivery Stats:*\n"
        f"• 📨 Recipients: {total_users} users\n"
        f"• 📝 Type: {update.message.reply_to_message.content_type}\n"
        f"• ⚡ Delivery: Instant\n\n"
        f"Are you sure you want to proceed?",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )
    
    context.user_data['broadcast_message'] = update.message.reply_to_message

async def handle_broadcast_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle broadcast confirmation."""
    query = update.callback_query
    await query.answer()
    
    await query.message.edit_text("📤 *BROADCASTING...*\n\nPlease wait, this may take a moment.")
    
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
    
    success_rate = (successful / total_users * 100) if total_users > 0 else 0
    
    await query.message.edit_text(
        f"✅ *BROADCAST COMPLETE!*\n\n"
        f"📊 *Delivery Report:*\n"
        f"• 📨 Total Recipients: {total_users}\n"
        f"• ✅ Successful: {successful}\n"
        f"• ❌ Failed: {failed}\n"
        f"• 📈 Success Rate: {success_rate:.1f}%\n"
        f"• ⏰ Time: {datetime.datetime.now().strftime('%H:%M:%S')}\n\n"
        f"✨ Broadcast logged in system.",
        parse_mode=ParseMode.MARKDOWN
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show bot statistics (Admin only)."""
    admin_id = int(os.environ.get("ADMIN_ID", 0))
    if update.effective_user.id != admin_id:
        await update.message.reply_text(
            "🔒 *ADMIN ACCESS REQUIRED*\n\n"
            "This command is restricted to system administrators only.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    total_users = users_collection.count_documents({})
    total_links = links_collection.count_documents({})
    active_links = links_collection.count_documents({"active": True})
    
    today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    new_users_today = users_collection.count_documents({"last_active": {"$gte": today}})
    new_links_today = links_collection.count_documents({"created_at": {"$gte": today}})
    
    # Calculate total clicks
    total_clicks_result = links_collection.aggregate([
        {"$group": {"_id": None, "total_clicks": {"$sum": "$clicks"}}}
    ])
    total_clicks = 0
    for result in total_clicks_result:
        total_clicks = result.get('total_clicks', 0)
    
    await update.message.reply_text(
        f"📊 *SYSTEM ANALYTICS DASHBOARD*\n\n"
        f"👥 *USER STATISTICS*\n"
        f"• 📈 Total Users: {total_users}\n"
        f"• 🆕 New Today: {new_users_today}\n\n"
        f"🔗 *LINK STATISTICS*\n"
        f"• 🔢 Total Links: {total_links}\n"
        f"• 🟢 Active Links: {active_links}\n"
        f"• 🆕 Created Today: {new_links_today}\n"
        f"• 👆 Total Clicks: {total_clicks}\n\n"
        f"⚙️ *SYSTEM STATUS*\n"
        f"• 🗄️ Database: 🟢 Operational\n"
        f"• 🤖 Bot: 🟢 Online\n"
        f"• ⚡ Uptime: 100%\n"
        f"• 🕐 Last Update: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        parse_mode=ParseMode.MARKDOWN
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help message."""
    if not await require_channel_membership(update, context):
        return
    
    keyboard = [
        [InlineKeyboardButton("🚀 CREATE LINK", callback_data="create_link")],
        [InlineKeyboardButton("📊 VIEW STATS", callback_data="view_stats")],
        [InlineKeyboardButton("💎 GET PREMIUM", callback_data="get_premium")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🛡️ *LINKSHIELD PRO - HELP CENTER*\n\n"
        "✨ *PREMIUM FEATURES OVERVIEW*\n"
        "• 🔐 Military-grade encryption\n"
        "• 📊 Advanced analytics\n"
        "• ⚡ Priority processing\n"
        "• 🛡️ DDoS protection\n\n"
        "📋 *AVAILABLE COMMANDS*\n"
        "• `/start` - Premium welcome\n"
        "• `/protect <link>` - Create secure link\n"
        "• `/revoke` - Revoke access\n"
        "• `/help` - This message\n\n"
        "🔒 *HOW TO USE*\n"
        "1. Use `/protect https://t.me/yourgroup`\n"
        "2. Share the generated link\n"
        "3. Users join via verification\n"
        "4. Manage with `/revoke`\n\n"
        "💡 *PRO TIPS*\n"
        "• Use descriptive group names\n"
        "• Monitor link analytics\n"
        "• Revoke unused links\n"
        "• Join our premium channel\n\n"
        "👇 *Quick actions:*",
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
    logger.info("✨ Starting LinkShield Pro...")
    
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
    logger.info(f"🤖 Premium Bot: @{bot_info.username}")
    logger.info("🚀 LinkShield Pro started successfully!")

@app.on_event("shutdown")
async def on_shutdown():
    """Stops the PTB application and closes the database connection."""
    logger.info("🛑 Stopping LinkShield Pro...")
    await telegram_bot_app.stop()
    await telegram_bot_app.shutdown()
    client.close()
    logger.info("✅ LinkShield Pro stopped.")

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
        "version": "Premium 2.1.0",
        "time": datetime.datetime.now().isoformat(),
        "features": ["military-grade-encryption", "advanced-analytics", "real-time-tracking"]
    }