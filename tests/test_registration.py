#!/usr/bin/env python3
"""
Self-service supporter registration: /register and the DM approval flow.

Drives the REAL BotHandlers against a recording stub db_mgr whose registration
transitions ENFORCE THEIR FILTERS, so a lost race really does return None the way
Mongo would. If decide_registration returned a document unconditionally, cases
(i), (j) and (k) would all pass while production sent two DMs and wrote two
rosters.

This feature grants somebody the ability to read messages from students in
crisis, so three cases matter more than the rest:

  * case (e) -- an outsider, an HF member, a PSS member and the applicant
    themselves each tap Approve, and all four are refused. Being on a roster is
    NOT approval authority.
  * case (i) -- two admins tapping Approve at the same instant produce exactly
    ONE roster write and exactly ONE message to the applicant.
  * case (m) -- nothing the applicant ever reads names, numbers or hints at who
    decided, and the rejection is byte-identical to MESSAGES.

Run directly: `python tests/test_registration.py`
"""

import asyncio
import contextlib
import datetime
import io
import logging
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymongo import ReturnDocument

import config
from config import MESSAGES, ServiceType
from src.timeutil import UTC, utcnow

import main
import src.bot.handlers as handlers_mod
import src.bot.managers.expiry as expiry_mod
import src.bot.managers.queue as queue_mod
import src.bot.managers.session as session_mod
import src.bot.restore as restore_mod
import src.database.manager as db_manager_mod
import src.database.utils as utils_mod
from src.bot.handlers import BotHandlers
from src.bot.managers.queue import QueueManager
from src.bot.managers.session import SessionManager

A1 = 7001          # registration admin, on no roster at all
A2 = 7002          # the second registration admin, likewise
APPLICANT = 7100
OUTSIDER = 7200    # nobody: not an admin, not on a roster
HF_MEMBER = 7300   # on the HF roster, and therefore NOT an approver
PSS_MEMBER = 7400  # on the PSS roster, and therefore NOT an approver

HF_CHANNEL = "-100HF"
PSS_CHANNEL = "-100PSS"

HF_COLL = "heartfelt_members"
PSS_COLL = "peer_supporters"

# Substrings that would betray WHO decided, or that a decision was reviewed by a
# person at all. Scanned against every string the APPLICANT can read. "@" is in
# here because a Telegram handle is the most likely accidental leak.
DECIDER_MARKERS = ("approved by", "rejected by", "declined by", "reviewed",
                   "admin", "decided")

# The approver's Telegram identity in the tests that run the leak scan.
# Deliberately distinctive: a generic name like "Mem" is a substring of "member"
# and would make the scan pass for the wrong reason.
APPROVER_FIRST = "Zephyrine"
APPROVER_USER = "zephyrhandle"


# --------------------------------------------------------------------------- stubs
class StubDB:
    """Recording stand-in for db_mgr.

    EVERY registration transition enforces the same filter its real counterpart
    does. decide_registration and close_registration return None unless the row
    is still 'pending', because that single clause is the entire exactly-once
    guarantee -- see case (i).
    """

    def __init__(self, db_available=True):
        self.db_available = db_available
        self.calls = []
        self.regs = {}       # registration_id -> doc
        self.members = {}    # collection name -> {telegram_id -> doc}
        self._frozen = {}    # registration_id -> the snapshot every read returns

    @staticmethod
    def _sort_key(doc, field):
        """Mongo's .sort() orders mixed BSON types and NEVER raises. Python's
        sort() raises on datetime-vs-str, so a stub that sorted naively would
        invent a failure Mongo cannot produce -- and case (ab) deliberately
        stores a malformed created_at."""
        from src.timeutil import ensure_aware_utc
        return ensure_aware_utc(doc.get(field)) or datetime.datetime.min.replace(
            tzinfo=UTC)

    # --- registration ------------------------------------------------------
    def create_registration(self, telegram_id, username=None, first_name=None,
                            last_name=None, attempt=1):
        self.calls.append(('create_registration', int(telegram_id), int(attempt)))
        if not self.db_available:
            return None
        # A real uuid4, so the callback_data length assertions in case (d) are
        # measured against production-sized ids rather than short test ones.
        registration_id = str(uuid.uuid4())
        now = utcnow()
        self.regs[registration_id] = {
            'registration_id': registration_id,
            'telegram_id': int(telegram_id),
            'username': username,
            'first_name': first_name,
            'last_name': last_name,
            'status': 'pending',
            'created_at': now,
            'updated_at': now,
            'decided_at': None,
            'decided_by': None,
            'decision_service': None,
            'close_reason': None,
            'admin_messages': [],
            'notified_admin_ids': [],
            'attempt': int(attempt),
        }
        return registration_id

    def freeze_read(self, registration_id):
        """Pin what get_registration returns, modelling two readers whose reads
        both landed before either wrote. See case (i) for why that is the only
        way to make two callers genuinely reach the atomic gate."""
        self._frozen[registration_id] = dict(self.regs[registration_id])

    def get_registration(self, registration_id):
        self.calls.append(('get_registration', registration_id))
        if not self.db_available:
            return None
        if registration_id in self._frozen:
            return dict(self._frozen[registration_id])
        doc = self.regs.get(registration_id)
        # A COPY, like a real Mongo read: the handler must never be able to
        # mutate stored state by editing what it was handed.
        return dict(doc) if doc is not None else None

    def get_pending_registration(self, telegram_id):
        self.calls.append(('get_pending_registration', int(telegram_id)))
        if not self.db_available:
            return None
        rows = [d for d in self.regs.values()
                if d['telegram_id'] == int(telegram_id) and d['status'] == 'pending']
        rows.sort(key=lambda d: self._sort_key(d, 'created_at'), reverse=True)
        return dict(rows[0]) if rows else None

    def get_last_decided_registration(self, telegram_id):
        self.calls.append(('get_last_decided_registration', int(telegram_id)))
        if not self.db_available:
            return None
        rows = [d for d in self.regs.values()
                if d['telegram_id'] == int(telegram_id)
                and d['status'] in ('approved', 'rejected')]
        rows.sort(key=lambda d: self._sort_key(d, 'decided_at'), reverse=True)
        return dict(rows[0]) if rows else None

    def count_registrations(self, telegram_id):
        self.calls.append(('count_registrations', int(telegram_id)))
        if not self.db_available:
            return 0
        return len([d for d in self.regs.values()
                    if d['telegram_id'] == int(telegram_id)])

    def set_registration_admin_messages(self, registration_id, delivered):
        self.calls.append(('set_registration_admin_messages', registration_id,
                           [(d['admin_id'], d['message_id']) for d in delivered]))
        if not self.db_available:
            return False
        doc = self.regs.get(registration_id)
        if doc is None:
            return False
        # Deliberately NOT filtered on status, exactly like the real one: the DMs
        # physically exist whatever the row now says.
        doc['admin_messages'] = [dict(d) for d in delivered]
        doc['notified_admin_ids'] = [d['admin_id'] for d in delivered]
        doc['updated_at'] = utcnow()
        return True

    def decide_registration(self, registration_id, admin_id, decision,
                            service_key=None):
        self.calls.append(('decide_registration', registration_id, int(admin_id),
                           decision, service_key))
        if decision not in ('approved', 'rejected'):
            return None
        if not self.db_available:
            return None
        doc = self.regs.get(registration_id)
        # THE FILTER. Without this clause cases (i), (j) and (k) pass while
        # production writes two rosters and sends two DMs.
        if doc is None or doc['status'] != 'pending':
            return None
        now = utcnow()
        doc.update(status=decision, decided_by=int(admin_id), decided_at=now,
                   decision_service=service_key, updated_at=now)
        return dict(doc)

    def close_registration(self, registration_id, reason):
        self.calls.append(('close_registration', registration_id, reason))
        if not self.db_available:
            return None
        doc = self.regs.get(registration_id)
        if doc is None or doc['status'] != 'pending':
            return None
        doc.update(status='closed', close_reason=reason, updated_at=utcnow())
        return dict(doc)

    def list_registrations(self, status=None, limit=50):
        self.calls.append(('list_registrations', status, limit))
        if not self.db_available:
            return []
        rows = [d for d in self.regs.values()
                if status is None or d['status'] == status]
        rows.sort(key=lambda d: self._sort_key(d, 'created_at'), reverse=True)
        return [dict(d) for d in rows[:limit]]

    # --- member roster -----------------------------------------------------
    def add_authorized_member(self, member_id, username=None, active=True,
                              collection=None):
        self.calls.append(('add_authorized_member', int(member_id), collection,
                           bool(active)))
        if not self.db_available:
            return False
        bucket = self.members.setdefault(collection, {})
        doc = bucket.setdefault(int(member_id), {'telegram_id': int(member_id)})
        doc['active'] = bool(active)
        if username:
            doc['username'] = username
        return True

    def mark_member_started(self, member_id, started=True, collection=None):
        self.calls.append(('mark_member_started', int(member_id), bool(started),
                           collection))
        if not self.db_available:
            return False
        bucket = self.members.setdefault(collection, {})
        doc = bucket.get(int(member_id))
        # No upsert, exactly like the real one: with no document there is nothing
        # to set, which is why the handler must add the member FIRST.
        if doc is None:
            return False
        doc['has_started_bot'] = bool(started)
        return True

    def get_member_profile_doc(self, member_id, collection=None):
        self.calls.append(('get_member_profile_doc', int(member_id), collection))
        if not self.db_available:
            return None
        doc = self.members.get(collection, {}).get(int(member_id))
        return dict(doc) if doc is not None else None

    def get_authorized_member_records(self, include_inactive=False, collection=None):
        self.calls.append(('get_authorized_member_records', include_inactive,
                           collection))
        if not self.db_available:
            return None
        docs = self.members.get(collection, {}).values()
        if not include_inactive:
            docs = [d for d in docs if d.get('active', True)]
        return [dict(d) for d in docs]

    def set_member_availability(self, member_id, available, collection=None):
        self.calls.append(('set_member_availability', int(member_id),
                           bool(available), collection))
        return True

    # --- assertion helpers -------------------------------------------------
    def called(self, method):
        return [c for c in self.calls if c[0] == method]

    def count(self, method):
        return len(self.called(method))


