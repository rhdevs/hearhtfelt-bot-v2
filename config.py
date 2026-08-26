import dataclasses
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Set
from dotenv import load_dotenv

load_dotenv()

class UserState(Enum):
    IDLE = "idle"
    WAITING_FOR_SERVICE = "waiting_for_service"   # only entered when >=2 services are runnable
    WAITING_FOR_DESCRIPTION = "waiting_for_description"
    IN_QUEUE = "in_queue"
    IN_CONVERSATION = "in_conversation"
    # The requester is at the supporter picker, or at the "what next?" prompt after a
    # decline or a lapse. A request that has been DMed to one supporter deliberately
    # reuses IN_QUEUE instead -- they ARE waiting -- so this is the only new state.
    CHOOSING_SUPPORTER = "choosing_supporter"

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHANNEL_ID = os.getenv("ADMIN_CHANNEL_ID")
PSS_CHANNEL_ID = os.getenv("PSS_CHANNEL_ID")   # unset in Phase 1 -> None (PSS not runnable)
MONGODB_URI = os.getenv("MONGODB_URI")

# Public document links, not secrets. Env-overridable so a real PSS policy can be
# swapped in without a code deploy; both default to today's umbrella document.
PRIVACY_POLICY_URL = os.getenv(
    "PRIVACY_POLICY_URL",
    "https://docs.google.com/document/d/1pWvutw151h_sypdttkwEH7hDiBwBdX-qF_xJypffn7Y/edit?usp=sharing",
)
HF_PRIVACY_POLICY_URL = os.getenv("HF_PRIVACY_POLICY_URL", PRIVACY_POLICY_URL)
PSS_PRIVACY_POLICY_URL = os.getenv("PSS_PRIVACY_POLICY_URL", PRIVACY_POLICY_URL)

# Per-track follow-up wording, appended to the CLOSING message of a PSS conversation.
# Deliberately EMPTY by default and deliberately NOT written in this repo: inventing
# clinical follow-up instructions for a mental-health service is not a coding
# decision. Empty => today's closing messages are byte-identical. See D30 / runbook R8.
PSS_FOLLOWUP_NOTE = os.getenv("PSS_FOLLOWUP_NOTE", "")   # -> the SUPPORTER's closing
PSS_CLOSING_NOTE  = os.getenv("PSS_CLOSING_NOTE", "")    # -> the REQUESTER's closing


@dataclass
class MemberProfile:
    """SHAPE-STABLE ON PURPOSE. The stakeholder has NOT decided what appears next to
    a supporter's name or who curates it. `blurb` is the one free-text line the picker
    renders today; `fields` carries every OTHER key on the Mongo doc verbatim. When the
    answer arrives it either fills `blurb` or names a key already in `fields` -- one line
    in the renderer, no migration, no backfill. DO NOT invent profile content here."""
    telegram_id: int
    display_name: str = ""            # picker label. EMPTY => not pickable.
    blurb: str = ""
    available: bool = True
    has_started_bot: bool = False     # False => the bot may NOT DM them
    username: Optional[str] = None
    fields: Dict[str, Any] = field(default_factory=dict)


# Keys MemberProfile already accounts for, either as a typed attribute or as Mongo
# bookkeeping nobody renders. Everything else on the document lands in `fields`
# verbatim, which is the whole point of D14.
_PROFILE_KEYS = ("telegram_id", "display_name", "blurb", "available",
                 "has_started_bot", "username", "active", "_id",
                 "created_at", "updated_at", "started_bot_at")


