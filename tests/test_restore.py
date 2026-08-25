#!/usr/bin/env python3
"""
Restart durability (Phase 3).

Drives the REAL restore_state / QueueManager / SessionManager / SessionExpiryManager
/ BotHandlers against a recording stub db_mgr, with a fake bot. This is the suite
that has to be right: it decides whether a deploy silently drops people who were
queued or mid-conversation, and whether the first boot DMs every past requester of
a mental-health helpline.

Run directly: `python tests/test_restore.py`
"""

import asyncio
import datetime
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from config import ServiceType, UserState
from src.timeutil import UTC, utcnow

import src.bot.restore as restore_mod
import src.bot.managers.expiry as expiry_mod
import src.bot.managers.queue as queue_mod
import src.bot.managers.session as session_mod
from src.bot.handlers import BotHandlers
from src.bot.managers.expiry import SessionExpiryManager
from src.bot.managers.queue import QueueManager
from src.bot.managers.session import SessionManager
from src.bot.restore import restore_state

HF_MEMBER = 1001
PSS_MEMBER = 2001
HF_CHANNEL = "-100HF"
PSS_CHANNEL = "-100PSS"


# --------------------------------------------------------------------------- stubs
class StubDB:
    """Recording stand-in for db_mgr, backed by a list of session documents."""

    def __init__(self, docs=None, db_available=True):
        self.db_available = db_available
        self.docs = {d['session_id']: d for d in (docs or [])}
        self.calls = []                 # (method, *args) in order
        self.ended = {}                 # session_id -> end_reason
        self.queue_messages = {}        # session_id -> (channel_id, message_id)
        self.raise_on_pending = False
        # When True, get_sessions_by_activity keeps returning already-ended docs,
        # simulating a stale read or a duplicated sweep.
        self.activity_ignores_status = False

    # --- reads
    def get_pending_sessions(self):
        self.calls.append(('get_pending_sessions',))
        if self.raise_on_pending:
            raise RuntimeError("Mongo said no")
        out = [d for d in self.docs.values() if d.get('status') == 'pending']
        return sorted(out, key=lambda d: d.get('created_at') or datetime.datetime.min.replace(tzinfo=UTC))

    def get_active_sessions(self):
        self.calls.append(('get_active_sessions',))
        out = [d for d in self.docs.values() if d.get('status') == 'active']
        return sorted(out, key=lambda d: d.get('created_at') or datetime.datetime.min.replace(tzinfo=UTC))

    def get_session(self, session_id):
        self.calls.append(('get_session', session_id))
        return self.docs.get(session_id)

    def get_sessions_by_activity(self, cutoff):
        self.calls.append(('get_sessions_by_activity', cutoff))
        out = []
        for d in self.docs.values():
            if d.get('status') != 'active' and not self.activity_ignores_status:
                continue
            if self.activity_ignores_status and d.get('_was_active') is not True:
                continue
            last = d.get('last_activity_at')
            if last is not None and last <= cutoff:
                out.append(d)
        return out

    # --- writes
    def end_session(self, session_id, ended_by_user_id, system_end=False, end_reason=None):
        self.calls.append(('end_session', session_id, ended_by_user_id, system_end, end_reason))
        doc = self.docs.get(session_id)
        if session_id in self.ended:
            return False                      # atomic once-only, as Mongo would be
        if doc is not None and doc.get('status') not in ('pending', 'active'):
            return False
        self.ended[session_id] = end_reason
        if doc is not None:
            doc['status'] = 'ended'
        return True

    def claim_session(self, session_id, member_id, telehandle=None):
        self.calls.append(('claim_session', session_id, member_id))
        doc = self.docs.get(session_id)
        if doc is None or doc.get('status') != 'pending':
            return False
        doc['status'] = 'active'
        doc['heartfelt_member_id'] = member_id
        doc['claimed_at'] = utcnow()
        return True

    def set_queue_message(self, session_id, channel_id, message_id):
        self.calls.append(('set_queue_message', session_id, channel_id, message_id))
        self.queue_messages[session_id] = (channel_id, message_id)
        return True

    def create_session(self, *a, **kw):
        self.calls.append(('create_session',))
        return None

    def log_message(self, **kw):
        self.calls.append(('log_message', kw.get('session_id')))
        return True

    def update_session_activity(self, session_id):
        self.calls.append(('update_session_activity', session_id))
        return True

    # --- helpers for assertions
    def called(self, method):
        return [c for c in self.calls if c[0] == method]

    def end_reason_for(self, session_id):
        return self.ended.get(session_id)


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.deleted = []
        self._mid = 0

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self._mid += 1
        self.sent.append((str(chat_id), text))
        return SimpleNamespace(message_id=self._mid)

    async def edit_message_text(self, chat_id=None, message_id=None, text=None, **kw):
        self.edited.append((str(chat_id), message_id, text))
        return None

    async def delete_message(self, chat_id=None, message_id=None, **kw):
        self.deleted.append((str(chat_id), message_id))
        return None

    def texts_to(self, user_id):
        return [t for c, t in self.sent if c == str(user_id)]