class FakeBot:
    def __init__(self):
        self.sent = []      # (chat_id, text, reply_markup)
        self.edited = []    # (chat_id, message_id, text, reply_markup)
        self.fail_for = {}  # chat_id (str) -> exception message
        self._mid = 0

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if str(chat_id) in self.fail_for:
            raise RuntimeError(self.fail_for[str(chat_id)])
        self._mid += 1
        self.sent.append((str(chat_id), text, reply_markup))
        return SimpleNamespace(message_id=self._mid)

    async def edit_message_text(self, chat_id=None, message_id=None, text=None,
                                reply_markup=None, **kw):
        if str(chat_id) in self.fail_for:
            raise RuntimeError(self.fail_for[str(chat_id)])
        self.edited.append((str(chat_id), message_id, text, reply_markup))
        return None

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, **kw):
        return None

    async def delete_message(self, chat_id=None, message_id=None, **kw):
        return None

    def texts_to(self, user_id):
        return [t for c, t, _m in self.sent if c == str(user_id)]

    def markups_to(self, user_id):
        return [m for c, _t, m in self.sent if c == str(user_id)]

    def edits_to(self, user_id):
        return [(mid, t, m) for c, mid, t, m in self.edited if c == str(user_id)]


class Rec:
    def __init__(self):
        self.replies = []       # (text, reply_markup) sent back to the acting user
        self.answers = []       # (text, show_alert)
        self.markup_strips = [] # every edit_message_reply_markup on the tapped card

    def answer_texts(self):
        return [t for t, _alert in self.answers]


def text_update(user_id, text, rec, username=None, first_name="User",
                last_name=None, chat_type="private", with_chat=True):
    """A message update. with_chat=False builds an update with NO effective_chat
    at all -- the shape _is_private_chat must fail CLOSED on."""
    async def reply_text(t, reply_markup=None, **kw):
        rec.replies.append((t, reply_markup))

    user = SimpleNamespace(id=user_id, username=username, first_name=first_name,
                           last_name=last_name)
    msg = SimpleNamespace(text=text, reply_text=reply_text, photo=[], sticker=None)
    fields = dict(effective_user=user, message=msg, callback_query=None)
    if with_chat:
        fields['effective_chat'] = SimpleNamespace(id=user_id, type=chat_type)
    return SimpleNamespace(**fields)


def cb_update(user_id, data, rec, username=APPROVER_USER, first=APPROVER_FIRST):
    async def answer(t=None, show_alert=False):
        rec.answers.append((t, show_alert))

    async def edit_message_text(t, reply_markup=None, **kw):
        rec.replies.append((t, reply_markup))

    async def edit_message_reply_markup(reply_markup=None, **kw):
        rec.markup_strips.append(reply_markup)

    fu = SimpleNamespace(id=user_id, username=username, first_name=first,
                         last_name=None)
    q = SimpleNamespace(from_user=fu, data=data, answer=answer,
                        edit_message_text=edit_message_text,
                        edit_message_reply_markup=edit_message_reply_markup)
    return SimpleNamespace(effective_user=fu, message=None, callback_query=q,
                           effective_chat=SimpleNamespace(id=user_id, type="private"))


class LogCatcher(logging.Handler):
    """Collects records at or above `level` so a case can assert an ERROR was
    actually emitted. Not a unittest fixture -- this suite has no TestCase."""

    def __init__(self, level=logging.ERROR):
        logging.Handler.__init__(self, level)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self):
        return [r.getMessage() for r in self.records]


@contextlib.contextmanager
def catching(level=logging.ERROR):
    catcher = LogCatcher(level)
    root = logging.getLogger()
    previous = root.level
    root.addHandler(catcher)
    root.setLevel(min(previous, level))
    try:
        yield catcher
    finally:
        root.removeHandler(catcher)
        root.setLevel(previous)


# --------------------------------------------------------------------------- fixtures
def reset_state():
    for d in (config.user_states, config.user_to_service_map, config.queue_entries,
              config.user_to_queue_map, config.active_sessions,
              config.user_to_session_map, config.session_warnings,
              config.directed_by_member, config.picker_views):
        d.clear()
    config.queue_order.clear()
    config.used_anonymous_ids.clear()
    config.safety_logs.clear()
    # Registration is OFF by default and every pre-existing suite must run with it
    # off. This is a REBIND, not a .clear(): REGISTRATION_ADMINS is a frozenset, so
    # any consumer that imported it by value would not see this -- which is exactly
    # why handlers.py goes through config.is_registration_admin() instead. It also
    # makes this suite deterministic regardless of a maintainer's local .env, which
    # config.py reads via load_dotenv() at import.
    config.REGISTRATION_ADMINS = frozenset()


def install(stub):
    handlers_mod.db_mgr = stub
    queue_mod.db_mgr = stub
    session_mod.db_mgr = stub
    restore_mod.db_mgr = stub
    expiry_mod.db_mgr = stub
    main.db_mgr = stub
    utils_mod.db_mgr = stub


def build(stub, bot):
    """Fresh managers over an existing stub -- case (w) uses this to simulate a
    restart against the same Mongo."""
    return BotHandlers(SessionManager(bot), QueueManager(bot))


def setup(db_available=True, admins=(A1, A2), hf_members=(), pss_members=()):
    """Both tracks configured and enabled, empty rosters, allowlist = admins."""
    reset_state()
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]
    hf.channel_id, hf.enabled = HF_CHANNEL, True
    pss.channel_id, pss.enabled = PSS_CHANNEL, True
    # replace_records([]) FIRST: replace() with the same id set is a no-op that
    # KEEPS existing profiles, so profile data from an earlier case would leak.
    hf.roster.replace_records([])
    pss.roster.replace_records([])
    hf.roster.replace(hf_members)
    pss.roster.replace(pss_members)

    config.REGISTRATION_ADMINS = frozenset(admins)

    stub = StubDB(db_available=db_available)
    install(stub)
    bot = FakeBot()
    return bot, build(stub, bot), stub


def ctx_for(bot):
    return SimpleNamespace(bot=bot)


def services():
    return (config.SERVICES[ServiceType.HF.value],
            config.SERVICES[ServiceType.PSS.value])


def sole_registration(stub):
    assert len(stub.regs) == 1, "expected exactly one registration row, got %r" % (
        sorted(stub.regs),)
    return list(stub.regs.values())[0]


async def do_register(handlers, bot, user_id=APPLICANT, **kw):
    rec = Rec()
    await handlers.register_command(
        text_update(user_id, "/register", rec, **kw), ctx_for(bot))
    return rec


async def tap(handlers, bot, admin_id, data, **kw):
    """Go through the REAL dispatcher, not straight to the private callback, so
    every case also proves the rg_* branch is reachable and correctly placed."""
    rec = Rec()
    await handlers.handle_callback_query(
        cb_update(admin_id, data, rec, **kw), ctx_for(bot))
    return rec