class AuthorizedMembersStore:
    """Thread-safe in-memory store for authorized heartfelt members."""

    def __init__(self, initial_members: Iterable[int] = None):
        self._lock = threading.RLock()
        self._members: Set[int] = set()
        # member_id -> MemberProfile. A SUBSET of _members: a member added via
        # add(), or arriving on a bare-id roster, is authorized but has no
        # profile and is therefore invisible in the picker. That is correct.
        self._profiles: Dict[int, MemberProfile] = {}
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
        """Replace the member set from BARE IDS, returning True if the contents changed.

        A bare-id roster carries no profile data, so every profile for a departed
        member is dropped and no new one is created. Members who arrive this way have
        `display_name == ""`: authorized to claim, invisible in the picker. That is
        what keeps the pre-Phase-4 suites driving today's un-forked code path.
        """
        new_members = self._normalize(member_ids)
        with self._lock:
            if new_members == self._members:
                return False
            self._members = new_members
            self._profiles = {k: v for k, v in self._profiles.items() if k in new_members}
            return True

    @staticmethod
    def _profile_from_doc(doc) -> Optional["MemberProfile"]:
        """Build a MemberProfile from a raw Mongo document, or None if unusable."""
        if not isinstance(doc, dict):
            return None
        try:
            member_int = int(doc.get('telegram_id'))
        except (TypeError, ValueError):
            return None
        return MemberProfile(
            telegram_id=member_int,
            display_name=str(doc.get('display_name') or "").strip(),
            blurb=str(doc.get('blurb') or "").strip(),
            # ABSENT means available: today's profile-less documents must not all
            # read as offline the moment this ships.
            available=bool(doc.get('available', True)),
            # ABSENT means NOT started -- fail closed. Telegram forbids the bot
            # messaging first, so guessing True here strands a real request.
            has_started_bot=bool(doc.get('has_started_bot', False)),
            username=doc.get('username'),
            fields={k: v for k, v in doc.items() if k not in _PROFILE_KEYS},
        )

    def replace_records(self, docs: Iterable[Dict[str, Any]]) -> bool:
        """Replace ids AND profiles from raw Mongo documents.

        An empty list is a legitimate empty roster. Records whose telegram_id will
        not coerce are dropped entirely, so they are not authorized either.
        Duplicate telegram_ids: last wins.
        """
        new_members: Set[int] = set()
        new_profiles: Dict[int, MemberProfile] = {}
        for doc in docs or []:
            profile = self._profile_from_doc(doc)
            if profile is None:
                continue
            new_members.add(profile.telegram_id)
            new_profiles[profile.telegram_id] = profile
        with self._lock:
            if new_members == self._members and new_profiles == self._profiles:
                return False
            self._members = new_members
            self._profiles = new_profiles
            return True

    def profile(self, member_id) -> Optional["MemberProfile"]:
        """A COPY of the stored profile, never the stored object."""
        try:
            member_int = int(member_id)
        except (TypeError, ValueError):
            return None
        with self._lock:
            stored = self._profiles.get(member_int)
            if stored is None:
                return None
            return dataclasses.replace(stored, fields=dict(stored.fields))

    def profiles(self) -> List["MemberProfile"]:
        """Copies of every stored profile, in a DETERMINISTIC, STABLE order.

        The picker is paginated and a tap arrives seconds after the render; dict
        insertion order or set iteration would page-shift a supporter between the
        two, and a typed number would then select the wrong person.
        """
        with self._lock:
            out = [dataclasses.replace(p, fields=dict(p.fields))
                   for p in self._profiles.values()]
        out.sort(key=lambda p: (p.display_name.casefold(), p.telegram_id))
        return out

    def set_available(self, member_id, available: bool) -> bool:
        """In-memory only. False when this member has no profile."""
        try:
            member_int = int(member_id)
        except (TypeError, ValueError):
            return False
        with self._lock:
            stored = self._profiles.get(member_int)
            if stored is None:
                return False
            stored.available = bool(available)
            return True

    def mark_started(self, member_id, started: bool = True) -> bool:
        """In-memory only. False when this member has no profile."""
        try:
            member_int = int(member_id)
        except (TypeError, ValueError):
            return False
        with self._lock:
            stored = self._profiles.get(member_int)
            if stored is None:
                return False
            stored.has_started_bot = bool(started)
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


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# --- Restart durability (see src/bot/restore.py) -------------------------------
# RESTORE_DRY_RUN exists because the FIRST deploy of this feature runs against a
# sessions collection that has been accumulating never-closed pending rows since
# the bot went live. A dry run logs exactly what WOULD be restored, expired,
# notified and silently closed, and touches nothing.
# RESTORE_MAX_* are a circuit breaker: if Mongo hands back more rows than this,
# rehydration aborts entirely rather than messaging dozens of people.
# STALE_NOTIFY_GRACE_MINUTES is the horizon past which "your request expired"
# stops being kind and starts being confusing.
RESTORE_ENABLED = _env_bool("RESTORE_ENABLED", True)
RESTORE_DRY_RUN = _env_bool("RESTORE_DRY_RUN", False)
RESTORE_MAX_PENDING = _env_int("RESTORE_MAX_PENDING", 50)
RESTORE_MAX_ACTIVE = _env_int("RESTORE_MAX_ACTIVE", 50)
STALE_NOTIFY_GRACE_MINUTES = _env_int("STALE_NOTIFY_GRACE_MINUTES", 120)


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

    # Everything below MUST carry a default and stay AFTER `enabled`: every field
    # above is non-default, and a defaulted field before them is a TypeError.

    # Requester-facing copy
    sharing_clause: str = "shared anonymously with our support team"
    privacy_policy_url: str = PRIVACY_POLICY_URL

    # Per-track timers, all in minutes.
    queue_expire_minutes: int = 60      # how long a pending request waits in the channel
    session_timeout_minutes: int = 30   # idle timeout for a claimed conversation
    session_warning_minutes: int = 5    # LEAD TIME before expiry at which we warn,
                                        # i.e. the warning fires at
                                        # (session_timeout_minutes - session_warning_minutes)
                                        # of idleness, NOT at this much idleness.

    # Phase 4/5/6: the directed-support sub-branch. Every one of these carries a
    # default and stays AFTER session_warning_minutes, or a bare Service(...) --
    # which test_service_config.py::test_service_runnable_requires_channel builds --
    # becomes a TypeError.
    directed_enabled: bool = False          # OFF for HF: directed is a PSS feature
    directed_response_minutes: int = 1440   # how long ONE supporter has to answer
    picker_page_size: int = 8
    closing_extra: str = ""                 # appended to the REQUESTER's closing msg
    closing_extra_member: str = ""          # appended to the MEMBER's closing msg

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
        sharing_clause="shared anonymously with our support team",
        privacy_policy_url=HF_PRIVACY_POLICY_URL,
        queue_expire_minutes=60,
        session_timeout_minutes=30,
        session_warning_minutes=5,     # warns at 25 min idle
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
        sharing_clause="shared with our peer student supporters",
        privacy_policy_url=PSS_PRIVACY_POLICY_URL,
        queue_expire_minutes=1440,     # 24h: peer supporters answer on a student timetable
        session_timeout_minutes=1440,  # 24h
        session_warning_minutes=60,    # warns at 23h idle; a 5-min lead on a 24h window
                                       # is unactionable noise at 3am. See D4.
        # Directed support is a PSS feature. HF leaves every one of these at its
        # default, so HF's behaviour is provably unchanged.
        directed_enabled=_env_bool("PSS_DIRECTED_ENABLED", True),
        directed_response_minutes=1440,
        picker_page_size=8,
        closing_extra=PSS_CLOSING_NOTE,
        closing_extra_member=PSS_FOLLOWUP_NOTE,
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
user_to_service_map = {}  # user_id -> chosen service key (set at /chat or via the chooser)