def make_callback_update(user_id, data, rec, username="mem", first="Mem"):
    async def answer(t=None, show_alert=False):
        rec.append((t, show_alert))

    async def edit_message_text(t, **kw):
        return None

    fu = SimpleNamespace(id=user_id, username=username, first_name=first, last_name=None)
    q = SimpleNamespace(from_user=fu, data=data, answer=answer,
                        edit_message_text=edit_message_text)
    return SimpleNamespace(effective_user=fu, message=None, callback_query=q)


# --------------------------------------------------------------------------- fixtures
def reset_state():
    for d in (config.user_states, config.user_to_service_map, config.queue_entries,
              config.user_to_queue_map, config.active_sessions, config.user_to_session_map,
              config.session_warnings):
        d.clear()
    config.queue_order.clear()
    config.used_anonymous_ids.clear()
    config.safety_logs.clear()


def install(stub):
    restore_mod.db_mgr = stub
    queue_mod.db_mgr = stub
    session_mod.db_mgr = stub
    expiry_mod.db_mgr = stub


def enable_both_services():
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]
    hf.channel_id, hf.enabled = HF_CHANNEL, True
    hf.roster.replace([HF_MEMBER])
    pss.channel_id, pss.enabled = PSS_CHANNEL, True
    pss.roster.replace([PSS_MEMBER])


def pending_doc(sid, user_id, age_minutes, service="hf", **extra):
    created = utcnow() - datetime.timedelta(minutes=age_minutes)
    doc = {
        'session_id': sid,
        'user_id': user_id,
        'service': service,
        'status': 'pending',
        'description': 'I could use someone to talk to',
        'anonymous_user_id': f'RHesident #{user_id % 10000}',
        'created_at': created,
        'last_activity_at': created,
        'claimed_at': None,
        'heartfelt_member_id': None,
        'queue_channel_id': config.get_service(service).channel_id,
        'queue_message_id': 900 + (user_id % 100),
    }
    doc.update(extra)
    return doc


def active_doc(sid, user_id, member_id, idle_minutes, service="hf", **extra):
    last = utcnow() - datetime.timedelta(minutes=idle_minutes)
    doc = {
        'session_id': sid,
        'user_id': user_id,
        'heartfelt_member_id': member_id,
        'service': service,
        'status': 'active',
        'description': 'ongoing',
        'anonymous_user_id': f'RHesident #{user_id % 10000}',
        'created_at': last,
        'claimed_at': last,
        'last_activity_at': last,
        '_was_active': True,
    }
    doc.update(extra)
    return doc


def build(stub):
    bot = FakeBot()
    sm = SessionManager(bot)
    qm = QueueManager(bot)
    em = SessionExpiryManager(bot, sm)
    handlers = BotHandlers(sm, qm)
    install(stub)
    return bot, sm, qm, em, handlers


# --------------------------------------------------------------------------- cases
def case_a_happy_pending():
    reset_state()
    doc = pending_doc("p-a", 3001, 5)
    stub = StubDB([doc])
    bot, sm, qm, em, _ = build(stub)

    stats = restore_state(qm, sm)
    assert stats['pending_restored'] == 1, stats
    entry = config.queue_entries["p-a"]
    assert entry['user_id'] == 3001
    assert entry['message_id'] == doc['queue_message_id']
    assert entry['channel_id'] == HF_CHANNEL
    assert entry['restored'] is True
    assert config.user_to_queue_map[3001] == "p-a"
    assert config.queue_order == ["p-a"]
    assert config.user_states[3001] == UserState.IN_QUEUE
    assert entry['anonymous_id'] in config.used_anonymous_ids
    print("OK  a. a live pending request is restored with its channel post coordinates")