def approve_data(registration_id, service_key):
    return "%s:%s:%s" % (config.CB_REG_APPROVE, registration_id, service_key)


def reject_data(registration_id):
    return "%s:%s" % (config.CB_REG_REJECT, registration_id)


def assert_no_decider_leak(bot, extra_names=(APPROVER_FIRST, APPROVER_USER)):
    """Nothing the APPLICANT reads may name, number or hint at who decided.

    Run from every case that sends the applicant anything, not only case (m):
    one leaked handle in one branch is the whole failure, and the branch that
    leaks is the one nobody thought to check.

    SELF-GUARDING. Every one of this helper's call sites is on a path where a
    decision was made and the applicant was therefore messaged, so an empty
    list means the notification was lost, not that the scan is clean. Without
    this assertion the entire scan below degrades to a silent no-op in exactly
    the situation where somebody most needs to be told it broke.
    """
    assert bot.texts_to(APPLICANT), (
        "the leak scan was handed nothing to scan; a decision was made, so the "
        "applicant must have been messaged: %r" % (bot.sent,))
    for text in bot.texts_to(APPLICANT):
        assert "@" not in text, (
            "an applicant-bound message contains '@', which is how a Telegram "
            "handle leaks: %r" % text)
        lowered = text.lower()
        for marker in DECIDER_MARKERS:
            assert marker not in lowered, (
                "an applicant-bound message contains %r, which tells them a "
                "person reviewed them and invites the question 'who?': %r"
                % (marker, text))
        for admin_id in (A1, A2):
            assert str(admin_id) not in text, (
                "an applicant-bound message contains the id of an approver: %r"
                % text)
        for name in extra_names:
            assert name.lower() not in lowered, (
                "an applicant-bound message contains the approver's name %r: %r"
                % (name, text))


# --------------------------------------------------------------------------- cases
async def case_a_an_empty_allowlist_makes_register_indistinguishable_from_nothing():
    bot, handlers, stub = setup(admins=())
    rec = await do_register(handlers, bot)

    assert len(rec.replies) == 1, rec.replies
    assert rec.replies[0][0] == MESSAGES["unknown_command"], (
        "an inert /register must answer with the EXACT string handle_message "
        "sends for gibberish, or the command advertises itself: %r"
        % (rec.replies[0][0],))
    assert stub.calls == [], (
        "an inert /register must not touch the database at all: %r" % (stub.calls,))
    assert bot.sent == [], "an inert /register must send nothing: %r" % (bot.sent,)
    print("OK  a. an empty allowlist makes /register indistinguishable from nothing")


async def case_b_an_inert_bot_refuses_every_registration_tap():
    """Gate #2. The same empty frozenset that silences /register also refuses
    every rg_* callback -- including one from an id that WOULD be an admin."""
    bot, handlers, stub = setup(admins=())
    fake_rid = str(uuid.uuid4())

    for tapper in (OUTSIDER, A1, A2):
        rec = await tap(handlers, bot, tapper, approve_data(fake_rid, "pss"))
        assert rec.answers == [("You are not authorized to perform this action.",
                                True)], (tapper, rec.answers)
        rec = await tap(handlers, bot, tapper, reject_data(fake_rid))
        assert rec.answers == [("You are not authorized to perform this action.",
                                True)], (tapper, rec.answers)

    assert stub.count('decide_registration') == 0, stub.calls
    assert bot.sent == [], bot.sent
    print("OK  b. an inert bot refuses every registration tap, including from a "
          "would-be admin")


async def case_c_a_registration_reaches_every_admin_and_is_recorded():
    bot, handlers, stub = setup()
    rec = await do_register(handlers, bot, username="hopeful")

    doc = sole_registration(stub)
    assert doc['status'] == 'pending', doc
    assert doc['attempt'] == 1, doc
    assert doc['telegram_id'] == APPLICANT, doc

    assert sorted(c for c, _t, _m in bot.sent) == [str(A1), str(A2)], (
        "exactly one card per admin: %r" % (bot.sent,))
    assert [t for t, _m in rec.replies] == [MESSAGES["registration_submitted"]], \
        rec.replies

    recorded = stub.called('set_registration_admin_messages')
    assert len(recorded) == 1, recorded
    assert [pair[0] for pair in recorded[0][2]] == [A1, A2], (
        "both admin/message pairs must be persisted, in the deterministic "
        "sorted fan-out order: %r" % (recorded,))
    assert [p['admin_id'] for p in doc['admin_messages']] == [A1, A2], doc
    print("OK  c. a registration reaches every admin and both cards are recorded")


async def case_d_the_card_and_its_buttons_are_well_formed():
    bot, handlers, stub = setup()
    await do_register(handlers, bot, username="hopeful", first_name="Ada",
                      last_name="Lovelace")

    doc = sole_registration(stub)
    card = bot.texts_to(A1)[0]
    assert str(APPLICANT) in card, card
    assert "@hopeful" in card, card
    assert "Ada Lovelace" in card, card
    assert MESSAGES["registration_card_note"] in card, card
    assert "@@" not in card, "the '@' must be added only when absent: %r" % card

    markup = bot.markups_to(A1)[0]
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert datas == [approve_data(doc['registration_id'], "hf"),
                     approve_data(doc['registration_id'], "pss"),
                     reject_data(doc['registration_id'])], datas
    assert len(datas) == len(set(datas)), "a service must not appear twice: %r" % datas
    for data in datas:
        size = len(data.encode("utf-8"))
        assert size <= 64, (
            "Telegram caps callback_data at 64 BYTES and silently refuses the "
            "whole keyboard above it: %r is %d" % (data, size))
    print("OK  d. the card and its buttons are well formed and inside the 64-byte cap")


async def case_e_no_roster_membership_grants_any_approval_power():
    """THE AUTHORIZATION PROOF.

    Four people who are emphatically not on the allowlist tap Approve: a total
    stranger, an HF companion, a PSS supporter, and the applicant themselves.
    Being trusted to CLAIM a conversation is not the same privilege as granting
    somebody else the ability to read one, and after this change no roster
    membership grants any approval power at all.
    """
    bot, handlers, stub = setup(hf_members=(HF_MEMBER,), pss_members=(PSS_MEMBER,))
    await do_register(handlers, bot)
    doc = sole_registration(stub)
    hf, pss = services()

    for tapper in (OUTSIDER, HF_MEMBER, PSS_MEMBER, APPLICANT):
        rec = await tap(handlers, bot, tapper,
                        approve_data(doc['registration_id'], "pss"))
        assert rec.answers == [("You are not authorized to perform this action.",
                                True)], (tapper, rec.answers)

    assert stub.count('decide_registration') == 0, (
        "not one of them may reach the gate: %r" % (stub.calls,))
    assert stub.count('add_authorized_member') == 0, stub.calls
    assert APPLICANT not in pss.roster and APPLICANT not in hf.roster, (
        "no roster may have changed")
    assert list(hf.roster) == [HF_MEMBER] and list(pss.roster) == [PSS_MEMBER], (
        list(hf.roster), list(pss.roster))
    assert bot.texts_to(APPLICANT) == [], (
        "the applicant must hear nothing at all: %r" % (bot.texts_to(APPLICANT),))
    assert doc['status'] == 'pending', doc
    print("OK  e. no roster membership grants any approval power")


async def case_f_an_admin_on_no_roster_can_still_approve():
    """The other half of case (e), and the reason the rg_* branch sits BEFORE the
    is_any_member gate: the owner and the welfare director may be neither an HF
    nor a PSS member, and the gate would refuse the only people entitled to
    decide."""
    bot, handlers, stub = setup()
    hf, pss = services()
    assert A1 not in hf.roster and A1 not in pss.roster, (
        "this case is only meaningful while the approver is on no roster")
    assert not config.is_any_member(A1), "A1 must fail the membership gate"

    await do_register(handlers, bot)
    doc = sole_registration(stub)
    rec = await tap(handlers, bot, A1, approve_data(doc['registration_id'], "pss"))

    assert rec.answers == [(None, False)], (
        "the winner gets a plain ack, not an alert: %r" % (rec.answers,))
    assert doc['status'] == 'approved', doc
    assert APPLICANT in pss.roster
    assert_no_decider_leak(bot)
    print("OK  f. an admin on no roster can still approve (dispatch is before the "
          "membership gate)")


