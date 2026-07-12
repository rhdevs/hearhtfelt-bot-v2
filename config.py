import os
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional, Set
from dotenv import load_dotenv

load_dotenv()

class UserState(Enum):
    IDLE = "idle"
    WAITING_FOR_SERVICE = "waiting_for_service"   # only entered when >=2 services are runnable
    WAITING_FOR_DESCRIPTION = "waiting_for_description"
    IN_QUEUE = "in_queue"
    IN_CONVERSATION = "in_conversation"

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHANNEL_ID = os.getenv("ADMIN_CHANNEL_ID")
PSS_CHANNEL_ID = os.getenv("PSS_CHANNEL_ID")   # unset in Phase 1 -> None (PSS not runnable)
MONGODB_URI = os.getenv("MONGODB_URI")


class AuthorizedMembersStore:
    """Thread-safe in-memory store for authorized heartfelt members."""

    def __init__(self, initial_members: Iterable[int] = None):
        self._lock = threading.RLock()
        self._members: Set[int] = set()
        self._last_synced_at: Optional[float] = None
        if initial_members:
            self.replace(initial_members)

    @staticmethod
    def _normalize(member_ids: Iterable[int]) -> Set[int]:
        normalized: Set[int] = set()
        for member_id in member_ids:
            try:
                normalized.add(int(member_id))
            except (TypeError, ValueError):
                # Ignore values that cannot be coerced to integers
                continue
        return normalized

    def replace(self, member_ids: Iterable[int]) -> bool:
        """Replace the member set, returning True if the contents changed."""
        new_members = self._normalize(member_ids)
        with self._lock:
            if new_members == self._members:
                return False
            self._members = new_members
            return True

    def update_last_synced(self, timestamp: float) -> None:
        with self._lock:
            self._last_synced_at = timestamp

    def add(self, member_id: int) -> None:
        with self._lock:
            try:
                self._members.add(int(member_id))
            except (TypeError, ValueError):
                pass

    def remove(self, member_id: int) -> None:
        with self._lock:
            try:
                self._members.discard(int(member_id))
            except (TypeError, ValueError):
                pass

    def snapshot(self) -> List[int]:
        with self._lock:
            return sorted(self._members)

    def last_synced_at(self) -> Optional[float]:
        with self._lock:
            return self._last_synced_at

    def __contains__(self, member_id: object) -> bool:
        try:
            member_int = int(member_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        with self._lock:
            return member_int in self._members

    def __len__(self) -> int:
        with self._lock:
            return len(self._members)

    def __iter__(self):
        return iter(self.snapshot())


DEFAULT_HEARTFELT_MEMBERS = [
    1522275008, # tzefoong (rhdevs)
    968176987, # lcw (rhdevs)
    7389740882, # purav (rhdevs)
    802699568, # pauline (rhdevs)
    5645796797, # arushi (hearhtfelt)
    566339809, # eddy (hearhtfelt)
    5206468379, # elysia (hearhtfelt)

    1606899112, # @roguex_07 
    1132094209, # @ang333lyn
    1534532196, # @kwantze
    1082099224, # @wapeull
    5084492192, # @oxqandrea
    7221406953, # @rawchili
    
    534188521, # @evanlimsw (welfare d)
    # Add more authorized heartfelt member Telegram user IDs here
]

DEFAULT_PSS_MEMBERS: List[int] = []   # empty in Phase 1; populated when PSS is enabled


class ServiceType(str, Enum):
    """Support tracks under the umbrella. str-mixin so it serializes to "hf"/"pss" in Mongo."""
    HF = "hf"
    PSS = "pss"


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Service:
    """Configuration for a single support track (e.g. HF or PSS)."""
    key: str
    display_name: str          # chooser button label (only shown when >1 service runnable)
    member_label: str          # requester-facing helper name (used by get_anonymous_name)
    anon_prefix: str           # prefix for the requester's anonymous id
    request_title: str         # header line of the channel queue post
    channel_id: Optional[str]  # queue channel for this service
    members_collection: str    # Mongo collection of authorized members
    default_members: List[int]
    roster: "AuthorizedMembersStore"
    enabled: bool              # explicit on/off flag

    @property
    def runnable(self) -> bool:
        # A service is only offered/posted-to when enabled AND fully configured with a channel.
        return bool(self.enabled and self.channel_id)


SERVICES: Dict[str, Service] = {
    ServiceType.HF.value: Service(
        key=ServiceType.HF.value,
        display_name="Talk to a HF member (friendly listening ear)",
        member_label="Hearhtfelt Member",
        anon_prefix="RHesident",
        request_title="🆘 New Help Request",
        channel_id=ADMIN_CHANNEL_ID,
        members_collection="heartfelt_members",
        default_members=DEFAULT_HEARTFELT_MEMBERS,
        roster=AuthorizedMembersStore(DEFAULT_HEARTFELT_MEMBERS),
        enabled=True,
    ),
    ServiceType.PSS.value: Service(
        key=ServiceType.PSS.value,
        display_name="Talk to a PSS (trained peers who can offer support)",
        member_label="Peer Supporter",
        anon_prefix="RHesident",
        request_title="🆘 New Peer Support Request",
        channel_id=PSS_CHANNEL_ID,                # None in Phase 1 -> runnable == False
        members_collection="peer_supporters",
        default_members=DEFAULT_PSS_MEMBERS,
        roster=AuthorizedMembersStore(DEFAULT_PSS_MEMBERS),
        enabled=_env_bool("PSS_ENABLED", False),  # False in Phase 1
    ),
}


def get_service(key: Optional[str]) -> Service:
    """Resolve a service by key, falling back to HF for unknown/missing keys."""
    return SERVICES.get(key or ServiceType.HF.value, SERVICES[ServiceType.HF.value])


def enabled_services() -> List[Service]:
    """Services that are both enabled and fully configured (have a channel)."""
    return [s for s in SERVICES.values() if s.runnable]


def default_service_key() -> str:
    """The implicit service when no chooser is shown (single runnable service, else HF)."""
    svcs = enabled_services()
    return svcs[0].key if len(svcs) == 1 else ServiceType.HF.value


def is_member_of_service(user_id: int, service_key: str) -> bool:
    """True if the user is in the given service's roster."""
    return user_id in get_service(service_key).roster


def is_any_member(user_id: int) -> bool:
    """True if the user belongs to any enabled service's roster."""
    return any(user_id in s.roster for s in SERVICES.values() if s.enabled)


# Back-compat: HEARTFELT_MEMBERS must be the SAME store instance held in SERVICES["hf"],
# so the existing refresh loop mutates the object the rest of the code reads.
HF_SERVICE = SERVICES[ServiceType.HF.value]
HEARTFELT_MEMBERS = HF_SERVICE.roster

active_sessions = {}
user_states = {}
queue_entries = {}
safety_logs = []

# Performance optimization: O(1) lookup indices
user_to_session_map = {}  # user_id -> session_id for fast session lookups
user_to_queue_map = {}    # user_id -> queue_id for fast queue lookups
queue_order = []          # ordered list of queue_ids for position tracking
user_to_service_map = {}  # user_id -> chosen service key (set at /help or via the chooser)

QUEUE_EXPIRE_MINUTES = 60
AUTHORIZED_MEMBER_REFRESH_SECONDS = 300  # Interval for refreshing Heartfelt members from DB

# Session timeout settings
SESSION_TIMEOUT_MINUTES = 10      # Auto-expire sessions after 10 minutes of inactivity
SESSION_WARNING_MINUTES = 5       # Send warning 5 minutes before expiry
SESSION_SWEEP_SECONDS = 180       # Check for expired sessions every 3 minutes

# Feature flags
PHOTO_SHARING_ENABLED = True      # Allow users to send photos

# Session warning tracking (in-memory only)
session_warnings = {}             # session_id -> bool (has warning been sent?)


MESSAGES = {
    "welcome": (
        "Welcome to the HeaRHtfelt Companion Helpline! 🤗\n\n"
        "This is a safe, anonymous space where you can speak with our hearhtfelt members.\n\n"
        "📋 How it works:\n"
        "1️⃣ Use /help to request support\n"
        "2️⃣ Describe what you need help with\n"
        "3️⃣ You'll be placed in a queue\n"
        "4️⃣ A hearhtfelt member will connect with you anonymously\n"
        "5️⃣ Chat freely - share text, photos, and stickers\n"
        "6️⃣ Use /end when you're ready to finish\n\n"
        "🔒 Complete anonymity guaranteed\n"
        "💚 Confidential and judgment-free\n"
        "📸 Photos and media supported\n\n"
        "Commands:\n"
        "/help - Request support (start here!)\n"
        "/status - Check your queue status\n"
        "/cancel - Leave the queue if you're waiting\n"
        "/end - End your current conversation\n\n"
        "We value your privacy. Please review our full privacy policy here:\n"
        "https://docs.google.com/document/d/1pWvutw151h_sypdttkwEH7hDiBwBdX-qF_xJypffn7Y/edit?usp=sharing"
    ),
    "choose_service": "Which kind of support would you like? Please choose below.",
    "help_request": "Please describe what you'd like help with. Your message will be shared anonymously with our support team. You can use /cancel to cancel.",
    "queue_added": "Thank you. You've been added to the queue. A support member will be with you shortly.",
    "conversation_started": "A support member has joined the conversation. You can now chat anonymously.",
    "conversation_ended": "The conversation has ended. Thank you for using our service. Take care! 💚",
    "conversation_ended_heartfelt": "This conversation has ended. Thank you for helping someone today! 💚",
    "no_active_conversation": "You don't have an active conversation to end.",
    "already_in_queue": "You're already in the queue. Please wait for a support member to connect with you.",
    "member_cancel_before_claim": "You are in a queue. Use /cancel to remove it before claiming another conversation.",
    "already_in_conversation": "You're already in a conversation. Use /end to finish your current conversation first.",
    "queue_status": (
        "You are currently in the queue. We'll notify you as soon as a Hearhtfelt member is available."
    ),
    "conversation_status": "You are currently in a conversation with a support member.",
    "idle_status": "You are not currently in a queue or conversation. Use /help to start.",
    "channel_error": "⚠️ Our support system is temporarily unavailable. Please try again in a few minutes. If this continues, our technical team has been notified.",
    "channel_access_denied": "Bot doesn't have permission to access the admin channel. Please contact the administrator.",
    "queue_system_offline": "The queue system is currently offline. Your request has been noted but may experience delays.",
    "queue_cancelled": "✅ You have been removed from the queue. Thank you for considering our support service. You can use /help again anytime if you need assistance.",
    "help_request_cancelled": (
        "Your help request has been cancelled. You can use /help again anytime when you're ready."
    ),
    "queue_expired": (
        "⏱️ Your place in the queue expired because no Hearhtfelt member was available in time. "
        "You can use /help to join the queue again whenever you're ready."
    ),
    "not_in_queue": "You are not currently in the queue. Use /help to request support or /status to check your current status.",
    "cancel_error": "There was an error removing you from the queue. Please try again or use /status to check your current status.",
    "session_warning": "⏰ Are you still there? This conversation will automatically close in 5 minutes if there's no activity.",
    "session_expired": "⏱️ This conversation has been automatically closed due to inactivity. You can start a new conversation anytime with /help. Take care! 💚",
    "session_expired_heartfelt": "⏱️ This conversation has been automatically closed due to inactivity. Thank you for your time helping someone today! 💚",
    "photo_size_limit": "⚠️ Photo is too large. Please send a smaller image (max 10MB).",
    "photo_error": "❌ Unable to send photo. Please try again or use text instead."
}


def is_heartfelt_member(user_id: int) -> bool:
    """Back-compat shim: True if the user belongs to any enabled service roster.

    In Phase 1 only HF is enabled, so this is exactly the old HF membership check.
    In Phase 2 a PSS supporter is also a "member" here, which is what the end/expiry
    copy ("thank you for helping") wants.
    """
    return is_any_member(user_id)


def get_heartfelt_members() -> List[int]:
    """Return a snapshot list of authorized heartfelt members."""
    return HEARTFELT_MEMBERS.snapshot()

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