def case_b_fifo():
    reset_state()
    stub = StubDB([pending_doc("p-old", 3101, 30), pending_doc("p-new", 3102, 5)])
    bot, sm, qm, em, _ = build(stub)

    restore_state(qm, sm)
    assert config.queue_order == ["p-old", "p-new"], config.queue_order
    assert qm.get_queue_position(3101) == 1
    assert qm.get_queue_position(3102) == 2
    print("OK  b. FIFO order survives the restart (oldest request keeps position 1)")


async def case_c_legacy_doc_without_message_id():
    reset_state()
    doc = pending_doc("p-c", 3201, 5, queue_message_id=None, queue_channel_id=None)
    stub = StubDB([doc])
    bot, sm, qm, em, handlers = build(stub)

    restore_state(qm, sm)
    assert config.queue_entries["p-c"]['message_id'] is None
    # The Claim button on the surviving post still resolves: callback_data is
    # claim_<session_id> and the entry is restored under that exact key.
    answers = []
    await handlers.handle_callback_query(
        make_callback_update(HF_MEMBER, "claim_p-c", answers), SimpleNamespace(bot=bot))
    assert config.user_states[3201] == UserState.IN_CONVERSATION
    assert config.user_states[HF_MEMBER] == UserState.IN_CONVERSATION
    assert not bot.edited, "there is no message_id to edit, and that must not raise"
    print("OK  c. a legacy doc with no queue_message_id restores and can still be claimed")


def case_d_naive_created_at():
    """Legacy rows were written by datetime.utcnow(), i.e. naive. This is the case
    that fails on a non-UTC machine if the timezone convention is wrong."""
    reset_state()
    naive = datetime.datetime.utcnow() - datetime.timedelta(minutes=20)
    assert naive.tzinfo is None
    doc = pending_doc("p-d", 3301, 20, created_at=naive, last_activity_at=naive)
    stub = StubDB([doc])
    bot, sm, qm, em, _ = build(stub)

    stats = restore_state(qm, sm)
    assert stats['aborted'] is False, "a naive created_at must not blow up rehydration"
    assert stats['pending_restored'] == 1
    created = config.queue_entries["p-d"]['created_at']
    assert created.tzinfo is not None, "restored timestamps must be aware"
    age = (utcnow() - created).total_seconds() / 60.0
    assert 19 < age < 21, age
    print("OK  d. a naive legacy created_at is read as UTC; age computed correctly")


async def case_e_expired_during_deploy():
    reset_state()
    stub = StubDB([pending_doc("p-e", 3401, 75)])   # HF window is 60 min
    bot, sm, qm, em, _ = build(stub)

    stats = restore_state(qm, sm)
    assert stats['pending_restored'] == 1, "75 min is inside the notify horizon; restore it"
    assert "p-e" in config.queue_entries

    await qm.sweep_expired_queues()
    assert "p-e" not in config.queue_entries
    assert bot.texts_to(3401), "the requester must be told their place expired"
    assert stub.end_reason_for("p-e") == 'queue_expired', stub.ended
    print("OK  e. a request that expired during the deploy is restored, then expired and notified")


async def case_f_ancient_pending():
    reset_state()
    stub = StubDB([pending_doc("p-f", 3501, 30 * 24 * 60)])   # 30 days old
    bot, sm, qm, em, _ = build(stub)

    stats = restore_state(qm, sm)
    assert stats['pending_stale_closed'] == 1, stats
    assert stats['pending_restored'] == 0
    assert "p-f" not in config.queue_entries
    assert stub.end_reason_for("p-f") == 'stale_startup_sweep', stub.ended

    await qm.sweep_expired_queues()
    assert not bot.texts_to(3501), (
        "a 30-day-old requester must NEVER be DMed 'your place in the queue expired'")
    print("OK  f. an ancient pending row is closed silently, with nothing sent to that user")


async def case_g_pss_pending_not_expired():
    reset_state()
    stub = StubDB([pending_doc("p-g", 3601, 90, service="pss")])
    bot, sm, qm, em, _ = build(stub)

    restore_state(qm, sm)
    assert "p-g" in config.queue_entries
    await qm.sweep_expired_queues()
    assert "p-g" in config.queue_entries, "90 min is nowhere near PSS's 24h window"
    assert not bot.texts_to(3601)
    assert "p-g" not in stub.ended
    print("OK  g. a 90-minute PSS request is restored and left alone by the sweep")


