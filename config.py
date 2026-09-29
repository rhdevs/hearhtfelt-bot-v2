import dataclasses
import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple
from dotenv import load_dotenv

load_dotenv()

# A PURE module (re/unicodedata/typing only) that never imports config, so there is
# no cycle: the name rules stay testable without the bot's state.
from src.supporter_names import NAME_MAX_LENGTH, clean_name, disambiguate, name_key, name_problem

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
    """SHAPE-STABLE ON PURPOSE. `fields` carries every key on the Mongo doc that is
    not a typed attribute or bookkeeping, verbatim, so new profile content needs no
    migration or backfill. DO NOT invent profile content here.

    The name a requester sees is NEVER read off one attribute: it is
    listed_name(profile) -- the chosen-name override if there is a valid one, else
    the automatically captured Telegram first name, else nothing (not listed).
    `blurb` stays in the data and the admin CLI but is NEVER rendered to a requester:
    the picker shows names only."""
    telegram_id: int
    # The CHOSEN-NAME OVERRIDE, set by the supporter's own /name or by the admin CLI
    # `set-profile --display-name`. "" means no override, NOT "not listable".
    display_name: str = ""
    blurb: str = ""                   # data/CLI only; never shown to a requester
    available: bool = True
    has_started_bot: bool = False     # False => the bot may NOT DM them
    username: Optional[str] = None
    # Captured automatically from the supporter's OWN private messages/commands to
    # the bot, refreshed on every one. The listed-name fallback; no admin involved.
    telegram_first_name: str = ""
    fields: Dict[str, Any] = field(default_factory=dict)


# Keys MemberProfile already accounts for, either as a typed attribute or as Mongo
# bookkeeping nobody renders. Everything else on the document lands in `fields`
# verbatim, which is the whole point of D14.
_PROFILE_KEYS = ("telegram_id", "display_name", "blurb", "available",
                 "has_started_bot", "username", "active", "_id",
                 "created_at", "updated_at", "started_bot_at",
                 "telegram_first_name", "telegram_first_name_at", "display_name_set_at")


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
        no profile and therefore no known name: authorized to claim, never listed.
        That is what keeps the pre-Phase-4 suites driving today's un-forked path.
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
            telegram_first_name=str(doc.get('telegram_first_name') or "").strip(),
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

    def set_display_name(self, member_id, display_name) -> bool:
        """In-memory only. The chosen-name override ("" clears it). False when this
        member has no profile."""
        try:
            member_int = int(member_id)
        except (TypeError, ValueError):
            return False
        with self._lock:
            stored = self._profiles.get(member_int)
            if stored is None:
                return False
            stored.display_name = str(display_name)
            return True

    def set_first_name(self, member_id, first_name, create_if_missing: bool = False) -> bool:
        """In-memory only. The captured Telegram first name.

        False when this member has no profile -- unless `create_if_missing` and the
        id IS a member, in which case a minimal profile is created. ONLY the
        private-contact capture passes the flag: a member who just messaged the bot
        privately HAS started it, and that caller wrote Mongo first. It closes the
        window after a registration approval, whose roster.add() creates no profile,
        so the new supporter would otherwise stay unlisted until the next refresh.
        A non-member returns False even with the flag.
        """
        try:
            member_int = int(member_id)
        except (TypeError, ValueError):
            return False
        with self._lock:
            stored = self._profiles.get(member_int)
            if stored is not None:
                stored.telegram_first_name = str(first_name)
                return True
            if not create_if_missing or member_int not in self._members:
                return False
            self._profiles[member_int] = MemberProfile(
                telegram_id=member_int,
                has_started_bot=True,
                telegram_first_name=str(first_name),
            )
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


