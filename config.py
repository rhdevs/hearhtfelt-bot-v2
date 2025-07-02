import os
from enum import Enum
from dotenv import load_dotenv

load_dotenv()

class UserState(Enum):
    IDLE = "idle"
    WAITING_FOR_DESCRIPTION = "waiting_for_description"
    IN_QUEUE = "in_queue"
    IN_CONVERSATION = "in_conversation"

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHANNEL_ID = os.getenv("ADMIN_CHANNEL_ID")
MONGODB_URI = os.getenv("MONGODB_URI")

HEARTFELT_MEMBERS = [
    1522275008, # tzefoong
    968176987, # lcw
    # Add more authorized heartfelt member Telegram user IDs here
]

active_sessions = {}
user_states = {}
queue_entries = {}
safety_logs = []

# Performance optimization: O(1) lookup indices
user_to_session_map = {}  # user_id -> session_id for fast session lookups
user_to_queue_map = {}    # user_id -> queue_id for fast queue lookups  
queue_order = []          # ordered list of queue_ids for position tracking

QUEUE_EXPIRE_MINUTES = 30

# Session timeout settings
SESSION_TIMEOUT_MINUTES = 10      # Auto-expire sessions after 10 minutes of inactivity
SESSION_WARNING_MINUTES = 5       # Send warning 5 minutes before expiry
SESSION_SWEEP_SECONDS = 180       # Check for expired sessions every 3 minutes

# Session warning tracking (in-memory only)
session_warnings = {}             # session_id -> bool (has warning been sent?)

MESSAGES = {
    "welcome": (
        "Welcome to the Heartfelt Anonymous Helpline! 🤗\n\n"
        "This is a safe, anonymous space where you can speak with our trained support team.\n\n"
        "📋 How it works:\n"
        "1️⃣ Use /help to request support\n"
        "2️⃣ Describe what you need help with\n"
        "3️⃣ You'll be placed in a queue\n"
        "4️⃣ A support member will connect with you anonymously\n"
        "5️⃣ Chat freely - your identity stays private\n"
        "6️⃣ Use /end when you're ready to finish\n\n"
        "🔒 Complete anonymity guaranteed\n"
        "💚 Confidential and judgment-free\n\n"
        "Commands:\n"
        "/help - Request support (start here!)\n"
        "/status - Check your queue position\n"
        "/cancel - Leave the queue if you're waiting\n"
        "/end - End your current conversation"
    ),
    "help_request": "Please describe what you'd like help with. Your message will be shared anonymously with our support team.",
    "queue_added": "Thank you. You've been added to the queue. A support member will be with you shortly.",
    "conversation_started": "A support member has joined the conversation. You can now chat anonymously.",
    "conversation_ended": "The conversation has ended. Thank you for using our service. Take care! 💚",
    "conversation_ended_heartfelt": "This conversation has ended. Thank you for helping someone today! 💚",
    "no_active_conversation": "You don't have an active conversation to end.",
    "already_in_queue": "You're already in the queue. Please wait for a support member to connect with you.",
    "already_in_conversation": "You're already in a conversation. Use /end to finish your current conversation first.",
    "queue_status": "You are currently in the queue, position: {position}. Estimated wait time: {wait_time} minutes.",
    "conversation_status": "You are currently in a conversation with a support member.",
    "idle_status": "You are not currently in a queue or conversation. Use /help to start.",
    "channel_error": "⚠️ Our support system is temporarily unavailable. Please try again in a few minutes. If this continues, our technical team has been notified.",
    "channel_access_denied": "Bot doesn't have permission to access the admin channel. Please contact the administrator.",
    "queue_system_offline": "The queue system is currently offline. Your request has been noted but may experience delays.",
    "queue_cancelled": "✅ You have been removed from the queue. Thank you for considering our support service. You can use /help again anytime if you need assistance.",
    "not_in_queue": "You are not currently in the queue. Use /help to request support or /status to check your current status.",
    "cancel_error": "There was an error removing you from the queue. Please try again or use /status to check your current status.",
    "session_warning": "⏰ Are you still there? This conversation will automatically close in 5 minutes if there's no activity.",
    "session_expired": "⏱️ This conversation has been automatically closed due to inactivity. You can start a new conversation anytime with /help. Take care! 💚",
    "session_expired_heartfelt": "⏱️ This conversation has been automatically closed due to inactivity. Thank you for your time helping someone today! 💚"
}

async def validate_channel_access(bot, channel_id):
    """Validate that bot can access the admin channel"""
    try:
        chat = await bot.get_chat(channel_id)
        return True, f"Connected to: {chat.title}"
    except Exception as e:
        error_msg = str(e).lower()
        if "chat not found" in error_msg:
            return False, "Channel not found - check ADMIN_CHANNEL_ID"
        elif "forbidden" in error_msg or "not enough rights" in error_msg:
            return False, "Bot lacks admin permissions in channel"
        else:
            return False, f"Channel access error: {str(e)}"