def case_h_happy_active():
    reset_state()
    doc = active_doc("a-h", 3701, PSS_MEMBER, 5, service="pss")
    stub = StubDB([doc])
    bot, sm, qm, em, _ = build(stub)

    stats = restore_state(qm, sm)
    assert stats['active_restored'] == 1, stats
    sess = config.active_sessions["a-h"]
    assert sess['service'] == "pss"
    assert sess['anonymous_user_id'] == doc['anonymous_user_id']
    assert sess['anonymous_user_id'] in config.used_anonymous_ids
    assert config.user_to_session_map[3701] == "a-h"
    assert config.user_to_session_map[PSS_MEMBER] == "a-h"
    assert config.user_states[3701] == UserState.IN_CONVERSATION
    assert config.user_states[PSS_MEMBER] == UserState.IN_CONVERSATION
    assert sm.get_anonymous_name("a-h", 3701) == "Peer Supporter"
    print("OK  h. an in-flight conversation is restored for both parties, service preserved")


def case_i_active_without_claimer():
    reset_state()
    stub = StubDB([active_doc("a-i", 3801, None, 5)])
    bot, sm, qm, em, _ = build(stub)

    stats = restore_state(qm, sm)
    assert stats['active_skipped'] == 1, stats
    assert stats['active_restored'] == 0
    assert "a-i" not in config.active_sessions
    assert 3801 not in config.user_to_session_map
    print("OK  i. an active doc with no claimer is skipped, not restored, and does not raise")


def case_j_pending_and_active_same_user():
    reset_state()
    stub = StubDB([
        active_doc("a-j", 3901, HF_MEMBER, 3),
        pending_doc("p-j", 3901, 40),
    ])
    bot, sm, qm, em, _ = build(stub)

    stats = restore_state(qm, sm)
    assert stats['active_restored'] == 1, stats
    assert stats['pending_restored'] == 0, stats
    assert stats['pending_skipped'] == 1, stats
    assert "a-j" in config.active_sessions
    assert "p-j" not in config.queue_entries
    assert 3901 not in config.user_to_queue_map
    assert config.user_states[3901] == UserState.IN_CONVERSATION
    assert stub.end_reason_for("p-j") == 'superseded_by_active', stub.ended
    print("OK  j. active wins over a stale pending row for the same user, which is closed")


def case_k_db_unavailable():
    reset_state()
    stub = StubDB([pending_doc("p-k", 4001, 5)], db_available=False)
    bot, sm, qm, em, _ = build(stub)

    stats = restore_state(qm, sm)
    assert stats['db_available'] is False, stats
    assert stats['pending_restored'] == 0
    assert not config.queue_entries
    assert not config.active_sessions
    assert not config.user_to_queue_map
    assert not stub.calls, "no query should be attempted with the DB down"
    print("OK  k. Mongo down: restore is a loud no-op and the bot still boots")


def case_l_pending_query_raises():
    reset_state()
    stub = StubDB([pending_doc("p-l", 4101, 5)])
    stub.raise_on_pending = True
    bot, sm, qm, em, _ = build(stub)

    stats = restore_state(qm, sm)
    assert stats['aborted'] is True, stats
    assert not config.queue_entries
    assert not config.active_sessions
    print("OK  l. a Mongo error is caught, aborts rehydration, and never blocks boot")


async def case_m_circuit_breaker():
    reset_state()
    docs = [pending_doc(f"p-m{i}", 4200 + i, 5) for i in range(60)]
    stub = StubDB(docs)
    bot, sm, qm, em, _ = build(stub)

    original = config.RESTORE_MAX_PENDING
    config.RESTORE_MAX_PENDING = 50
    try:
        stats = restore_state(qm, sm)
    finally:
        config.RESTORE_MAX_PENDING = original

    assert stats['aborted'] is True, stats
    assert stats['pending_restored'] == 0
    assert not config.queue_entries, "nothing may be restored past the circuit breaker"
    assert not stub.ended, "and nothing may be closed either"
    await qm.sweep_expired_queues()
    assert not bot.sent, "nobody may be messaged"
    print("OK  m. the circuit breaker aborts rehydration wholesale and messages nobody")