# member_id -> queue_id of the directed request currently sitting with them.
# Rebuilt at boot by src/bot/restore.py. Feeds available_supporters(), which is why
# a supporter holding one request is never offered a second.
directed_by_member: Dict[int, str] = {}

# requester_id -> {'queue_id', 'page', 'ids': [member_id, ...], 'rendered_at'}
# What the requester last SAW, so a typed number maps to the person whose name was
# next to that number. MEMORY ONLY, DELIBERATELY NOT PERSISTED: after a restart
# there is no view, so a typed number is unrecognised and the picker re-renders.
# Persisting it would let a stale number select a supporter the requester never saw.
picker_views: Dict[int, dict] = {}

# Anonymous ids are handed out by BOTH QueueManager and SessionManager. They must draw
# from one set, or a session can be created with an id a queue entry already holds.
used_anonymous_ids: Set[str] = set()

AUTHORIZED_MEMBER_REFRESH_SECONDS = 300  # Interval for refreshing Heartfelt members from DB

# --- callback_data prefixes ----------------------------------------------------
# Telegram caps callback_data at 64 BYTES. The requester's picker callbacks carry no
# session id (a requester has at most one open request, resolved through
# user_to_queue_map and then ownership-checked), which keeps them at <= 17 bytes; the
# supporter's carry one, at 41. Both have headroom.
#
# Defined here as constants so the handlers and the tests cannot drift apart on a
# string literal.
#
# DISPATCH ORDER MATTERS: pk_* is handled BEFORE the membership gate, for the same
# reason svc_ is -- requesters are not members. A member who is themselves a
# requester still works, because every pk_* handler resolves through
# user_to_queue_map[user_id] and then validates that the entry is theirs.
CB_PICK_OPEN = "pk_o"      # "ask anyone who's free"
CB_PICK_LIST = "pk_l"      # pk_l:<page>
CB_PICK_SELECT = "pk_s"    # pk_s:<member_id>
CB_PICK_CANCEL = "pk_x"    # cancel my request
CB_DIRECT_ACCEPT = "dr_a"  # dr_a:<session_id>, targeted supporter only
CB_DIRECT_DECLINE = "dr_d" # dr_d:<session_id>, targeted supporter only