async def case_g_approving_into_pss_writes_pss_and_settles_every_card():
    bot, handlers, stub = setup()
    await do_register(handlers, bot, username="hopeful")
    doc = sole_registration(stub)
    rid = doc['registration_id']
    hf, pss = services()

    await tap(handlers, bot, A1, approve_data(rid, "pss"))

    decisions = stub.called('decide_registration')
    assert len(decisions) == 1 and decisions[0][3] == 'approved' \
        and decisions[0][4] == 'pss', decisions

    adds = stub.called('add_authorized_member')
    assert [a[2] for a in adds] == [PSS_COLL], adds
    starts = [c for c in stub.called('mark_member_started')
              if c[1] == APPLICANT and c[3] == PSS_COLL]
    assert starts and starts[0][2] is True, (
        "has_started_bot must be recorded on approval -- /register IS a private "
        "message from them: %r" % (stub.called('mark_member_started'),))
    assert APPLICANT in pss.roster and APPLICANT not in hf.roster

    assert bot.texts_to(APPLICANT) == [
        MESSAGES["registration_approved"].format(member=pss.member_label)], \
        bot.texts_to(APPLICANT)

    settled = [(c, m) for c, _mid, _t, m in bot.edited]
    assert sorted(c for c, _m in settled) == [str(A1), str(A2)], (
        "EVERY admin's card is settled, including the decider's own: %r"
        % (bot.edited,))
    for chat, markup in settled:
        assert markup is None, (
            "a settled card must have its buttons stripped: %r" % (chat,))
    for _c, _mid, text, _m in bot.edited:
        assert MESSAGES["registration_settled_approved"].format(
            member=pss.member_label, approver=APPROVER_FIRST) in text, text
    assert_no_decider_leak(bot)
    print("OK  g. approving into PSS writes PSS, tells the applicant, and settles "
          "every card")


async def case_h_approving_into_hf_never_touches_the_pss_collection():
    bot, handlers, stub = setup()
    await do_register(handlers, bot)
    doc = sole_registration(stub)
    hf, pss = services()

    await tap(handlers, bot, A1, approve_data(doc['registration_id'], "hf"))

    adds = stub.called('add_authorized_member')
    assert [a[2] for a in adds] == [HF_COLL], adds
    assert PSS_COLL not in stub.members, stub.members
    assert APPLICANT in hf.roster and APPLICANT not in pss.roster
    assert doc['decision_service'] == 'hf', doc
    assert_no_decider_leak(bot)
    print("OK  h. approving into HF lands in heartfelt_members and never touches "
          "peer_supporters")


async def case_i_two_simultaneous_approvals_produce_exactly_one_of_everything():
    """The race the atomic gate exists for. Both admins tap Approve; both reach
    decide_registration; exactly one gets a document back.

    The freeze_read below is doing real work, not papering over anything. There
    is DELIBERATELY no await between the status pre-check and the gate, and
    PyMongo blocks the event loop, so within one process the second tap normally
    stops at the cheap pre-check -- which cases (j) and (k) cover. The gate is
    what protects the case the pre-check cannot: two reads that both landed
    while the row still said 'pending' (two bot processes, a redeploy mid-flight,
    or PTB handling updates concurrently). freeze_read reproduces exactly that
    overlap, so both callers really do arrive at decide_registration and the
    filter is the only thing separating them. Without it this case would assert
    nothing about the gate at all.
    """
    bot, handlers, stub = setup()
    await do_register(handlers, bot)
    doc = sole_registration(stub)
    rid = doc['registration_id']
    _hf, pss = services()
    stub.freeze_read(rid)

    rec1, rec2 = Rec(), Rec()
    await asyncio.gather(
        handlers.handle_callback_query(
            cb_update(A1, approve_data(rid, "pss"), rec1), ctx_for(bot)),
        handlers.handle_callback_query(
            cb_update(A2, approve_data(rid, "pss"), rec2), ctx_for(bot)),
    )

    assert stub.count('decide_registration') == 2, (
        "both taps must actually reach the gate, or this proves nothing: %r"
        % (stub.calls,))
    assert stub.count('add_authorized_member') == 1, (
        "exactly ONE roster write: %r" % (stub.called('add_authorized_member'),))
    assert len(bot.texts_to(APPLICANT)) == 1, (
        "the applicant must be told exactly once: %r" % (bot.texts_to(APPLICANT),))
    assert bot.texts_to(APPLICANT)[0] == MESSAGES["registration_approved"].format(
        member=pss.member_label)

    answers = rec1.answer_texts() + rec2.answer_texts()
    assert sorted(answers, key=lambda t: t or "") == sorted(
        [None, MESSAGES["registration_already_handled"]], key=lambda t: t or ""), (
        "one plain ack for the winner, one 'already handled' for the loser: %r"
        % (answers,))
    assert doc['status'] == 'approved', doc
    assert_no_decider_leak(bot)
    print("OK  i. two simultaneous approvals produce exactly one of everything")


async def case_j_a_reject_after_an_approve_changes_nothing():
    """An Approve and a Reject racing each other -- the worst-flavoured version
    of case (i), because the loser here would send a REJECTION to somebody who
    has just been approved. freeze_read for the same reason as (i): it is what
    makes the reject actually reach the gate rather than stopping at the cheap
    pre-check."""
    bot, handlers, stub = setup()
    await do_register(handlers, bot)
    doc = sole_registration(stub)
    rid = doc['registration_id']
    _hf, pss = services()
    stub.freeze_read(rid)

    await tap(handlers, bot, A1, approve_data(rid, "pss"))
    rec = await tap(handlers, bot, A2, reject_data(rid))

    decisions = stub.called('decide_registration')
    assert len(decisions) == 2, (
        "the reject must genuinely reach the gate: %r" % (decisions,))
    assert decisions[1][3] == 'rejected', decisions
    assert doc['status'] == 'approved', (
        "the second decision must not overwrite the first: %r" % (doc,))
    assert rec.answer_texts() == [MESSAGES["registration_already_handled"]], \
        rec.answers

    assert bot.texts_to(APPLICANT) == [
        MESSAGES["registration_approved"].format(member=pss.member_label)], (
        "the applicant must have received exactly one message, and it must be "
        "the approval -- never a rejection after being approved: %r"
        % (bot.texts_to(APPLICANT),))
    assert APPLICANT in pss.roster
    assert stub.count('add_authorized_member') == 1, stub.calls
    assert_no_decider_leak(bot)
    print("OK  j. a reject landing after an approve changes nothing and sends nothing")


async def case_k_a_double_tap_by_one_admin_is_idempotent():
    """The ORDINARY double tap, deliberately left un-frozen: this is the path
    where the cheap status pre-check catches it before the gate is even reached,
    and it must be just as safe as the raced one."""
    bot, handlers, stub = setup()
    await do_register(handlers, bot)
    doc = sole_registration(stub)
    rid = doc['registration_id']

    await tap(handlers, bot, A1, approve_data(rid, "pss"))
    rec = await tap(handlers, bot, A1, approve_data(rid, "pss"))

    assert len(bot.texts_to(APPLICANT)) == 1, bot.texts_to(APPLICANT)
    assert stub.count('add_authorized_member') == 1, \
        stub.called('add_authorized_member')
    assert rec.answer_texts() == [MESSAGES["registration_already_handled"]], \
        rec.answers
    assert rec.markup_strips == [None], (
        "the second tap tidies its own buttons away: %r" % (rec.markup_strips,))
    assert_no_decider_leak(bot)
    print("OK  k. a double tap by one admin is idempotent")


async def case_l_a_rejection_writes_no_roster():
    bot, handlers, stub = setup()
    await do_register(handlers, bot)
    doc = sole_registration(stub)

    await tap(handlers, bot, A1, reject_data(doc['registration_id']))

    decisions = stub.called('decide_registration')
    assert len(decisions) == 1, decisions
    assert decisions[0][3] == 'rejected' and decisions[0][4] is None, decisions
    assert stub.count('add_authorized_member') == 0, (
        "a rejection must never write a roster: %r" % (stub.calls,))
    assert stub.count('mark_member_started') == 0, stub.calls
    assert bot.texts_to(APPLICANT) == [MESSAGES["registration_rejected"]], \
        bot.texts_to(APPLICANT)
    assert doc['status'] == 'rejected' and doc['decision_service'] is None, doc
    print("OK  l. a rejection writes no roster and no started flag")