def case_n_dry_run():
    reset_state()
    stub = StubDB([
        pending_doc("p-n", 4301, 5),
        pending_doc("p-n-old", 4302, 30 * 24 * 60),
        active_doc("a-n", 4303, HF_MEMBER, 5),
    ])
    bot, sm, qm, em, _ = build(stub)

    original = config.RESTORE_DRY_RUN
    config.RESTORE_DRY_RUN = True
    try:
        stats = restore_state(qm, sm)
    finally:
        config.RESTORE_DRY_RUN = original

    assert stats['dry_run'] is True, stats
    assert stats['pending_restored'] == 1, stats
    assert stats['pending_stale_closed'] == 1, stats
    assert stats['active_restored'] == 1, stats
    # ...but nothing actually happened.
    assert not config.queue_entries
    assert not config.active_sessions
    assert not config.user_to_queue_map
    assert not config.user_to_session_map
    assert not config.user_states
    assert not stub.called('end_session'), "a dry run must not write to Mongo"
    print("OK  n. a dry run reports what it would do and mutates nothing")


async def case_o_claim_on_restored_entry():
    reset_state()
    doc = pending_doc("p-o", 4401, 10)
    original_anon = doc['anonymous_user_id']
    stub = StubDB([doc])
    bot, sm, qm, em, handlers = build(stub)

    restore_state(qm, sm)
    answers = []
    await handlers.handle_callback_query(
        make_callback_update(HF_MEMBER, "claim_p-o", answers), SimpleNamespace(bot=bot))

    claims = stub.called('claim_session')
    assert claims and claims[0][1] == "p-o", claims
    session_id = sm.get_session_by_user(4401)
    assert session_id == "p-o"
    assert config.user_states[4401] == UserState.IN_CONVERSATION
    assert config.user_states[HF_MEMBER] == UserState.IN_CONVERSATION
    # The resident must keep the SAME anonymous id they had before the restart.
    assert sm.get_session_info("p-o")['anonymous_user_id'] == original_anon
    # The surviving channel post is edited to CLAIMED, in its recorded channel.
    assert bot.edited and bot.edited[-1][0] == HF_CHANNEL
    assert "CLAIMED" in bot.edited[-1][2]
    print("OK  o. a member can claim a rehydrated entry; the resident keeps their identity")


def case_p_shared_anon_set():
    reset_state()
    bot, sm, qm, em, _ = build(StubDB([]))
    assert qm.used_anonymous_ids is sm.used_anonymous_ids is config.used_anonymous_ids
    print("OK  p. QueueManager and SessionManager share one anonymous-id set")


async def case_q_p0_regression_no_notification_storm():
    """The live bug: an active doc not in memory was re-expired and re-notified on
    every sweep, forever."""
    reset_state()
    doc = active_doc("a-q", 4501, HF_MEMBER, 45)   # HF dies at 30 min idle
    stub = StubDB([doc])
    stub.activity_ignores_status = True            # keep handing back the same doc
    bot, sm, qm, em, _ = build(stub)

    # Deliberately do NOT restore it: this is the doc rehydration skipped.
    assert "a-q" not in config.active_sessions

    await em.run_once()
    first_pass = list(bot.sent)
    assert len(first_pass) == 2, f"both parties told exactly once: {first_pass}"
    assert stub.called('end_session'), "the orphaned row must be closed in Mongo"
    assert stub.end_reason_for("a-q") == 'idle_expired', stub.ended

    bot.sent.clear()
    await em.run_once()
    assert bot.sent == [], (
        f"second sweep must send NOTHING; this is the storm bug: {bot.sent}")
    print("OK  q. P0 regression: an orphaned active session is closed once and never re-notified")


async def case_r_db_fallback_respects_each_service_window():
    """The DB-fallback branch queries at the SHORTEST timeout to get a superset, then
    re-filters per service. Both halves matter: an orphaned HF conversation must be
    caught promptly, and a PSS one at the same age must be left alone."""
    reset_state()
    stub = StubDB([
        active_doc("a-r-hf", 4601, HF_MEMBER, 45),                  # HF dies at 30
        active_doc("a-r-pss", 4602, PSS_MEMBER, 45, service="pss"),  # PSS dies at 1440
    ])
    stub.activity_ignores_status = True
    bot, sm, qm, em, _ = build(stub)

    await em.run_once()

    assert stub.end_reason_for("a-r-hf") == 'idle_expired', (
        "a 45-minute-idle orphaned HF conversation must be closed, not left for 24h")
    assert bot.texts_to(4601), "its requester must be told"
    assert "a-r-pss" not in stub.ended, (
        "a 45-minute-idle PSS conversation is nowhere near its 24h window")
    assert not bot.texts_to(4602)
    print("OK  r. the DB-fallback sweep catches HF orphans promptly and spares PSS ones")