# INVARIANT: SESSION_SWEEP_SECONDS must be < (min service_warning_minutes * 60),
# or a session can expire without ever being warned. Currently 180 < 300 (HF).
# The binding constraint is the SHORTEST warning band, not the longest timeout:
# HF's band is [25 min, 30 min), five minutes wide, and a 180s sweep guarantees at
# least one tick inside it. Lengthening this sweep breaks that guarantee.
SESSION_SWEEP_SECONDS = 180       # Check for expired sessions every 3 minutes

# Deprecated module-level aliases. Kept so external imports don't break; they mirror
# the HF service. New code MUST read these values off a Service.
QUEUE_EXPIRE_MINUTES = SERVICES[ServiceType.HF.value].queue_expire_minutes      # 60
SESSION_TIMEOUT_MINUTES = SERVICES[ServiceType.HF.value].session_timeout_minutes  # 30
SESSION_WARNING_MINUTES = SERVICES[ServiceType.HF.value].session_warning_minutes  # 5

# Feature flags
PHOTO_SHARING_ENABLED = True      # Allow users to send photos

# Session warning tracking (in-memory only)
session_warnings = {}             # session_id -> bool (has warning been sent?)


MESSAGES = {
    "welcome": (
        "Welcome to the Care Network Bot 🤗\n\n"
        "This is a safe, anonymous space where you can talk things through with "
        "someone from the Care Network.\n\n"
        "📋 How it works:\n"
        "1️⃣ Use /chat to request support\n"
        "2️⃣ Describe what you'd like help with\n"
        "3️⃣ You'll be placed in a queue\n"
        "4️⃣ Someone from the Care Network will connect with you anonymously\n"
        "5️⃣ Chat freely - share text, photos, and stickers\n"
        "6️⃣ Use /end when you're ready to finish\n\n"
        "🔒 Complete anonymity guaranteed\n"
        "💚 Confidential and judgment-free\n"
        "📸 Photos and media supported\n\n"
        "Commands:\n"
        "/chat - Request support (start here!)\n"
        "/status - Check your queue status\n"
        "/cancel - Leave the queue if you're waiting\n"
        "/end - End your current conversation\n\n"
        "(/help still works and does exactly the same thing as /chat.)\n\n"
        "We value your privacy. Please review our full privacy policy here:\n"
        f"{PRIVACY_POLICY_URL}"
    ),
    "choose_service": "Which kind of support would you like? Please choose below.",
    # Template: rendered per-track by help_request_text(). Never send it raw.
    "help_request": (
        "Please describe what you'd like help with. "
        "Your message will be {sharing_clause}. "
        "You can use /cancel to cancel.\n\n"
        "Privacy policy: {privacy_url}"
    ),
    "queue_added": "Thank you. You've been added to the queue. A support member will be with you shortly.",
    "conversation_started": "A support member has joined the conversation. You can now chat anonymously.",
    "conversation_ended": "The conversation has ended. Thank you for using the Care Network. Take care! 💚",
    "conversation_ended_heartfelt": "This conversation has ended. Thank you for helping someone today! 💚",
    "no_active_conversation": "You don't have an active conversation to end.",
    "already_in_queue": "You're already in the queue. Please wait for a support member to connect with you.",
    "member_cancel_before_claim": "You are in a queue. Use /cancel to remove it before claiming another conversation.",
    "already_in_conversation": "You're already in a conversation. Use /end to finish your current conversation first.",
    "queue_status": (
        "You are currently in the queue. We'll notify you as soon as a {member} is available."
    ),
    "conversation_status": "You are currently in a conversation with a support member.",
    "idle_status": "You are not currently in a queue or conversation. Use /chat to start.",
    "channel_error": "⚠️ Our support system is temporarily unavailable. Please try again in a few minutes. If this continues, our technical team has been notified.",
    "channel_access_denied": "Bot doesn't have permission to access the admin channel. Please contact the administrator.",
    "queue_system_offline": "The queue system is currently offline. Your request has been noted but may experience delays.",
    "queue_cancelled": "✅ You have been removed from the queue. Thank you for considering our support service. You can use /chat again anytime if you need assistance.",
    "help_request_cancelled": (
        "Your help request has been cancelled. You can use /chat again anytime when you're ready."
    ),
    "queue_expired": (
        "⏱️ Your place in the queue expired because no {member} was available in time. "
        "You can use /chat to join the queue again whenever you're ready."
    ),
    "not_in_queue": "You are not currently in the queue. Use /chat to request support or /status to check your current status.",
    "cancel_error": "There was an error removing you from the queue. Please try again or use /status to check your current status.",
    # Template: rendered per-track by SessionExpiryManager._send_session_warning,
    # which is the ONLY sender. Anything else sending it raw shows a literal {duration}.
    "session_warning": (
        "⏰ Are you still there? This conversation will automatically close in "
        "{duration} if there's no activity."
    ),
    "session_expired": "⏱️ This conversation has been automatically closed due to inactivity. You can start a new conversation anytime with /chat. Take care! 💚",
    "session_expired_heartfelt": "⏱️ This conversation has been automatically closed due to inactivity. Thank you for your time helping someone today! 💚",
    "photo_size_limit": "⚠️ Photo is too large. Please send a smaller image (max 10MB).",
    "photo_error": "❌ Unable to send photo. Please try again or use text instead.",

    # --- Phase 4: supporter availability ------------------------------------
    # The neutral catch-all. handle_message sends it for anything it does not
    # recognise, and the member-only commands send exactly this to a NON-member, so
    # someone typing /available who is not on a roster cannot tell the command exists.
    "unknown_command": (
        "I'm not sure what you mean. Use /chat to start a conversation with a "
        "support member."
    ),
    "member_addendum": (
        "You're on a support roster, so you also have:\n"
        "/available - appear on the list of supporters people can choose\n"
        "/unavailable - stay off that list\n"
        "/release - hand your current conversation back if you can't continue it"
    ),
    "now_available": (
        "✅ You're on the list. People asking for support can choose you again."
    ),
    "now_unavailable": (
        "✅ You're off the list, so nobody new can choose you.\n\n"
        "This does NOT end a conversation you're already in, and it does not hand back "
        "a request that has already been sent to you - use the Not right now button on "
        "that request, or /release if the conversation has already started."
    ),
    "availability_needs_profile": (
        "Saved. You won't appear on the list people choose from until an admin adds "
        "your name to your supporter profile - ask them to set it up."
    ),
    "availability_failed": (
        "⚠️ Sorry, that couldn't be saved just now, so nothing has changed. "
        "Please try again in a few minutes."
    ),
    # Not in the plan's key list, but step 5 of P4.6 requires SAYING the toggle only
    # lasts until restart when Mongo is down, and none of the listed keys can say it.
    "availability_memory_only": (
        "(Our records are offline right now, so this will only last until the bot "
        "next restarts. Please set it again if it seems to have been forgotten.)"
    ),

    # --- Phase 5: choosing a specific supporter ------------------------------
    #
    # WORDING RULE FOR EVERYTHING THE REQUESTER SEES: a decline and a 24-hour silence
    # are the SAME transition and get the SAME words. Never "declined", never
    # "rejected", never anything that distinguishes the two. The requester learns only
    # that the person they chose is not free. tests/test_directed_requests.py case (o)
    # scans every requester-bound string in this file's flows for regressions.
    "comfort_question": (
        "Before I pass this on - would you like to talk to someone in particular, "
        "or is anyone who's free okay?"
    ),
    "comfort_specific_button": "I'd like to choose someone",
    "comfort_anyone_button": "Anyone who's free",

    "picker_header": "I'm comfortable talking to...",
    "picker_hint": "Tap a name below, or reply with its number.",
    "picker_page": "Page {page} of {pages}",
    "picker_anyone_button": "Anyone who's free (usually faster)",
    "picker_cancel_button": "Cancel my request",
    "picker_back_button": "Back",
    "picker_next_button": "Next",
    "picker_choose_else_button": "Choose someone else",

    "picker_nobody_free": (
        "Nobody is free to be chosen right now. You can ask anyone who's free instead - "
        "your message goes to the whole support team - or cancel the request."
    ),
    # Shown when we have no record of what this person was last looking at, which is
    # exactly the state after a restart. We re-render rather than guess, because a
    # stale number must never select somebody the requester never saw.
    "picker_lost_view": "Here's the list again.",
    "picker_not_a_number": (
        "Sorry, I didn't catch that. Please tap a name below, or reply with just the "
        "number next to it."
    ),
    "picker_busy": "That person has just become unavailable. Please choose someone else.",
    "picker_unreachable": (
        "Sorry, I couldn't reach them just now, so they're off the list for this "
        "request. Please choose someone else."
    ),

    "directed_sent": (
        "✅ Sent to {name}. They'll be in touch if they're free.\n\n"
        "If you'd rather not wait, you can ask anyone who's free instead."
    ),
    # Sent to the TARGETED SUPPORTER. HTML parse mode: {description} is escaped by the
    # caller. Carries NO requester name, username, Telegram id or anonymous id -- the
    # anonymous id is withheld until the conversation actually starts.
    "directed_request": (
        "Someone has asked to talk to you.\n\n"
        "\"{description}\"\n\n"
        "They chose you specifically, so this hasn't gone to the channel.\n"
        "If you're free, tap Accept. If not, tap Not right now - the request goes "
        "straight back to them and they can choose again. They are not told why.\n\n"
        "If there's no answer within {window}, it goes back to them automatically.\n"
        "(You can use /unavailable any time to stay off the list.)"
    ),
    "directed_accept_button": "Accept",
    "directed_decline_button": "Not right now",
    "directed_lapsed_member": (
        "That request has gone back to the person who sent it, because there was no "
        "answer. Nothing more is needed from you."
    ),
    "decline_ack": (
        "Thanks for letting us know - it's completely fine to say no. The request has "
        "gone back to the person who sent it so they can choose again, and they "
        "haven't been told anything about who was asked."
    ),
    # THE ONLY wording for both a decline and a 24-hour silence. {name} is the display
    # name the requester already chose, so this carries no new information.
    "directed_unavailable": "{name} isn't free right now.",
    "next_step_question": "What would you like to do?",
    "directed_status": (
        "Your request is with {name}. We'll let you know as soon as they reply. "
        "You can use /cancel if you've changed your mind."
    ),
    "choosing_status": (
        "Your request is open and you're choosing who to talk to. Tap a name on the "
        "list, or use /cancel if you've changed your mind."
    ),
    # queue_expired says "no {member} was available in time", which is false for a
    # request nobody was ever asked about.
    "choosing_expired": (
        "⏱️ Your request has expired because it wasn't sent to anyone in time. "
        "You can use /chat to start again whenever you're ready."
    ),
    "directed_gone": "This request is no longer waiting.",

    # --- Phase 6: ending and handing back -----------------------------------
    "end_is_requester_only": (
        "Only the person who asked for support can end this conversation.\n\n"
        "If you can't carry on right now, use /release instead. Their request goes "
        "back so someone else can pick it up, and they aren't left waiting on a "
        "conversation that has gone quiet."
    ),
    "release_is_member_only": (
        "/release is for supporters handing a conversation back. If you'd like to "
        "finish this conversation, use /end."
    ),
    "release_unavailable": (
        "⚠️ Sorry, /release isn't available right now because our records are "
        "offline, and handing a conversation back needs them. Please try again in a "
        "few minutes, and let the team know if it keeps failing."
    ),
    # Requester-facing. NEVER "you were dropped", NEVER "they left". Nothing was
    # done TO this person; their request simply went back to where it came from.
    "released_to_queue": (
        "This conversation has ended, and your request has gone back to the team. "
        "Someone will be with you as soon as they can. 💚"
    ),
    "released_choose_again": (
        "This conversation has ended, and your request is back with you so you can "
        "choose who to talk to next."
    ),
    "released_member": (
        "Thank you for letting us know - the conversation has been handed back and "
        "you're free again. Passing something on when you can't carry it is the "
        "right call, not a failure. 💚"
    ),
}