async def case_m_a_rejection_leaks_nothing_about_who_decided():
    """The copy rule this whole feature is built around.

    The applicant's message must be BYTE-IDENTICAL to MESSAGES: not merely
    "contains no name", but literally the untouched constant, so no future
    concatenation can creep in unnoticed.
    """
    bot, handlers, stub = setup()
    await do_register(handlers, bot)
    doc = sole_registration(stub)

    await tap(handlers, bot, A1, reject_data(doc['registration_id']),
              first=APPROVER_FIRST, username=APPROVER_USER)

    texts = bot.texts_to(APPLICANT)
    assert texts == [MESSAGES["registration_rejected"]], (
        "the rejection must be sent VERBATIM from MESSAGES with no interpolation "
        "of any kind: %r" % (texts,))
    assert_no_decider_leak(bot)

    # And the admin card DOES name them -- that asymmetry is the point, so assert
    # it rather than leaving it to chance.
    settled = [t for _c, _mid, t, _m in bot.edited]
    assert settled and all(APPROVER_FIRST in t for t in settled), (
        "the settled ADMIN card names the approver on purpose: %r" % (settled,))
    print("OK  m. a rejection leaks nothing about who decided, while the admin "
          "card names them")


async def case_n_repeated_registers_produce_exactly_one_fan_out():
    bot, handlers, stub = setup()
    replies = []
    for _ in range(5):
        rec = await do_register(handlers, bot)
        replies.append(rec.replies[-1][0])

    assert len(stub.regs) == 1, "only one row may exist: %r" % (stub.regs,)
    assert stub.count('create_registration') == 1, stub.calls
    assert len(bot.sent) == 2, (
        "two admin cards, not ten: %r" % ([c for c, _t, _m in bot.sent],))
    assert replies[0] == MESSAGES["registration_submitted"], replies
    assert replies[1:] == [MESSAGES["registration_already_pending"]] * 4, replies
    print("OK  n. five /registers produce exactly one fan-out")


async def case_o_an_existing_member_is_never_offered_registration():
    bot, handlers, stub = setup(pss_members=(APPLICANT,))
    rec = await do_register(handlers, bot)

    assert rec.replies[-1][0] == MESSAGES["registration_already_member"], rec.replies
    assert stub.count('create_registration') == 0, stub.calls
    assert stub.regs == {}, stub.regs
    assert bot.sent == [], bot.sent
    print("OK  o. an existing member is never offered registration")


async def case_p_a_rejection_starts_a_cooldown_that_expires():
    bot, handlers, stub = setup()
    await do_register(handlers, bot)
    first = sole_registration(stub)
    await tap(handlers, bot, A1, reject_data(first['registration_id']))

    sent_before = len(bot.sent)
    rec = await do_register(handlers, bot)
    assert rec.replies[-1][0] == MESSAGES["registration_cooldown"], rec.replies
    assert len(bot.sent) == sent_before, (
        "a cooldown must not fan anything out: %r" % (bot.sent[sent_before:],))
    assert stub.count('create_registration') == 1, stub.calls

    # Wind the decision back beyond the window and it works again.
    first['decided_at'] = utcnow() - datetime.timedelta(
        hours=config.REGISTRATION_REAPPLY_HOURS + 1)
    rec = await do_register(handlers, bot)
    assert rec.replies[-1][0] == MESSAGES["registration_submitted"], rec.replies
    assert stub.count('create_registration') == 2, stub.calls

    fresh = [d for d in stub.regs.values() if d['status'] == 'pending']
    assert len(fresh) == 1 and fresh[0]['attempt'] == 2, fresh
    card = bot.texts_to(A1)[-1]
    assert "Previous requests: 1" in card, card
    print("OK  p. a rejection starts a cooldown, and the cooldown expires")


async def case_q_register_outside_a_private_chat_says_nothing_to_the_group():
    for label, kwargs in (("a group chat", dict(chat_type="group")),
                          ("no effective_chat at all", dict(with_chat=False))):
        bot, handlers, stub = setup()
        rec = await do_register(handlers, bot, **kwargs)

        assert rec.replies == [], (
            "%s: reply_text must never be called -- it would broadcast the "
            "applicant's intent to everyone in that chat: %r" % (label, rec.replies))
        assert stub.count('create_registration') == 0, (label, stub.calls)
        assert stub.regs == {}, (label, stub.regs)
        assert [c for c, _t, _m in bot.sent] == [str(APPLICANT)], (
            "%s: the only thing sent is a private nudge to the applicant: %r"
            % (label, bot.sent))
        assert bot.texts_to(APPLICANT) == [MESSAGES["registration_private_only"]], \
            (label, bot.texts_to(APPLICANT))
    print("OK  q. /register outside a private chat says nothing to the group, with "
          "or without an effective_chat")


async def case_r_register_fails_closed_when_the_records_are_offline():
    bot, handlers, stub = setup(db_available=False)
    rec = await do_register(handlers, bot)

    assert rec.replies[-1][0] == MESSAGES["registration_unavailable"], rec.replies
    assert stub.count('create_registration') == 0, (
        "a memory-only registration would evaporate on restart while the "
        "applicant believed it pending: %r" % (stub.calls,))
    assert bot.sent == [], bot.sent
    print("OK  r. /register fails closed when the records are offline")


async def case_s_a_decision_with_the_records_offline_leaves_the_buttons_live():
    bot, handlers, stub = setup()
    await do_register(handlers, bot)
    doc = sole_registration(stub)

    stub.db_available = False
    rec = await tap(handlers, bot, A1, approve_data(doc['registration_id'], "pss"))

    assert rec.answer_texts() == [MESSAGES["registration_db_offline"]], rec.answers
    assert stub.count('decide_registration') == 0, stub.calls
    assert bot.texts_to(APPLICANT) == [], bot.texts_to(APPLICANT)
    assert rec.markup_strips == [], (
        "the buttons are deliberately LEFT LIVE so the same tap works once Mongo "
        "is back: %r" % (rec.markup_strips,))
    assert doc['status'] == 'pending', doc
    print("OK  s. a decision with the records offline decides nothing and leaves "
          "the buttons live")


async def case_t_an_undeliverable_registration_is_never_called_submitted():
    """The single worst available outcome, guarded.

    Telling somebody who has just volunteered that their offer is with the team,
    when in fact nobody will ever see it, is worse than telling them it failed.
    """
    bot, handlers, stub = setup()
    bot.fail_for[str(A1)] = "Forbidden: bot can't initiate conversation with a user"
    bot.fail_for[str(A2)] = "Forbidden: bot can't initiate conversation with a user"

    with catching() as errors:
        rec = await do_register(handlers, bot)

    assert rec.replies[-1][0] == MESSAGES["registration_unavailable"], (
        "must NEVER be registration_submitted: %r" % (rec.replies,))
    assert MESSAGES["registration_submitted"] not in [t for t, _m in rec.replies], \
        rec.replies

    closes = stub.called('close_registration')
    assert len(closes) == 1 and closes[0][2] == 'undeliverable', closes
    doc = sole_registration(stub)
    assert doc['status'] == 'closed', doc

    joined = " ".join(errors.messages())
    assert "REGISTRATION UNDELIVERABLE" in joined, errors.messages()
    assert str(A1) in joined and str(A2) in joined, (
        "the ERROR must name the unreachable admins, or R13 is unactionable: %r"
        % (errors.messages(),))

    # And it does not block them forever: a closed row is not a pending one.
    bot.fail_for.clear()
    rec = await do_register(handlers, bot)
    assert rec.replies[-1][0] == MESSAGES["registration_submitted"], rec.replies
    assert sorted(c for c, _t, _m in bot.sent) == [str(A1), str(A2)], bot.sent
    print("OK  t. an undeliverable registration is never called submitted, and "
          "never blocks a retry")


async def case_u_one_unreachable_admin_does_not_break_the_other():
    bot, handlers, stub = setup()
    bot.fail_for[str(A1)] = "Forbidden: bot was blocked by the user"
    rec = await do_register(handlers, bot)

    assert rec.replies[-1][0] == MESSAGES["registration_submitted"], rec.replies
    doc = sole_registration(stub)
    assert [p['admin_id'] for p in doc['admin_messages']] == [A2], (
        "only the admin who actually received a card may be recorded: %r" % (doc,))

    await tap(handlers, bot, A2, approve_data(doc['registration_id'], "pss"))
    assert [c for c, _mid, _t, _m in bot.edited] == [str(A2)], (
        "an admin with no card is simply skipped, and the settle loop does not "
        "raise: %r" % (bot.edited,))
    assert doc['status'] == 'approved', doc
    print("OK  u. one unreachable admin does not break the other's card")