async def _boot(docs, **cfg):
    """main()'s boot steps 3 and 4, verbatim: restore, then the two expiry passes
    that run BEFORE polling starts."""
    reset_state()
    saved = {k: getattr(config, k) for k in cfg}
    for k, v in cfg.items():
        setattr(config, k, v)
    try:
        stub = StubDB(docs)
        stub.activity_ignores_status = True   # Mongo keeps handing back 'active' rows
        bot, sm, qm, em, _ = build(stub)
        restore_state(qm, sm)                 # step 3
        await qm.sweep_expired_queues()       # step 4a
        await em.run_once()                   # step 4b
        return bot, stub
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


async def case_s_ancient_active_sessions_are_closed_silently():
    """Boot must never DM the two parties of a months-old status='active' row.

    The collection holds these today: every deploy that landed mid-conversation left
    one, and until the T3.0 fix they were re-notified every 3 minutes. Closing them is
    right; telling a mental-health helpline's past users "your conversation has been
    automatically closed" months later is not.

    Critically, this must hold under the three restore safety switches too. Each of
    them leaves active_sessions EMPTY, which makes every ancient row look like an
    orphan to the expiry sweep's DB-fallback branch -- so before the stale horizon
    existed, turning a safety switch ON strictly increased the number of DMs sent.
    """
    ancient = [active_doc(f"old-{i}", 5000 + i, HF_MEMBER + i, 90 * 24 * 60)
               for i in range(5)]

    for label, docs, cfg in [
        ("normal boot", ancient, {}),
        ("RESTORE_DRY_RUN=true", ancient, {"RESTORE_DRY_RUN": True}),
        ("RESTORE_ENABLED=false", ancient, {"RESTORE_ENABLED": False}),
        ("circuit breaker tripped",
         [active_doc(f"old-{i}", 5000 + i, HF_MEMBER + i, 90 * 24 * 60) for i in range(60)],
         {}),
    ]:
        bot, stub = await _boot([dict(d) for d in docs], **cfg)
        assert bot.sent == [], (
            f"{label}: boot DMed {len(bot.sent)} people about months-old sessions: "
            f"{bot.sent[:3]}")
        # ...but the rows must still be closed, or the storm just continues.
        assert stub.called('end_session'), f"{label}: ancient rows must still be closed"

    print("OK  s. months-old active sessions are closed silently, under every restore switch")


async def case_t_recently_dead_sessions_still_notify():
    """The horizon must not become a blanket mute: a conversation that died while the
    bot was briefly down is inside the grace window and its parties DO get told."""
    reset_state()
    # HF: dies at 30 min idle, horizon is 30 + STALE_NOTIFY_GRACE_MINUTES (120) = 150.
    doc = active_doc("a-t", 4701, HF_MEMBER, 40)      # past the window, well inside grace
    stub = StubDB([doc])
    stub.activity_ignores_status = True
    bot, sm, qm, em, _ = build(stub)

    await em.run_once()

    assert stub.end_reason_for("a-t") == 'idle_expired', stub.ended
    assert bot.texts_to(4701), "a conversation that died 40 min ago must still be announced"
    assert bot.texts_to(HF_MEMBER), "and so must its companion"
    print("OK  t. a session that died inside the grace window is still announced to both parties")


async def case_u_exactly_once_across_two_instances():
    """Deploy rollback briefly runs two containers. Both rehydrate the SAME document
    into their own memory and both expire it. Only the instance that wins the atomic
    find_one_and_update may notify -- otherwise both parties are told twice.

    Two processes sharing one Mongo are modelled here as two _expire_session passes
    over one StubDB, re-seeding active_sessions in between to stand in for the second
    container's independent memory.
    """
    reset_state()
    doc = active_doc("a-u", 4801, HF_MEMBER, 40)
    stub = StubDB([doc])
    bot, sm, qm, em, _ = build(stub)

    # Instance A: session is in ITS memory.
    restore_state(qm, sm)
    assert "a-u" in config.active_sessions
    await em.run_once()
    first = list(bot.sent)
    assert len(first) == 2, f"instance A tells both parties exactly once: {first}"

    # Instance B: same document, its own memory, same Mongo.
    config.active_sessions["a-u"] = {
        'user_id': 4801, 'heartfelt_member_id': HF_MEMBER,
        'created_at': doc['created_at'], 'last_activity_at': doc['last_activity_at'],
        'anonymous_user_id': doc['anonymous_user_id'], 'service': 'hf',
    }
    bot.sent.clear()
    await em.run_once()
    assert bot.sent == [], (
        f"instance B lost the atomic close and must stay silent; these are duplicate "
        f"DMs to real people: {bot.sent}")
    print("OK  u. a second instance that loses the atomic close never re-notifies")