def closing_text(service_key: Optional[str], for_member: bool) -> str:
    """The closing message for one side of a conversation, per track.

    The per-track extra is env-supplied and EMPTY by default, deliberately:
    inventing clinical follow-up wording for a mental-health service is not a coding
    decision. Empty => this returns today's message byte-identical. See D30 / R8.
    """
    svc = get_service(service_key)
    base = (MESSAGES["conversation_ended_heartfelt"] if for_member
            else MESSAGES["conversation_ended"])
    extra = svc.closing_extra_member if for_member else svc.closing_extra
    return base + ("\n\n" + extra if extra else "")


def is_supporter_available(service_key: str, member_id: int) -> bool:
    """True iff this member may be offered in, and DMed by, the picker right now.

    Every clause is a reason a directed request would otherwise strand or double up.
    Kept as one predicate so the picker, send_directed_request's re-check and the
    tests all read the same rule -- two copies is how one of them loses a clause.
    """
    svc = get_service(service_key)
    try:
        member_int = int(member_id)
    except (TypeError, ValueError):
        return False

    # 1. Authorized for this track. The roster already excludes active: False.
    if member_int not in svc.roster:
        return False

    profile = svc.roster.profile(member_int)
    if profile is None:
        return False

    # 2. A curated name to show. No name => nothing the picker can render.
    if not profile.display_name:
        return False

    # 3. Telegram forbids a bot messaging a user who has never messaged it. Without
    #    this the DM raises "bot can't initiate conversation" and the request strands
    #    with the requester told nothing.
    if not profile.has_started_bot:
        return False

    # 4. They said /unavailable.
    if not profile.available:
        return False

    # 5. Already in a conversation. Survives a restart via restore._restore_active.
    if member_int in user_to_session_map:
        return False

    # 6. Already holding a directed request. Never stack two on one person.
    if member_int in directed_by_member:
        return False

    # 7. They are themselves waiting for support.
    if member_int in user_to_queue_map:
        return False

    return True


def available_supporters(service_key: str) -> List[MemberProfile]:
    """Pickable supporters for a track, in a DETERMINISTIC, STABLE order.

    Ordering matters more than it looks: the picker is paginated and the tap arrives
    seconds after the render. Random or dict-insertion order would page-shift a
    supporter between render and tap, and a typed number would then select somebody
    the requester never chose. Never iterate a set here.
    """
    svc = get_service(service_key)
    return [p for p in svc.roster.profiles()
            if is_supporter_available(svc.key, p.telegram_id)]


def help_request_text(service_key: Optional[str]) -> str:
    """Per-track description prompt, including that track's privacy policy link."""
    svc = get_service(service_key)
    return MESSAGES["help_request"].format(
        sharing_clause=svc.sharing_clause,
        privacy_url=svc.privacy_policy_url,
    )


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