async def case_v_an_applicant_who_blocked_the_bot_is_still_approved_but_hidden():
    bot, handlers, stub = setup()
    await do_register(handlers, bot)
    doc = sole_registration(stub)
    _hf, pss = services()

    bot.fail_for[str(APPLICANT)] = "Forbidden: bot was blocked by the user"
    await tap(handlers, bot, A1, approve_data(doc['registration_id'], "pss"))

    assert doc['status'] == 'approved', (
        "a block is not grounds to deny somebody who volunteered: %r" % (doc,))
    assert stub.count('add_authorized_member') == 1, stub.calls
    assert APPLICANT in pss.roster

    cleared = [c for c in stub.called('mark_member_started')
               if c[1] == APPLICANT and c[2] is False]
    assert cleared, (
        "an approved supporter the bot cannot reach must never be offered in the "
        "picker; the flag is cleared and self-heals on their next message: %r"
        % (stub.called('mark_member_started'),))
    assert stub.members[PSS_COLL][APPLICANT]['has_started_bot'] is False, \
        stub.members[PSS_COLL][APPLICANT]

    settled = [t for _c, _mid, t, _m in bot.edited]
    assert settled and all(
        MESSAGES["registration_settled_unreachable"] in t for t in settled), (
        "the admins must be told the applicant could not be told: %r" % (settled,))
    print("OK  v. an applicant who blocked the bot is still approved, but hidden "
          "from the picker")


async def case_w_a_restart_leaves_the_live_buttons_fully_functional():
    """Registration state is Mongo-resolved on every callback, so a redeploy
    between the fan-out and the tap changes nothing."""
    bot, handlers, stub = setup()
    await do_register(handlers, bot)
    doc = sole_registration(stub)
    rid = doc['registration_id']

    # The restart: every in-memory index gone, brand new managers and bot, same
    # database.
    reset_state()
    config.REGISTRATION_ADMINS = frozenset((A1, A2))
    hf, pss = services()
    hf.roster.replace_records([])
    pss.roster.replace_records([])
    fresh_bot = FakeBot()
    install(stub)
    fresh_handlers = build(stub, fresh_bot)

    await tap(fresh_handlers, fresh_bot, A2, approve_data(rid, "pss"))

    assert doc['status'] == 'approved' and doc['decided_by'] == A2, doc
    assert APPLICANT in pss.roster
    assert fresh_bot.texts_to(APPLICANT) == [
        MESSAGES["registration_approved"].format(member=pss.member_label)], \
        fresh_bot.texts_to(APPLICANT)
    assert sorted(c for c, _mid, _t, _m in fresh_bot.edited) == [str(A1), str(A2)], (
        "both cards are still addressable after a restart, from the persisted "
        "admin_messages: %r" % (fresh_bot.edited,))
    assert_no_decider_leak(fresh_bot)
    print("OK  w. a restart leaves the live buttons fully functional")


async def case_x_approving_someone_deactivated_re_activates_them_once():
    bot, handlers, stub = setup()
    stub.members[PSS_COLL] = {APPLICANT: {'telegram_id': APPLICANT, 'active': False,
                                          'display_name': "Old Name"}}
    _hf, pss = services()
    assert APPLICANT not in pss.roster, (
        "a deactivated member is not in the in-memory roster, which is why "
        "/register is allowed to proceed at all")

    await do_register(handlers, bot)
    doc = sole_registration(stub)
    await tap(handlers, bot, A1, approve_data(doc['registration_id'], "pss"))

    assert doc['status'] == 'approved', doc
    assert stub.members[PSS_COLL][APPLICANT]['active'] is True, (
        "add_authorized_member is an idempotent upsert, so this re-activates "
        "rather than duplicating: %r" % (stub.members[PSS_COLL],))
    assert stub.count('add_authorized_member') == 1, stub.calls
    assert len(bot.texts_to(APPLICANT)) == 1, bot.texts_to(APPLICANT)
    assert APPLICANT in pss.roster
    print("OK  x. approving somebody previously deactivated re-activates them once")


async def case_y_cross_roster_membership_blocks_approval_from_memory_and_mongo():
    """Membership is exclusive, and the in-memory roster is up to 300s stale --
    so somebody added by the CLI thirty seconds ago is invisible to the memory
    check alone. Both halves are tested."""
    for label in ("in memory", "in Mongo only"):
        bot, handlers, stub = setup()
        await do_register(handlers, bot)
        doc = sole_registration(stub)
        hf, pss = services()

        # Added to the OTHER roster after the request was raised -- which is
        # exactly how this happens in practice.
        if label == "in memory":
            hf.roster.replace_records([{'telegram_id': APPLICANT, 'active': True,
                                        'display_name': "Already Here"}])
        else:
            stub.members[HF_COLL] = {APPLICANT: {'telegram_id': APPLICANT,
                                                 'active': True}}
            assert APPLICANT not in hf.roster, (
                "%s: the point of this half is that memory does NOT know" % label)

        rec = await tap(handlers, bot, A1, approve_data(doc['registration_id'], "pss"))

        assert rec.answer_texts() == [
            MESSAGES["registration_cross_roster"].format(member=hf.member_label)], \
            (label, rec.answers)
        assert stub.count('decide_registration') == 0, (
            "%s: nothing may be decided: %r" % (label, stub.calls))
        assert stub.count('add_authorized_member') == 0, (label, stub.calls)
        assert APPLICANT not in pss.roster, label
        assert doc['status'] == 'pending', (label, doc)
        assert rec.markup_strips == [], (
            "%s: the buttons stay live -- the fix is out-of-band and the admin "
            "should be able to retry: %r" % (label, rec.markup_strips))
    print("OK  y. cross-roster membership blocks approval, seen from memory or "
          "from Mongo alone")


async def case_z_an_approved_supporter_becomes_pickable_once_named():
    """The end-to-end proof of D38/R15: approval makes somebody claim-authorized
    immediately, and picker-visible only after a display name is set."""
    bot, handlers, stub = setup()
    await do_register(handlers, bot)
    doc = sole_registration(stub)
    _hf, pss = services()

    await tap(handlers, bot, A1, approve_data(doc['registration_id'], "pss"))

    assert stub.members[PSS_COLL][APPLICANT]['has_started_bot'] is True, \
        stub.members[PSS_COLL][APPLICANT]
    assert APPLICANT in pss.roster, "claim-authorized immediately"
    assert config.available_supporters("pss") == [], (
        "and PICKER-INVISIBLE until somebody sets a display name -- that is the "
        "staged-rollout lever, not an oversight: %r"
        % (config.available_supporters("pss"),))

    # An admin sets the name out-of-band; the next roster refresh picks it up.
    stub.members[PSS_COLL][APPLICANT].update(display_name="Robin", available=True)
    await main.refresh_all_rosters()

    pickable = [p.telegram_id for p in config.available_supporters("pss")]
    assert pickable == [APPLICANT], (
        "once named, the approved supporter is offered in the picker: %r"
        % (pickable,))
    assert_no_decider_leak(bot)
    print("OK  z. an approved supporter is claim-authorized at once and pickable "
          "once named")


async def case_aa_every_registration_string_passes_the_copy_guards():
    keys = [k for k in MESSAGES if k.startswith("registration_")]
    # 21 registration_* keys exist; zero headroom, house convention.
    assert len(keys) >= 21, (
        "far fewer registration keys than expected (%d); this scan would be "
        "nearly vacuous: %s" % (len(keys), sorted(keys)))
    for key in keys:
        value = MESSAGES[key]
        assert isinstance(value, str), (key, type(value))
        assert value.strip(), key
        assert "/help" not in value, (
            "only `welcome` may mention /help (test_copy.py pins it): %r" % key)
        lowered = value.lower()
        for token in ("heartfelt", "hearhtfelt"):
            assert token not in lowered, (
                "MESSAGES[%r] hardcodes the old brand; the roster label must come "
                "from svc.member_label instead: %r" % (key, value))
    print("OK  aa. every registration string passes the copy guards")