def case_v_end_session_return_is_a_commit_signal():
    """db_mgr.end_session's return value is the exactly-once gate on messaging real
    people, so it must mean "did the atomic transition commit?" and nothing else.

    Once find_one_and_update has committed, the row IS closed. If a later bookkeeping
    step could still make the function return False, callers
    (queue.sweep_expired_queues, expiry._expire_session) would read that as "someone
    else closed it, stay quiet" -- ending the row in Mongo and then telling the
    requester nothing at all. That is precisely what the pre-branch code did: its
    duration math raised on every pending row, after the commit.
    """
    import datetime as _dt
    from types import SimpleNamespace as _NS
    import src.database.manager as M
    from src.timeutil import UTC as _UTC

    class Coll:
        def __init__(self, boom):
            self.boom = boom
            self.updated = []

        def find_one_and_update(self, filt, upd, **kw):
            # A PENDING row, exactly as create_session writes it: claimed_at is
            # PRESENT and None, which is what broke the old `.get(k, default)`.
            return {'session_id': 's1', 'claimed_at': None,
                    'created_at': _dt.datetime(2026, 1, 1, tzinfo=_UTC)}

        def update_one(self, filt, upd):
            if self.boom:
                raise RuntimeError("mongo blip on the duration write")
            self.updated.append(upd)
            return _NS(matched_count=1)

    real_conn, real_avail = M.db_manager, M.db_mgr.db_available
    try:
        for boom in (False, True):
            M.db_manager = _NS(db=_NS(sessions=Coll(boom)))
            M.db_mgr.db_available = True
            got = M.db_mgr.end_session('s1', None, system_end=True,
                                       end_reason='queue_expired')
            assert got is True, (
                f"the row was committed as ended but end_session returned {got!r} "
                f"(duration write raising={boom}); every caller would silently skip "
                f"the user's notification")
    finally:
        M.db_manager, M.db_mgr.db_available = real_conn, real_avail

    print("OK  v. end_session returns True whenever the atomic close committed")


# --------------------------------------------------------------------------- runner
async def run():
    case_a_happy_pending()
    case_b_fifo()
    await case_c_legacy_doc_without_message_id()
    case_d_naive_created_at()
    await case_e_expired_during_deploy()
    await case_f_ancient_pending()
    await case_g_pss_pending_not_expired()
    case_h_happy_active()
    case_i_active_without_claimer()
    case_j_pending_and_active_same_user()
    case_k_db_unavailable()
    case_l_pending_query_raises()
    await case_m_circuit_breaker()
    case_n_dry_run()
    await case_o_claim_on_restored_entry()
    case_p_shared_anon_set()
    await case_q_p0_regression_no_notification_storm()
    await case_r_db_fallback_respects_each_service_window()
    await case_s_ancient_active_sessions_are_closed_silently()
    await case_t_recently_dead_sessions_still_notify()
    await case_u_exactly_once_across_two_instances()
    case_v_end_session_return_is_a_commit_signal()


if __name__ == "__main__":
    real = (restore_mod.db_mgr, queue_mod.db_mgr, session_mod.db_mgr, expiry_mod.db_mgr)
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]
    saved = (hf.channel_id, hf.enabled, pss.channel_id, pss.enabled)
    enable_both_services()
    try:
        asyncio.run(run())
        print("\nAll restart-durability assertions passed!")
    finally:
        (restore_mod.db_mgr, queue_mod.db_mgr,
         session_mod.db_mgr, expiry_mod.db_mgr) = real
        hf.channel_id, hf.enabled, pss.channel_id, pss.enabled = saved
        hf.roster.replace(config.DEFAULT_HEARTFELT_MEMBERS)
        pss.roster.replace([])
        reset_state()