# --- Self-service supporter registration (/register) ---------------------------
# REGISTRATION_ADMIN_IDS is the ENTIRE authorization surface for a feature that
# grants strangers the ability to read messages from students in crisis. Unset =>
# empty frozenset => the feature is COMPLETELY INERT: /register answers with the
# same neutral string handle_message sends for gibberish, and every rg_* callback
# is refused. Same staged-rollout property as PSS_DIRECTED_ENABLED (R11):
# the code ships, the behaviour does not, until somebody deliberately sets an env
# var and RECREATES the container (this is read once, at import). See runbook R13.
#
# REGISTRATION_ADMINS is a frozenset REBOUND on this module, never mutated. A
# consumer that did `from config import REGISTRATION_ADMINS` would hold the old
# object forever and never see a rebind -- which is exactly why the three
# accessors below exist and why handlers.py imports THEM and not the value. Same
# trick src/bot/restore.py uses with `import config` + config.RESTORE_*.
def _env_admin_ids(name: str) -> Tuple[FrozenSet[int], Tuple[str, ...]]:
    """Parse a comma-separated list of Telegram user ids from the environment.

    Returns (ids, rejected_tokens). Every token must parse as an int AND be
    strictly positive: Telegram USER ids are positive, channel/group ids are
    negative. A negative id here would make the bot DM a CHANNEL the applicant's
    real name, id and username -- precisely the audience this design exists to
    avoid (D33). A bad token is dropped and RETURNED, never silently ignored;
    main.py logs each one at boot, or "why did only one of us get the DM?" is an
    unanswerable question.
    """
    ids = set()
    rejected = []
    for token in os.getenv(name, "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token)
        except (TypeError, ValueError):
            rejected.append(token)
            continue
        if value <= 0:
            rejected.append(token)
            continue
        ids.add(value)
    return frozenset(ids), tuple(rejected)


REGISTRATION_ADMINS, REGISTRATION_ADMIN_ID_ERRORS = _env_admin_ids("REGISTRATION_ADMIN_IDS")

# How long after a decision somebody must wait before /register works again.
REGISTRATION_REAPPLY_HOURS = _env_int("REGISTRATION_REAPPLY_HOURS", 168)   # 7 days


def registration_is_enabled() -> bool:
    """False whenever the allowlist is empty. Inertness gate #1 (D41)."""
    return bool(REGISTRATION_ADMINS)


def is_registration_admin(user_id) -> bool:
    """The ENTIRE authorization check for approving a supporter.

    Reads the module global at CALL time, so a rebind of REGISTRATION_ADMINS is
    seen by handlers.py even though it imported this function by value. Coerces
    with int() in try/except -- the same shape as
    AuthorizedMembersStore.__contains__ -- so a str or None id can never raise
    out of an auth check. An empty allowlist refuses everyone: gate #2 (D40).
    """
    try:
        return int(user_id) in REGISTRATION_ADMINS
    except (TypeError, ValueError):
        return False


def registration_admin_ids() -> List[int]:
    """The fan-out targets, in a DETERMINISTIC order.

    sorted(), never set iteration: the order decides which admin is DMed first,
    and a non-deterministic fan-out makes an intermittent test the only symptom.
    """
    return sorted(REGISTRATION_ADMINS)


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
    # True => this track shows supporters to requesters BY NAME, captures each
    # member's Telegram first name, offers /name and uses the *_named copy. PSS
    # only; HF MUST stay False, which is what keeps HF byte-identical. Last, and
    # defaulted, for the same reason as everything above.
    supporter_names: bool = False

    @property
    def runnable(self) -> bool:
        # A service is only offered/posted-to when enabled AND fully configured with a channel.
        return bool(self.enabled and self.channel_id)


# ROLLOUT SAFETY. Production's PSS roster holds a test account, and names now default
# automatically to each supporter's Telegram first name -- so the old R11 lever ("no
# display_name => invisible in the picker") no longer exists: merging with this True
# would put that account in front of real students at once. PSS_DIRECTED_ENABLED is
# now the ONLY switch, and it is off unless deliberately set. Read at import, so the
# container must be RECREATED (not restarted) for a change to take effect.
PSS_DIRECTED_ENABLED_DEFAULT = False

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
        directed_enabled=_env_bool("PSS_DIRECTED_ENABLED", PSS_DIRECTED_ENABLED_DEFAULT),
        directed_response_minutes=1440,
        picker_page_size=8,
        closing_extra=PSS_CLOSING_NOTE,
        closing_extra_member=PSS_FOLLOWUP_NOTE,
        supporter_names=True,
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

# Registration approval. Dispatched BEFORE the membership gate, with its own and
# strictly narrower gate (is_registration_admin) -- see D40 and handlers.py.
# Longest form is "rg_a:" + uuid4 (36) + ":" + "pss" = 45 bytes; reject is 41.
# Telegram's cap is 64.
# Deep-link payload: t.me/<bot>?start=register. This is the ONLY discovery route
# for /register -- it is deliberately absent from set_my_commands, because that
# menu is what somebody who opened the bot in distress sees.
REGISTER_DEEP_LINK_PAYLOAD = "register"

CB_REG_APPROVE = "rg_a"    # rg_a:<registration_id>:<service_key>
CB_REG_REJECT = "rg_r"     # rg_r:<registration_id>

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

    # --- Supporter names (/name) ---------------------------------------------
    # EVERY value here is SUPPORTER-facing and PLAIN TEXT: they are sent with no
    # parse mode. The *_named variants replace the Phase 4 wording on a track with
    # supporter_names=True, where the list shows everyone and marks the busy ones
    # rather than hiding them. The rejection keys match
    # src/supporter_names.REJECTION_KEYS one for one.
    "member_addendum_named": (
        "You're on a support roster, so you also have:\n"
        "/available - show as free on the list people choose from\n"
        "/unavailable - show as busy on that list\n"
        "/name - see or change the name shown there\n"
        "/release - hand your current conversation back if you can't continue it"
    ),
    "now_available_named": (
        "✅ You're marked as free on the list again, so people asking for support can "
        "choose you."
    ),
    "now_unavailable_named": (
        "✅ You're marked as busy on the list, so nobody new can choose you. Your name "
        "still shows, with (busy) next to it.\n\n"
        "This does NOT end a conversation you're already in, and it does not hand back "
        "a request that has already been sent to you - use the Not right now button on "
        "that request, or /release if the conversation has already started."
    ),
    "availability_needs_name": (
        "Saved. You won't appear on the list people choose from until the bot has a "
        "name it can show for you. Send /name followed by the name you'd like - for "
        "example: /name Sam"
    ),
    "name_current_chosen": (
        "People choosing a peer supporter see you as: {name}\n\n"
        "This is a chosen name. To change it, send /name followed by the new one - for "
        "example: /name Sam\n"
        "To go back to your Telegram first name, send /name reset"
    ),
    "name_current_telegram": (
        "People choosing a peer supporter see you as: {name}\n\n"
        "That's your Telegram first name. To show a different name, send /name "
        "followed by the name you'd like - for example: /name Sam"
    ),
    "name_current_none": (
        "People choosing a peer supporter can't see you on the list yet, because "
        "there's no name the bot can show for you. Send /name followed by the name "
        "you'd like them to see - for example: /name Sam"
    ),
    "name_suffix_note": (
        "Someone else on the list has the same name, so a number is shown after yours "
        "to tell you apart. You can choose a different name with /name."
    ),
    "name_list_off_note": (
        "(People can't choose a supporter by name yet. This is the name they'll see "
        "once they can.)"
    ),
    "name_saved": "✅ Done. People choosing a peer supporter will now see you as: {name}",
    "name_reset_done": (
        "✅ Done. People choosing a peer supporter will now see your Telegram first "
        "name: {name}"
    ),
    "name_reset_done_no_name": (
        "✅ Done. Your chosen name has been removed. The bot can't show your Telegram "
        "first name on the list, so you won't appear there until you choose a name "
        "with /name."
    ),
    "name_reset_nothing": "You don't have a chosen name, so there's nothing to reset.",
    "name_unchanged": "That's already your name on the list, so nothing has changed.",
    "name_taken": (
        "Another peer supporter on the list already goes by that name, or one that "
        "looks almost the same. Please choose a different one - adding an initial "
        "usually works."
    ),
    "name_multiline": "Please keep your name on one line.",
    "name_invisible_chars": (
        "That name contains invisible or formatting characters. Please type it again "
        "using ordinary letters."
    ),
    "name_empty": "Please type the name you'd like after /name - for example: /name Sam",
    "name_too_long": "That name is too long. Please keep it to 32 characters or fewer.",
    "name_has_at": (
        "Names can't contain @, so they can't look like a Telegram username or an "
        "email address."
    ),
    "name_looks_like_link": (
        "Names can't look like a web address. If your name has a full stop in it, put "
        "a space after it."
    ),
    "name_has_phone": (
        "Names can't contain long runs of digits, so they can't look like a phone "
        "number."
    ),
    "name_bad_chars": (
        "Names can only use letters, numbers, spaces, emoji and the punctuation - ' . "
        "Please try again."
    ),
    "name_numeric": (
        "A name can't be just a number - people pick from the list by typing numbers, "
        "so it would be confusing."
    ),
    "name_needs_letter": "Please include at least one letter in your name.",
    "name_reserved": (
        "That's a word the bot uses on its own buttons and messages, so it would be "
        "confusing on the list. Please choose another name."
    ),

    # --- Phase 5: choosing a specific supporter ------------------------------
    #
    # WORDING RULE FOR EVERYTHING THE REQUESTER SEES: a decline and a 24-hour silence
    # are the SAME transition and get the SAME words. Never "declined", never
    # "rejected", never anything that distinguishes the two. The requester learns only
    # that the person they chose is not free. tests/test_directed_requests.py case (o)
    # scans every requester-bound string in this file's flows for regressions.
    #
    # BUSY PEOPLE ARE LISTED, NOT HIDDEN. Everyone on the roster with a usable name
    # appears, numbered; anyone who cannot be asked right now carries the one
    # picker_busy_marker and no button. The marker is the same for every reason --
    # not started the bot, /unavailable, in a conversation, holding a request, or
    # already asked for this one -- and a failed DM reads exactly like "busy" too, so
    # the requester can never work out WHY somebody is not free.
    "comfort_question": (
        "Before I pass this on - would you like to talk to someone in particular, "
        "or is anyone who's free okay?"
    ),
    "comfort_specific_button": "A specific peer supporter",
    "comfort_anyone_button": "Any available peer supporter",

    "picker_header": "I'm comfortable talking to...",
    "picker_hint": "Tap a name below, or reply with its number.",
    "picker_page": "Page {page} of {pages}",
    "picker_anyone_button": "Send to anyone instead",
    "picker_cancel_button": "Cancel",
    "picker_back_button": "Back",
    "picker_next_button": "Next",
    "picker_choose_else_button": "Choose someone else",

    "picker_busy_marker": "(busy)",

    "picker_nobody_free": (
        "Nobody on the list is free right now. You can send your request to anyone "
        "who's free instead - your message goes to the whole support team - or "
        "cancel it."
    ),
    # Shown when we have no record of what this person was last looking at, which is
    # exactly the state after a restart. We re-render rather than guess, because a
    # stale number must never select somebody the requester never saw.
    "picker_lost_view": "Here's the list to choose from.",
    "picker_not_a_number": (
        "Sorry, I didn't catch that. Please tap a name below, or reply with just the "
        "number next to it."
    ),
    # {name} is HTML-escaped by the caller: this is sent with parse_mode HTML. There
    # is deliberately no separate "couldn't reach them" wording -- see above.
    "picker_busy": (
        "{name} isn't free right now. Please choose someone else, or send your "
        "request to anyone instead."
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
        "(You can use /unavailable any time to show as busy on the list.)"
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

    # --- Registration (/register) -------------------------------------------
    # EVERY value here is a plain str. The APPLICANT-facing half is scanned by
    # test_copy.py's blame-word guard, by
    # test_copy.py::test_registration_copy_never_names_a_decider, and by
    # test_registration.py case (m), which asserts the rejection an applicant
    # reads is BYTE-IDENTICAL to the value below. Nothing an applicant reads may
    # name, number or hint at who decided (D44).

    # Applicant-facing ---------------------------------------------------------
    "registration_private_only": (
        "Please send /register to me here, in this private chat."
    ),
    # No timeframe promise and no "who": both would be commitments nobody made.
    "registration_submitted": (
        "Thanks. I've passed your request on to the team. Someone will get back to "
        "you."
    ),
    "registration_already_pending": (
        "You've already asked, and it's with the team. There's nothing more you need "
        "to do."
    ),
    "registration_already_member": (
        "You're already part of the support team. Use /available when you're free to "
        "take a conversation, and /unavailable when you're not."
    ),
    # Deliberately NOT named "..._declined": the key is what maintainers read, and
    # a cooldown after any decision is not a verdict being repeated at somebody.
    "registration_cooldown": (
        "We've already answered a request from you recently. Please give it a little "
        "time before asking again, or speak to the welfare team."
    ),
    # {member} is filled from the SERVICE's member_label, never a literal: HF's is
    # the old brand spelling, and hardcoding it fails
    # test_copy.py::test_no_message_mentions_the_old_brand.
    "registration_approved": (
        "Good news - you've been added to the support team as a {member}. 💚\n\n"
        "You can pick up requests from your team's channel straight away. Your name "
        "won't appear on the list people choose from until someone on the team sets "
        "it up for you.\n\n"
        "Use /available when you're free to take a conversation, /unavailable when "
        "you're not, and /release if you ever need to hand a conversation back."
    ),
    # PSS's approval copy: names now default automatically (no admin step), so this
    # tells the applicant about /name instead of promising someone else will set
    # them up. Kept applicant-facing and decider-free like registration_approved
    # (D44): no 'admin', '@', 'approved by', 'reviewed' or 'decided'. HF still gets
    # registration_approved, untouched, above.
    "registration_approved_named": (
        "Good news - you've been added to the support team as a {member}. 💚\n\n"
        "You can pick up requests from your team's channel straight away. When "
        "people choose a supporter by name, they'll see your Telegram first name - "
        "send /name to see it or to choose a different one.\n\n"
        "Use /available when you're free to take a conversation, /unavailable when "
        "you're not, and /release if you ever need to hand a conversation back."
    ),
    # NEVER interpolated, NEVER .format()ted, sent verbatim. No reason, no name,
    # no id, nothing traceable to a person. See D44 and case (m).
    "registration_rejected": (
        "Thanks for offering to help. We're not able to add you to the support team "
        "at the moment. That says nothing about you as a person, and nothing changes "
        "about using the bot - /chat is always open if you'd like support yourself."
        "\n\nIf you'd like to talk it through, please speak to the welfare team."
    ),
    # Mongo down, no reachable admin, or row creation failed. MUST NOT imply the
    # request is pending. Telling somebody who has just volunteered that it is with
    # the team, when nobody will ever see it, is the worst available outcome (M28).
    "registration_unavailable": (
        "Sorry, I couldn't pass your request on just now, so it hasn't been "
        "submitted. Please try again shortly, or speak to the welfare team directly."
    ),

    # Admin-facing -------------------------------------------------------------
    # Seen ONLY by the ids on REGISTRATION_ADMIN_IDS. These may name the approver;
    # nothing above may. That asymmetry is the whole of D44.
    "registration_card_title": "🆕 Supporter registration request",
    "registration_card_note": (
        "Approving adds them to that roster straight away. On a track where people "
        "choose a supporter by name, they'll be listed under their Telegram first "
        "name once they next message the bot, and can change it themselves with "
        "/name."
    ),
    "registration_approve_button": "✅ Approve as {member}",
    # Admin-facing only. A track with no channel, or switched off, still accepts
    # approvals -- that is how a roster gets built before launch -- but nothing is
    # posted to its queue, so an approved supporter would have nothing to claim.
    # The approver needs to see that; the applicant's copy is deliberately untouched.
    "registration_approve_button_offline": "✅ Approve as {member} (track is off)",
    "registration_reject_button": "🚫 Not now",
    "registration_already_handled": "Someone has already handled this one.",
    "registration_gone": "That request is no longer available.",
    "registration_db_offline": (
        "Our records are offline, so this can't be decided right now. The buttons "
        "still work - please try again in a few minutes."
    ),
    "registration_self_decision": "You can't decide your own request.",
    "registration_cross_roster": (
        "They are already a {member}. Someone would need to take them off that "
        "roster first."
    ),
    "registration_settled_approved": "✅ Approved as {member} by {approver}.",
    "registration_settled_rejected": "🚫 Not approved, by {approver}.",
    "registration_settled_unreachable": (
        "⚠️ They could not be told - the bot cannot message them."
    ),
    "registration_settled_write_failed": (
        "⚠️ The roster write did not confirm. Check with "
        "`admins --action list` before relying on this."
    ),
}


# Words a supporter may not be LISTED as: each would read as the bot's own UI next
# to a number a student types ("3. Cancel", "4. Anyone"), or as an authority.
_RESERVED_NAME_WORDS = (
    'busy', '(busy)', 'free', 'available', 'unavailable', 'anyone', 'anybody', 'any',
    'someone', 'somebody', 'everyone', 'everybody', 'nobody', 'no one', 'none', 'all',
    'cancel', 'back', 'next', 'reset', 'clear', 'remove', 'delete', 'claim', 'accept',
    'decline', 'yes', 'no', 'ok', 'okay', 'admin', 'administrator', 'moderator', 'mod',
    'bot', 'the bot', 'care network', 'care network bot', 'support', 'support team',
    'supporter', 'peer supporter', 'team', 'staff', 'welfare', 'counsellor',
    'counselor', 'help', 'unknown', 'anonymous', 'rhesident',
)

# Derived from MESSAGES and SERVICES, so a new or renamed *_button, member label or
# track name is covered automatically -- there is no second list to forget.
RESERVED_NAME_KEYS: FrozenSet[str] = frozenset(
    k for k in (
        [name_key(w) for w in _RESERVED_NAME_WORDS]
        + [name_key(v) for key, v in MESSAGES.items()
           if key.endswith('_button') and isinstance(v, str)]
        + [name_key(s.member_label) for s in SERVICES.values()]
        + [name_key(s.display_name) for s in SERVICES.values()]
    ) if k
)


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

    # 2. No name a requester could be shown. Names now default to the Telegram
    #    first name, so this means "no valid override AND no usable first name".
    if not listed_name(profile):
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
    """Pickable supporters for a track, in a DETERMINISTIC, STABLE order: by listed
    label, then telegram_id (the listable_supporters order).

    Ordering matters more than it looks: the picker is paginated and the tap arrives
    seconds after the render. Random or dict-insertion order would page-shift a
    supporter between render and tap, and a typed number would then select somebody
    the requester never chose. Never iterate a set here.

    Kept for existing callers and tests; from P3 on the picker uses picker_entries,
    which also lists the busy ones.
    """
    svc = get_service(service_key)
    return [p for p, _label in listable_supporters(svc.key)
            if is_supporter_available(svc.key, p.telegram_id)]


# --- Listed names ----------------------------------------------------------------
# Everything below reads MESSAGES and SERVICES at CALL time.

def supporter_name_problem(raw) -> Optional[str]:
    """name_problem with the bot's own reserved words. The one validator for /name,
    the CLI and listed_name, so what a supporter can set and what gets shown can
    never disagree."""
    return name_problem(raw, RESERVED_NAME_KEYS)


def listed_name(profile: Optional[MemberProfile]) -> str:
    """The name a requester would see for this supporter, before collision numbering.

    "" means NOT LISTED. Precedence: a chosen-name override (display_name), else the
    captured Telegram first name. An override that fails the rules gives "" -- it
    NEVER falls back to the real first name, because somebody who chose an override
    may have done so precisely so that their real name is not shown.
    """
    if profile is None:
        return ""
    override = clean_name(profile.display_name)
    if override:
        return override if supporter_name_problem(override) is None else ""
    auto = clean_name(profile.telegram_first_name)
    # A Telegram first name can run to 64 chars. Shorten rather than drop: the
    # supporter never typed it for us and would not know why they had vanished.
    if len(auto) > NAME_MAX_LENGTH:
        auto = auto[:NAME_MAX_LENGTH].rstrip()
    if auto and supporter_name_problem(auto) is None:
        return auto
    return ""


def supporter_labels(service_key: str) -> Dict[int, str]:
    """member_id -> the label a requester sees (collisions numbered), for every
    roster member with a listed name. Members without one are simply absent."""
    svc = get_service(service_key)
    entries = []
    for p in svc.roster.profiles():
        # roster.remove() leaves the profile behind; membership is the authority.
        if p.telegram_id not in svc.roster:
            continue
        name = listed_name(p)
        if name:
            entries.append((p.telegram_id, name))
    return disambiguate(entries)


def supporter_label(service_key: str, member_id) -> str:
    """This member's label, or "" when unlisted (or the id is unusable)."""
    try:
        member_int = int(member_id)
    except (TypeError, ValueError):
        return ""
    return supporter_labels(service_key).get(member_int, "")


def listable_supporters(service_key: str) -> List[Tuple[MemberProfile, str]]:
    """(profile, label) for every labelled member, busy or not, sorted by
    (label.casefold(), telegram_id). Never iterate a set here -- see
    available_supporters for why the order is load-bearing."""
    svc = get_service(service_key)
    labels = supporter_labels(svc.key)
    out = [(p, labels[p.telegram_id]) for p in svc.roster.profiles()
           if p.telegram_id in labels]
    out.sort(key=lambda pair: (pair[1].casefold(), pair[0].telegram_id))
    return out


def omitted_supporters(service_key: str) -> List[int]:
    """Sorted ids on the roster with NO label (bare ids included), so they can be
    logged: a supporter who expected to be listed must be findable in the logs."""
    svc = get_service(service_key)
    labels = supporter_labels(svc.key)
    return [m for m in svc.roster.snapshot() if m not in labels]


def name_taken_by_other(service_key: str, member_id, name) -> bool:
    """True iff some OTHER roster member is currently LISTED under a name that
    name_key-matches `name`. Only listed names count: another member's invalid raw
    text, or a Telegram name hidden behind their own override, is shown to nobody
    and so cannot be confused with anything."""
    svc = get_service(service_key)
    key = name_key(name)
    try:
        member_int = int(member_id)
    except (TypeError, ValueError):
        member_int = None
    for p in svc.roster.profiles():
        if p.telegram_id == member_int or p.telegram_id not in svc.roster:
            continue
        other = listed_name(p)
        if other and name_key(other) == key:
            return True
    return False


def picker_entries(service_key: str, requester_id=None,
                   declined: Iterable = ()) -> List[Tuple[MemberProfile, str, bool]]:
    """(profile, label, free) for every listable supporter, in listable order.

    Busy people are LISTED with free=False rather than hidden. `free` folds every
    reason into one bit -- not started, /unavailable, in a conversation, holding a
    directed request, waiting themselves, or already asked for this request -- so
    nothing downstream CAN render which reason it was. The requester never sees
    themselves.
    """
    svc = get_service(service_key)
    declined_ids = set()
    for d in declined or ():
        try:
            declined_ids.add(int(d))
        except (TypeError, ValueError):
            continue
    out = []
    for p, label in listable_supporters(svc.key):
        if requester_id is not None and p.telegram_id == requester_id:
            continue
        free = (p.telegram_id not in declined_ids
                and is_supporter_available(svc.key, p.telegram_id))
        out.append((p, label, free))
    return out


def directed_fork_offered(service_key: str, requester_id) -> bool:
    """Whether "a specific peer supporter / anyone" is offered at all: directed mode
    is on for the track AND somebody other than the requester is listable. Busy
    people count -- the list still shows them, with send-to-anyone prominent."""
    svc = get_service(service_key)
    if not svc.directed_enabled:
        return False
    return any(p.telegram_id != requester_id
               for p, _label in listable_supporters(svc.key))


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