async def case_ab_the_cli_renders_a_malformed_row_without_raising():
    bot, handlers, stub = setup()
    await do_register(handlers, bot, username="hopeful", first_name="Ada")
    doc = sole_registration(stub)

    # A row whose created_at Mongo would never produce, but a hand-edit might.
    # One bad row must not kill the whole dump.
    stub.regs['broken'] = {
        'registration_id': 'broken', 'telegram_id': 4242, 'status': 'pending',
        'created_at': "garbage", 'username': None, 'first_name': None,
        'last_name': None, 'decided_by': None, 'attempt': 1,
    }
    stub.initialize = lambda: True

    # Captured, for two reasons. First, so the case can assert what was actually
    # RENDERED rather than only that nothing raised -- "renders a malformed row"
    # is the claim in the name, and reaching the list branch does not prove it.
    # Second, utils.py prints emoji, which is house style there, and writing
    # them to a console whose encoding is not UTF-8 raises UnicodeEncodeError
    # from print() itself. Uncaptured, this suite was the only one of the
    # thirteen that could not run on a stock Windows console, which is a red
    # gate for a reason that has nothing to do with the code under test.
    dump = io.StringIO()
    with contextlib.redirect_stdout(dump):
        utils_mod.manage_registrations('list', status='pending')
        utils_mod.manage_registrations('list', status='approved')
    listing = dump.getvalue()
    assert stub.called('list_registrations'), stub.calls
    assert '--/-- --:--' in listing, (
        "the malformed created_at must fall back rather than kill the dump: %r"
        % (listing,))
    assert '4242' in listing and 'broken' in listing, (
        "the malformed row must still be printed: %r" % (listing,))
    assert '@hopeful' in listing and 'Ada' in listing, (
        "the good row must render its name and username: %r" % (listing,))
    assert "No registrations found for status 'approved'." in listing, (
        "an empty status must say so rather than print a bare header: %r"
        % (listing,))

    # The baseline is sampled BEFORE the id-less call, deliberately. Sampling it
    # after would make this pair of assertions self-neutralising: a regression
    # that dropped utils.py's "--registration-id is required" guard would call
    # close_registration(None, ...), closes_before would be 1, and the final
    # assertion would compare 2 == 2 and pass. Verified by mutation -- with the
    # guard deleted and the old ordering, the whole thirteen-suite gate stayed
    # green.
    closes_before = stub.count('close_registration')
    closes = io.StringIO()
    with contextlib.redirect_stdout(closes):
        utils_mod.manage_registrations('close')        # no id -> refused, no call
        assert stub.count('close_registration') == closes_before, (
            "`registrations --action close` with no --registration-id must "
            "refuse BEFORE it touches the database: %r" % (stub.calls,))
        utils_mod.manage_registrations(
            'close', registration_id=doc['registration_id'])
    assert '--registration-id is required' in closes.getvalue(), closes.getvalue()
    assert stub.count('close_registration') == closes_before + 1, stub.calls
    assert stub.regs[doc['registration_id']]['status'] == 'closed', doc
    print("OK  ab. the CLI renders a malformed row without raising, and closes a row")


async def case_ac_an_admin_can_never_decide_their_own_request():
    """Two independent guards, because this is the one path that grants privilege
    to the person tapping."""
    bot, handlers, stub = setup()
    rec = await do_register(handlers, bot, user_id=A1)

    assert rec.replies[-1][0] == MESSAGES["registration_submitted"], rec.replies
    assert [c for c, _t, _m in bot.sent] == [str(A2)], (
        "an admin's own id is excluded from the fan-out, so no card they could "
        "tap ever exists: %r" % (bot.sent,))

    doc = sole_registration(stub)
    # And a hand-crafted tap on a card that should not exist is still refused.
    rec = await tap(handlers, bot, A1, approve_data(doc['registration_id'], "pss"))
    assert rec.answer_texts() == [MESSAGES["registration_self_decision"]], rec.answers
    assert stub.count('decide_registration') == 0, stub.calls
    assert stub.count('add_authorized_member') == 0, stub.calls
    assert doc['status'] == 'pending', doc
    print("OK  ac. an admin can never decide their own request")


async def case_ad_the_allowlist_parser_drops_exactly_what_it_should():
    cases = {
        " 1 , ,2 ": (frozenset({1, 2}), ()),
        # A negative id is a CHANNEL id. Accepting one would post an applicant's
        # real name, id and username to a channel.
        "-1001234567890": (frozenset(), ("-1001234567890",)),
        "abc,3": (frozenset({3}), ("abc",)),
        "": (frozenset(), ()),
        "5,5,5": (frozenset({5}), ()),
        "0": (frozenset(), ("0",)),
    }
    saved = os.environ.get("REG_ADMIN_PARSE_PROBE")
    try:
        for raw, expected in cases.items():
            os.environ["REG_ADMIN_PARSE_PROBE"] = raw
            got = config._env_admin_ids("REG_ADMIN_PARSE_PROBE")
            assert got == expected, (raw, got, expected)
    finally:
        if saved is None:
            os.environ.pop("REG_ADMIN_PARSE_PROBE", None)
        else:
            os.environ["REG_ADMIN_PARSE_PROBE"] = saved

    assert config.is_registration_admin(None) is False
    assert config.is_registration_admin("not-a-number") is False
    config.REGISTRATION_ADMINS = frozenset({9, 4})
    assert config.registration_admin_ids() == [4, 9], (
        "the fan-out order must be deterministic, never set iteration order")
    assert config.is_registration_admin("9") is True, (
        "a str id must coerce, exactly like AuthorizedMembersStore.__contains__")
    assert config.registration_is_enabled() is True
    config.REGISTRATION_ADMINS = frozenset()
    assert config.registration_is_enabled() is False
    print("OK  ad. the allowlist parser drops exactly what it should")


async def case_ae_the_real_mongo_filter_names_the_state_it_leaves():
    """The stub above enforces the filter, but the stub is not what ships. Drive
    the REAL DBManager against a fake collection and read the filter it builds."""
    seen = {}

    class FakeCollection:
        def find_one_and_update(self, filt, update, **kw):
            seen['filter'] = filt
            seen['update'] = update
            seen['kwargs'] = kw
            return {'registration_id': 'rid-1', 'status': 'approved'}

    class FakeDB:
        def __getitem__(self, name):
            seen.setdefault('collections', []).append(name)
            return FakeCollection()

    real_connection = db_manager_mod.db_manager
    real_available = db_manager_mod.db_mgr.db_available
    try:
        db_manager_mod.db_manager = SimpleNamespace(db=FakeDB())
        db_manager_mod.db_mgr.db_available = True

        out = db_manager_mod.db_mgr.decide_registration('rid-1', A1, 'approved', 'pss')
        assert out is not None, out
        assert seen['filter'] == {'registration_id': 'rid-1', 'status': 'pending'}, (
            "the filter must NAME THE STATE BEING LEFT -- that single clause is "
            "the entire exactly-once guarantee: %r" % (seen['filter'],))
        assert seen['kwargs'].get('return_document') is ReturnDocument.AFTER, (
            "ReturnDocument.AFTER, not the legacy return_document=True: %r"
            % (seen['kwargs'],))
        assert seen['collections'][-1] == 'supporter_registrations', (
            "a registration row must NEVER live in `sessions`: restore.py would "
            "rehydrate it as a help request: %r" % (seen['collections'],))
        setted = seen['update']['$set']
        assert setted['status'] == 'approved' and setted['decided_by'] == A1 \
            and setted['decision_service'] == 'pss', setted

        seen.clear()
        assert db_manager_mod.db_mgr.decide_registration(
            'rid-1', A1, 'maybe') is None, "an invalid decision must be refused"
        assert 'filter' not in seen, (
            "and refused BEFORE Mongo is touched: %r" % (seen,))

        seen.clear()
        db_manager_mod.db_mgr.close_registration('rid-1', 'closed_by_admin')
        assert seen['filter'] == {'registration_id': 'rid-1', 'status': 'pending'}, (
            "closing is filtered on 'pending' too, so it can never erase a "
            "decision somebody just made: %r" % (seen['filter'],))
    finally:
        db_manager_mod.db_manager = real_connection
        db_manager_mod.db_mgr.db_available = real_available
    print("OK  ae. the real Mongo filter names the state it leaves")


# --------------------------------------------------------------------------- runner
async def case_af_a_switched_off_track_says_so_on_its_button():
    """A non-runnable track still accepts approvals, but nothing is posted to its
    queue -- so an approved supporter would be told they can claim and then find
    nothing to claim. The approver must be able to see that on the button.

    Admin-facing only: the callback_data is unchanged (so approving still works and
    every other case's callback string still matches) and NO applicant-facing copy
    differs. Reproduces the deployed shape of the trap: PSS switched off while HF
    stays live, which is exactly what HANDOFF's PSS rollback procedure produces.
    """
    bot, handlers, stub = setup()
    pss = config.SERVICES[ServiceType.PSS.value]
    hf = config.SERVICES[ServiceType.HF.value]
    pss.enabled = False                      # the documented rollback
    assert not pss.runnable and hf.runnable, "fixture must switch off exactly one track"

    markup = handlers._registration_keyboard("rid-af")
    buttons = [b for row in markup.inline_keyboard for b in row]
    by_data = {b.callback_data: b.text for b in buttons}

    hf_data = f"{config.CB_REG_APPROVE}:rid-af:{hf.key}"
    pss_data = f"{config.CB_REG_APPROVE}:rid-af:{pss.key}"

    # The callback_data must NOT change -- approving a switched-off track is still
    # how a roster gets built before launch (see _registration_keyboard's docstring).
    assert hf_data in by_data, f"the runnable track lost its button: {list(by_data)}"
    assert pss_data in by_data, (
        f"a switched-off track must still be approvable, only marked: {list(by_data)}")

    offline_marker = MESSAGES["registration_approve_button_offline"].format(
        member=pss.member_label)
    live_marker = MESSAGES["registration_approve_button"].format(member=hf.member_label)
    assert by_data[pss_data] == offline_marker, (
        f"the switched-off track must say so on its button, got {by_data[pss_data]!r}")
    assert by_data[hf_data] == live_marker, (
        f"the live track must be unmarked, got {by_data[hf_data]!r}")

    # And the applicant is told nothing different -- approving still works, and the
    # message they receive comes from MESSAGES verbatim as every other case asserts.
    await do_register(handlers, bot)
    doc = sole_registration(stub)
    before = len(bot.texts_to(APPLICANT))
    await tap(handlers, bot, A1, approve_data(doc['registration_id'], pss.key))
    sent = bot.texts_to(APPLICANT)[before:]
    assert len(sent) == 1, f"exactly one message to the applicant, got {sent}"
    assert sent[0] == MESSAGES["registration_approved"].format(member=pss.member_label), (
        "the applicant's copy must be untouched by the admin-facing marker")


async def case_ag_the_deep_link_starts_registration_only_when_it_is_on():
    """t.me/<bot>?start=register is the ONLY discovery route for /register.

    It must do three things and no more: register when the feature is on; be
    indistinguishable from a bare /start when it is off (so the link cannot betray
    that registration exists); and leave an ordinary /start completely alone.
    """
    async def start(handlers, bot, args):
        """Drive the REAL start_command and return what the applicant was told."""
        rec = Rec()
        ctx = SimpleNamespace(bot=bot) if args is None else               SimpleNamespace(bot=bot, args=args)
        await handlers.start_command(text_update(APPLICANT, "/start", rec), ctx)
        assert rec.replies, "expected a reply to the applicant"
        return rec.replies[-1][0]

    link = [config.REGISTER_DEEP_LINK_PAYLOAD]

    # 1. enabled -> the link registers, exactly as typing /register would
    bot, handlers, stub = setup()
    said = await start(handlers, bot, link)
    assert stub.called('create_registration'), "the link must start registration"
    assert bot.texts_to(A1) and bot.texts_to(A2), "both admins must get a card"
    assert said == MESSAGES["registration_submitted"], said

    # 2. DISABLED -> byte-identical to a bare /start, and nothing is created.
    #    A link that behaves differently when off is a link that leaks the feature.
    bot_off, handlers_off, stub_off = setup()
    config.REGISTRATION_ADMINS = frozenset()
    off_reply = await start(handlers_off, bot_off, link)
    assert not stub_off.calls, f"nothing may be created while off: {stub_off.calls}"
    assert not bot_off.sent, f"nobody may be messaged while off: {bot_off.sent}"

    bot_bare, handlers_bare, _ = setup()
    config.REGISTRATION_ADMINS = frozenset()
    bare_reply = await start(handlers_bare, bot_bare, [])
    assert off_reply == bare_reply, (
        "a disabled deep link must be indistinguishable from a bare /start")

    # 3. enabled, but an ORDINARY /start -> still the welcome, no registration
    bot_p, handlers_p, stub_p = setup()
    plain = await start(handlers_p, bot_p, [])
    assert not stub_p.called('create_registration'), "a bare /start must not register"
    assert "Care Network" in plain

    # 4. a context with NO .args at all -- the shape every other suite's fake has
    bot_n, handlers_n, stub_n = setup()
    noargs = await start(handlers_n, bot_n, None)
    assert not stub_n.called('create_registration')
    assert "Care Network" in noargs

CASES = [
    case_a_an_empty_allowlist_makes_register_indistinguishable_from_nothing,
    case_b_an_inert_bot_refuses_every_registration_tap,
    case_c_a_registration_reaches_every_admin_and_is_recorded,
    case_d_the_card_and_its_buttons_are_well_formed,
    case_e_no_roster_membership_grants_any_approval_power,
    case_f_an_admin_on_no_roster_can_still_approve,
    case_g_approving_into_pss_writes_pss_and_settles_every_card,
    case_h_approving_into_hf_never_touches_the_pss_collection,
    case_i_two_simultaneous_approvals_produce_exactly_one_of_everything,
    case_j_a_reject_after_an_approve_changes_nothing,
    case_k_a_double_tap_by_one_admin_is_idempotent,
    case_l_a_rejection_writes_no_roster,
    case_m_a_rejection_leaks_nothing_about_who_decided,
    case_n_repeated_registers_produce_exactly_one_fan_out,
    case_o_an_existing_member_is_never_offered_registration,
    case_p_a_rejection_starts_a_cooldown_that_expires,
    case_q_register_outside_a_private_chat_says_nothing_to_the_group,
    case_r_register_fails_closed_when_the_records_are_offline,
    case_s_a_decision_with_the_records_offline_leaves_the_buttons_live,
    case_t_an_undeliverable_registration_is_never_called_submitted,
    case_u_one_unreachable_admin_does_not_break_the_other,
    case_v_an_applicant_who_blocked_the_bot_is_still_approved_but_hidden,
    case_w_a_restart_leaves_the_live_buttons_fully_functional,
    case_x_approving_someone_deactivated_re_activates_them_once,
    case_y_cross_roster_membership_blocks_approval_from_memory_and_mongo,
    case_z_an_approved_supporter_becomes_pickable_once_named,
    case_aa_every_registration_string_passes_the_copy_guards,
    case_ab_the_cli_renders_a_malformed_row_without_raising,
    case_ac_an_admin_can_never_decide_their_own_request,
    case_ad_the_allowlist_parser_drops_exactly_what_it_should,
    case_ae_the_real_mongo_filter_names_the_state_it_leaves,
    case_af_a_switched_off_track_says_so_on_its_button,
    case_ag_the_deep_link_starts_registration_only_when_it_is_on,
]


async def run():
    for case in CASES:
        result = case()
        if asyncio.iscoroutine(result):
            await result


if __name__ == "__main__":
    # A driver that discovers its own tests reports success when it discovers
    # NOTHING. Verified: renaming the `test_` prefix in this file made it print
    # "All 0 tests passed!" and exit 0 -- a fully green CI step, in front of a
    # deploy to a live helpline, having run zero assertions. A refactor into a
    # class, a rename, an import shadow or a bad merge all reach that state.
    # Coverage here may grow; it may not silently shrink.
    assert len(CASES) >= 33, (
        "expected at least 31 cases, collected %d (%s). Test discovery has "
        "regressed -- fix the discovery, do not lower this number."
        % (len(CASES), ", ".join(c.__name__ for c in CASES) or "none")
    )

    real = (handlers_mod.db_mgr, queue_mod.db_mgr, session_mod.db_mgr,
            restore_mod.db_mgr, expiry_mod.db_mgr, main.db_mgr, utils_mod.db_mgr)
    real_connection = db_manager_mod.db_manager
    real_available = db_manager_mod.db_mgr.db_available
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]
    saved = (hf.channel_id, hf.enabled, pss.channel_id, pss.enabled)
    saved_admins = config.REGISTRATION_ADMINS
    try:
        asyncio.run(run())
        print("\nAll %d registration assertions passed!" % len(CASES))
    finally:
        (handlers_mod.db_mgr, queue_mod.db_mgr, session_mod.db_mgr,
         restore_mod.db_mgr, expiry_mod.db_mgr, main.db_mgr,
         utils_mod.db_mgr) = real
        db_manager_mod.db_manager = real_connection
        db_manager_mod.db_mgr.db_available = real_available
        hf.channel_id, hf.enabled, pss.channel_id, pss.enabled = saved
        # replace_records([]) FIRST: replace() with the same id set is a no-op
        # that keeps existing profiles, so without this the scratch profiles
        # would leak into any suite sharing this process.
        hf.roster.replace_records([])
        hf.roster.replace(config.DEFAULT_HEARTFELT_MEMBERS)
        pss.roster.replace_records([])
        pss.roster.replace([])
        config.REGISTRATION_ADMINS = saved_admins
        reset_state()
