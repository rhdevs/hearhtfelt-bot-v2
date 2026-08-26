#!/usr/bin/env python3
"""
Ending and handing back (Phase 6).

Two rules decide everything in this file:

  * /end is the REQUESTER's. A supporter who has to stop cannot close the
    conversation out from under someone who is still talking -- they use /release,
    which keeps the request alive and finds that person somebody else.
  * /release is active -> pending, NOT end_session. The request is not over. And a
    request whose requester deliberately kept it OFF the channel does not go to the
    channel because the supporter they chose had to step away.

Run directly: `python tests/test_release_and_end.py`
"""

import asyncio
import datetime
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from config import ServiceType, UserState, closing_text
from src.timeutil import utcnow

import src.bot.handlers as handlers_mod
import src.bot.managers.expiry as expiry_mod
import src.bot.managers.queue as queue_mod
import src.bot.managers.session as session_mod
import src.bot.restore as restore_mod
from src.bot.handlers import BotHandlers
from src.bot.managers.queue import QueueManager
from src.bot.managers.session import SessionManager

REQUESTER = 5000
MEMBER = 3000
SECOND_MEMBER = 3001
PSS_CHANNEL = "-100PSS"
HF_CHANNEL = "-100HF"

# Nothing the requester reads may suggest they were abandoned, and nothing the
# releasing member reads may suggest they failed.
BLAME_WORDS = ("dropped", "left you", "abandoned", "gave up", "walked out",
               "declin", "rejected")


class StubDB:
    """Recording db_mgr with real filters on the two transitions that matter."""

    def __init__(self, db_available=True):
        self.db_available = db_available
        self.docs = {}
        self.calls = []
        self.ended = {}

    def create_session(self, user_id, description, anonymous_user_id, session_id=None,
                       user_telehandle=None, service="hf", routing="open"):
        self.calls.append(('create_session', session_id, routing))
        now = utcnow()
        self.docs[session_id] = {
            'session_id': session_id, 'user_id': user_id, 'service': service,
            'status': 'pending', 'description': description,
            'anonymous_user_id': anonymous_user_id, 'created_at': now,
            'waiting_since': now, 'last_activity_at': now, 'claimed_at': None,
            'heartfelt_member_id': None, 'heartfelt_member_telehandle': None,
            'queue_channel_id': None, 'queue_message_id': None,
            'routing': routing, 'target_member_id': None, 'directed_at': None,
            'directed_message_id': None, 'notice_channel_id': None,
            'notice_message_id': None, 'declined_by': [],
        }
        return session_id

    def claim_session(self, session_id, member_id, telehandle=None):
        self.calls.append(('claim_session', session_id, member_id))
        doc = self.docs.get(session_id)
        if doc is None or doc.get('status') != 'pending':
            return False
        doc.update(status='active', heartfelt_member_id=member_id,
                   heartfelt_member_telehandle=telehandle, claimed_at=utcnow())
        return True

    def end_session(self, session_id, ended_by_user_id, system_end=False, end_reason=None):
        self.calls.append(('end_session', session_id, ended_by_user_id, system_end,
                           end_reason))
        doc = self.docs.get(session_id)
        if session_id in self.ended:
            return False
        if doc is not None and doc.get('status') not in ('pending', 'active'):
            return False
        self.ended[session_id] = end_reason
        if doc is not None:
            doc['status'] = 'ended'
        return True

    def release_session(self, session_id, member_id):
        self.calls.append(('release_session', session_id, member_id))
        doc = self.docs.get(session_id)
        # The real filter: only an ACTIVE row, and only by the member holding it.
        if (doc is None or doc.get('status') != 'active'
                or doc.get('heartfelt_member_id') != int(member_id)):
            return None
        now = utcnow()
        doc.update(status='pending', heartfelt_member_id=None,
                   heartfelt_member_telehandle=None, claimed_at=None,
                   waiting_since=now, released_at=now)
        doc['release_count'] = doc.get('release_count', 0) + 1
        for key in ('released_by', 'declined_by'):
            doc.setdefault(key, [])
            if int(member_id) not in doc[key]:
                doc[key].append(int(member_id))
        return dict(doc)

    def direct_session(self, session_id, member_id):
        self.calls.append(('direct_session', session_id, member_id))
        doc = self.docs.get(session_id)
        if (doc is None or doc.get('status') != 'pending'
                or doc.get('routing') != 'choosing'):
            return None
        doc.update(routing='directed', target_member_id=int(member_id),
                   directed_at=utcnow())
        return dict(doc)

    def undirect_session(self, session_id, member_id, reason=None):
        self.calls.append(('undirect_session', session_id, member_id, reason))
        doc = self.docs.get(session_id)
        if (doc is None or doc.get('status') != 'pending'
                or doc.get('routing') != 'directed'
                or doc.get('target_member_id') != int(member_id)):
            return None
        doc.update(routing='choosing', target_member_id=None, directed_at=None,
                   waiting_since=utcnow(), last_undirect_reason=reason)
        doc.setdefault('declined_by', [])
        if int(member_id) not in doc['declined_by']:
            doc['declined_by'].append(int(member_id))
        return dict(doc)

    def open_session(self, session_id):
        self.calls.append(('open_session', session_id))
        return self.docs.get(session_id)

    def get_session(self, session_id):
        self.calls.append(('get_session', session_id))
        return self.docs.get(session_id)

    def set_queue_message(self, session_id, channel_id, message_id):
        self.calls.append(('set_queue_message', session_id, channel_id, message_id))
        return True

    def set_directed_message(self, session_id, message_id):
        self.calls.append(('set_directed_message', session_id, message_id))
        return True

    def set_directed_notice(self, session_id, channel_id, message_id):
        self.calls.append(('set_directed_notice', session_id, channel_id, message_id))
        return True

    def mark_member_started(self, member_id, started=True, collection=None):
        self.calls.append(('mark_member_started', int(member_id), bool(started)))
        return True

    def log_message(self, **kw):
        self.calls.append(('log_message', kw.get('session_id')))
        return True

    def update_session_activity(self, session_id):
        return True

    def get_pending_sessions(self):
        return [d for d in self.docs.values() if d.get('status') == 'pending']

    def get_active_sessions(self):
        return [d for d in self.docs.values() if d.get('status') == 'active']

    def get_sessions_by_activity(self, cutoff):
        return []

    def names(self):
        return [c[0] for c in self.calls]


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []
        self.markup_edits = []
        self.fail_for = {}
        self._mid = 0

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if str(chat_id) in self.fail_for:
            raise RuntimeError(self.fail_for[str(chat_id)])
        self._mid += 1
        self.sent.append((str(chat_id), text, reply_markup))
        return SimpleNamespace(message_id=self._mid)

    async def edit_message_text(self, chat_id=None, message_id=None, text=None, **kw):
        self.edited.append((str(chat_id), message_id, text))

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, **kw):
        self.markup_edits.append((str(chat_id), message_id))

    async def delete_message(self, **kw):
        return None

    def texts_to(self, user_id):
        return [t for c, t, _m in self.sent if c == str(user_id)]


class Rec:
    def __init__(self):
        self.replies = []
        self.answers = []


def text_update(user_id, text, rec):
    async def reply_text(t, reply_markup=None, **kw):
        rec.replies.append((t, reply_markup))
    user = SimpleNamespace(id=user_id, username=None, first_name="User", last_name=None)
    msg = SimpleNamespace(text=text, reply_text=reply_text, photo=[], sticker=None)
    return SimpleNamespace(effective_user=user, message=msg, callback_query=None,
                           effective_chat=SimpleNamespace(id=user_id, type="private"))


def cb_update(user_id, data, rec):
    async def answer(t=None, show_alert=False):
        rec.answers.append((t, show_alert))

    async def edit_message_text(t, reply_markup=None, **kw):
        rec.replies.append((t, reply_markup))

    async def edit_message_reply_markup(reply_markup=None, **kw):
        return None

    fu = SimpleNamespace(id=user_id, username="mem", first_name="Mem", last_name=None)
    q = SimpleNamespace(from_user=fu, data=data, answer=answer,
                        edit_message_text=edit_message_text,
                        edit_message_reply_markup=edit_message_reply_markup)
    return SimpleNamespace(effective_user=fu, message=None, callback_query=q,
                           effective_chat=SimpleNamespace(id=user_id, type="private"))


def reset_state():
    for d in (config.user_states, config.user_to_service_map, config.queue_entries,
              config.user_to_queue_map, config.active_sessions,
              config.user_to_session_map, config.session_warnings,
              config.directed_by_member, config.picker_views):
        d.clear()
    config.queue_order.clear()
    config.used_anonymous_ids.clear()
    config.safety_logs.clear()


def install(stub):
    handlers_mod.db_mgr = stub
    queue_mod.db_mgr = stub
    session_mod.db_mgr = stub
    restore_mod.db_mgr = stub
    expiry_mod.db_mgr = stub


def build(pickable, db_available=True, extra_members=()):
    """PSS-only world. `pickable` decides which lane a new request takes: with a
    profiled supporter the requester is offered the picker (directed provenance),
    with a bare-int roster they go straight to the channel (open provenance)."""
    reset_state()
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]
    hf.channel_id, hf.enabled = HF_CHANNEL, False
    pss.channel_id, pss.enabled = PSS_CHANNEL, True
    hf.roster.replace_records([])

    ids = [MEMBER] + list(extra_members)
    if pickable:
        pss.roster.replace_records([
            {'telegram_id': mid, 'display_name': "Sup %d" % mid, 'available': True,
             'has_started_bot': True, 'active': True} for mid in ids])
    else:
        pss.roster.replace_records([])
        pss.roster.replace(ids)

    stub = StubDB(db_available=db_available)
    install(stub)
    bot = FakeBot()
    sm = SessionManager(bot)
    qm = QueueManager(bot)
    handlers = BotHandlers(sm, qm)
    return bot, sm, qm, handlers, stub


async def open_conversation(handlers, bot, rec, ctx, claimer=MEMBER):
    """Requester asks, the request goes to the channel, `claimer` claims it."""
    await handlers.chat_command(text_update(REQUESTER, "/chat", rec), ctx)
    await handlers.handle_message(
        text_update(REQUESTER, "my flatmate situation is awful", rec), ctx)
    queue_id = config.user_to_queue_map[REQUESTER]
    assert config.queue_entries[queue_id]['routing'] == 'open'
    await handlers.handle_callback_query(cb_update(claimer, "claim_%s" % queue_id, rec), ctx)
    assert config.user_to_session_map.get(REQUESTER) == queue_id, "claim failed"
    return queue_id


async def directed_conversation(handlers, bot, rec, ctx):
    """Requester picks one supporter, who accepts."""
    await handlers.chat_command(text_update(REQUESTER, "/chat", rec), ctx)
    await handlers.handle_message(
        text_update(REQUESTER, "my flatmate situation is awful", rec), ctx)
    queue_id = config.user_to_queue_map[REQUESTER]
    assert config.queue_entries[queue_id]['routing'] == 'choosing'
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % MEMBER, rec), ctx)
    await handlers.handle_callback_query(
        cb_update(MEMBER, "dr_a:%s" % queue_id, rec), ctx)
    assert config.user_to_session_map.get(REQUESTER) == queue_id, "accept failed"
    return queue_id


def assert_no_blame(texts, who):
    for text in texts:
        lowered = text.lower()
        for word in BLAME_WORDS:
            assert word not in lowered, (
                "%s must never read %r: %r" % (who, word, text))


# --------------------------------------------------------------------------- cases
async def case_a_a_member_cannot_end_the_conversation():
    """M14. Ending is the requester's decision. A supporter who has to stop must not
    be able to close it out from under somebody who is still talking."""
    bot, sm, qm, handlers, stub = build(pickable=False)
    rec = Rec()
    ctx = SimpleNamespace(bot=bot)
    queue_id = await open_conversation(handlers, bot, rec, ctx)
    sent_before = list(bot.sent)

    await handlers.end_command(text_update(MEMBER, "/end", rec), ctx)

    assert rec.replies[-1][0] == config.MESSAGES["end_is_requester_only"], rec.replies[-1]
    assert "/release" in rec.replies[-1][0], "and it must point them at /release"
    assert sm.get_session_by_user(REQUESTER) == queue_id, "the session SURVIVES"
    assert sm.get_session_by_user(MEMBER) == queue_id
    assert config.user_states[REQUESTER] == UserState.IN_CONVERSATION
    assert stub.docs[queue_id]['status'] == 'active'
    assert 'end_session' not in stub.names(), stub.calls
    assert bot.sent == sent_before, (
        "and NOBODY else is messaged: %r" % (bot.sent[len(sent_before):],))
    print("OK  a. a member's /end is refused, the session survives, nobody is messaged")


async def case_b_the_requester_can_end():
    bot, sm, qm, handlers, stub = build(pickable=False)
    rec = Rec()
    ctx = SimpleNamespace(bot=bot)
    queue_id = await open_conversation(handlers, bot, rec, ctx)

    await handlers.end_command(text_update(REQUESTER, "/end", rec), ctx)

    assert sm.get_session_by_user(REQUESTER) is None
    assert sm.get_session_by_user(MEMBER) is None
    assert config.user_states[REQUESTER] == UserState.IDLE
    assert config.user_states[MEMBER] == UserState.IDLE
    assert stub.ended.get(queue_id) == 'user_ended', stub.ended
    assert config.MESSAGES["conversation_ended"] in bot.texts_to(REQUESTER)
    assert config.MESSAGES["conversation_ended_heartfelt"] in bot.texts_to(MEMBER)
    print("OK  b. the requester's /end closes the conversation and tells both sides")


async def case_c_a_requester_cannot_release():
    bot, sm, qm, handlers, stub = build(pickable=False)
    rec = Rec()
    ctx = SimpleNamespace(bot=bot)
    queue_id = await open_conversation(handlers, bot, rec, ctx)
    sent_before = list(bot.sent)

    await handlers.release_command(text_update(REQUESTER, "/release", rec), ctx)

    assert rec.replies[-1][0] == config.MESSAGES["release_is_member_only"]
    assert "/end" in rec.replies[-1][0], "and it points them at the command they want"
    assert sm.get_session_by_user(REQUESTER) == queue_id, "the session survives"
    assert 'release_session' not in stub.names(), stub.calls
    assert bot.sent == sent_before
    print("OK  c. a requester's /release is refused and changes nothing")


async def case_d_release_on_open_provenance_goes_back_to_the_channel():
    """M15. release_session, NOT end_session: the request is not over."""
    bot, sm, qm, handlers, stub = build(pickable=False, extra_members=[SECOND_MEMBER])
    rec = Rec()
    ctx = SimpleNamespace(bot=bot)
    queue_id = await open_conversation(handlers, bot, rec, ctx)
    anon = stub.docs[queue_id]['anonymous_user_id']

    await handlers.release_command(text_update(MEMBER, "/release", rec), ctx)

    assert 'release_session' in stub.names(), stub.calls
    assert 'end_session' not in stub.names(), (
        "end_session would flip the row to 'ended' and the person who asked for help "
        "would silently stop existing as far as the queue is concerned", stub.calls)
    doc = stub.docs[queue_id]
    assert doc['status'] == 'pending', doc['status']
    assert doc['claimed_at'] is None, (
        "claimed_at MUST reset, or the eventual end_session measures duration from "
        "the abandoned stint")
    assert doc['heartfelt_member_id'] is None
    assert doc['release_count'] == 1
    assert MEMBER in doc['released_by'] and MEMBER in doc['declined_by']

    assert sm.get_session_by_user(REQUESTER) is None
    assert sm.get_session_by_user(MEMBER) is None
    assert config.user_states[MEMBER] == UserState.IDLE
    assert config.user_states[REQUESTER] == UserState.IN_QUEUE
    assert config.user_to_queue_map[REQUESTER] == queue_id
    assert queue_id in config.queue_order

    posts = [s for s in bot.sent if s[0] == PSS_CHANNEL and s[2] is not None]
    assert len(posts) == 2, ("a fresh channel post with a live Claim button", posts)
    assert anon in posts[-1][1], (
        "the SAME anonymous id: a new one would read as a new person asking")
    assert posts[-1][2].inline_keyboard[0][0].callback_data == "claim_%s" % queue_id

    assert config.MESSAGES["released_to_queue"] in bot.texts_to(REQUESTER)
    assert rec.replies[-1][0] == config.MESSAGES["released_member"]
    assert_no_blame(bot.texts_to(REQUESTER), "the requester")
    assert_no_blame([t for t, _m in rec.replies], "the member")
    assert 'released' in config.queue_entries[queue_id]
    assert [log for log in config.safety_logs
            if log.get('action') == 'session_released'], config.safety_logs
    print("OK  d. /release on an open request returns it to the channel, not to 'ended'")


async def case_e_release_on_directed_provenance_never_touches_the_channel():
    """The consent guard. This requester deliberately kept their message off the
    channel; their supporter walking away does not change that."""
    bot, sm, qm, handlers, stub = build(pickable=True, extra_members=[SECOND_MEMBER])
    rec = Rec()
    ctx = SimpleNamespace(bot=bot)
    queue_id = await directed_conversation(handlers, bot, rec, ctx)
    assert stub.docs[queue_id]['routing'] == 'directed', "provenance survives the claim"
    channel_before = [s for s in bot.sent if s[0] == PSS_CHANNEL]

    await handlers.release_command(text_update(MEMBER, "/release", rec), ctx)

    channel_after = [s for s in bot.sent if s[0] == PSS_CHANNEL]
    assert channel_after == channel_before, (
        "pushing this request to the channel because their chosen supporter left is "
        "exactly the reroute that must never happen: %r"
        % (channel_after[len(channel_before):],))
    assert config.user_states[REQUESTER] == UserState.CHOOSING_SUPPORTER
    assert queue_id not in config.queue_order
    assert config.queue_entries[queue_id]['routing'] == 'choosing'
    assert stub.docs[queue_id]['routing'] == 'choosing', (
        "persisted too, or a restart rehydrates this as still sitting with the "
        "supporter who walked away")
    assert MEMBER in stub.docs[queue_id]['declined_by'], (
        "the releaser must not be immediately re-offered")
    assert MEMBER in config.queue_entries[queue_id]['declined_by']

    to_requester = bot.texts_to(REQUESTER)
    assert any(config.MESSAGES["released_choose_again"] in t for t in to_requester)
    assert any(config.MESSAGES["next_step_question"] in t for t in to_requester)
    prompt = [s for s in bot.sent
              if s[0] == str(REQUESTER) and config.MESSAGES["next_step_question"] in s[1]]
    datas = sorted(b.callback_data for row in prompt[-1][2].inline_keyboard for b in row)
    assert datas == ["pk_l:0", "pk_o", "pk_x"], datas
    assert_no_blame(to_requester, "the requester")

    # And the picker really does exclude the person who just released.
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    text = [t for t, _m in rec.replies][-1]
    assert "Sup %d" % MEMBER not in text, text
    assert "Sup %d" % SECOND_MEMBER in text, text
    print("OK  e. /release on a directed request stays off the channel and re-offers the choice")


async def case_f_a_lost_release_race_tells_the_requester_nothing():
    bot, sm, qm, handlers, stub = build(pickable=False)
    rec = Rec()
    ctx = SimpleNamespace(bot=bot)
    queue_id = await open_conversation(handlers, bot, rec, ctx)

    # Somebody else already ended it: the atomic filter will match nothing.
    stub.docs[queue_id]['status'] = 'ended'
    sent_before = list(bot.sent)

    await handlers.release_command(text_update(MEMBER, "/release", rec), ctx)

    assert rec.replies[-1][0] == config.MESSAGES["no_active_conversation"]
    assert bot.sent == sent_before, (
        "the requester did not ask for any of this and must hear NOTHING when the "
        "release lost its race: %r" % (bot.sent[len(sent_before):],))
    assert queue_id not in config.queue_entries, config.queue_entries
    assert sm.get_session_by_user(REQUESTER) == queue_id, (
        "and nothing local was torn down either")
    print("OK  f. a release that loses its atomic race messages the requester not at all")


async def case_g_release_requires_mongo():
    bot, sm, qm, handlers, stub = build(pickable=False)
    rec = Rec()
    ctx = SimpleNamespace(bot=bot)
    queue_id = await open_conversation(handlers, bot, rec, ctx)
    stub.db_available = False
    sent_before = list(bot.sent)

    await handlers.release_command(text_update(MEMBER, "/release", rec), ctx)

    assert rec.replies[-1][0] == config.MESSAGES["release_unavailable"]
    assert 'release_session' not in stub.names(), stub.calls
    assert sm.get_session_by_user(REQUESTER) == queue_id, "the session survives"
    assert bot.sent == sent_before
    print("OK  g. /release refuses without Mongo, because the description lives only there")


async def case_h_a_released_request_can_be_claimed_again():
    bot, sm, qm, handlers, stub = build(pickable=False, extra_members=[SECOND_MEMBER])
    rec = Rec()
    ctx = SimpleNamespace(bot=bot)
    queue_id = await open_conversation(handlers, bot, rec, ctx)
    anon = sm.get_session_info(queue_id)['anonymous_user_id']
    description = stub.docs[queue_id]['description']

    await handlers.release_command(text_update(MEMBER, "/release", rec), ctx)
    posts = [s for s in bot.sent if s[0] == PSS_CHANNEL and s[2] is not None]
    assert description in posts[-1][1], (
        "the description is carried across from the document, which is the whole "
        "reason /release needs Mongo")

    await handlers.handle_callback_query(
        cb_update(SECOND_MEMBER, "claim_%s" % queue_id, rec), ctx)

    assert sm.get_session_by_user(REQUESTER) == queue_id
    assert sm.get_session_by_user(SECOND_MEMBER) == queue_id
    assert sm.get_session_info(queue_id)['anonymous_user_id'] == anon, (
        "one request, one anonymous id, across both stints")
    assert stub.docs[queue_id]['status'] == 'active'
    assert stub.docs[queue_id]['heartfelt_member_id'] == SECOND_MEMBER
    print("OK  h. a released request is claimable end to end, with the same anonymous id")


def case_i_closing_text_appends_only_when_configured():
    reset_state()
    pss = config.SERVICES[ServiceType.PSS.value]
    saved = (pss.closing_extra, pss.closing_extra_member)
    try:
        pss.closing_extra = ""
        pss.closing_extra_member = ""
        assert closing_text("pss", for_member=False) == config.MESSAGES["conversation_ended"], (
            "empty is the default and MUST leave today's message byte-identical")
        assert closing_text("pss", for_member=True) == \
            config.MESSAGES["conversation_ended_heartfelt"]
        assert closing_text("hf", for_member=False) == config.MESSAGES["conversation_ended"]
        assert closing_text(None, for_member=False) == config.MESSAGES["conversation_ended"]

        pss.closing_extra = "REQUESTER NOTE"
        pss.closing_extra_member = "MEMBER NOTE"
        requester_copy = closing_text("pss", for_member=False)
        member_copy = closing_text("pss", for_member=True)
        assert requester_copy.endswith("\n\nREQUESTER NOTE"), requester_copy
        assert member_copy.endswith("\n\nMEMBER NOTE"), member_copy
        assert "MEMBER NOTE" not in requester_copy, "the two notes must not cross over"
        assert "REQUESTER NOTE" not in member_copy
        assert closing_text("hf", for_member=False) == config.MESSAGES["conversation_ended"], (
            "HF is untouched by PSS configuration")
    finally:
        pss.closing_extra, pss.closing_extra_member = saved
    print("OK  i. closing_text appends the per-track note only when one is configured")


async def case_j_closing_copy_follows_the_session_role_not_the_roster():
    """A supporter can themselves ask for support. A roster lookup then hands them the
    "thank you for helping someone today" copy for a conversation in which they were
    the one asking. Session roles are strictly more correct."""
    bot, sm, qm, handlers, stub = build(pickable=False, extra_members=[SECOND_MEMBER])
    # The REQUESTER is also on the roster.
    config.SERVICES[ServiceType.PSS.value].roster.add(REQUESTER)
    assert config.is_any_member(REQUESTER) is True, "the fixture must actually bite"
    assert config.is_heartfelt_member(REQUESTER) is True

    rec = Rec()
    ctx = SimpleNamespace(bot=bot)
    queue_id = await open_conversation(handlers, bot, rec, ctx)

    await handlers.end_command(text_update(REQUESTER, "/end", rec), ctx)

    to_requester = bot.texts_to(REQUESTER)
    to_member = bot.texts_to(MEMBER)
    assert config.MESSAGES["conversation_ended"] in to_requester, (
        "the person who ASKED gets the requester copy, even though they happen to be "
        "on a roster", to_requester)
    assert config.MESSAGES["conversation_ended_heartfelt"] not in to_requester, (
        "a roster lookup would have sent them 'thank you for helping someone today' "
        "for a conversation in which they were the one asking")
    assert config.MESSAGES["conversation_ended_heartfelt"] in to_member, to_member
    print("OK  j. closing copy is chosen by session role, not by a roster lookup")


# --------------------------------------------------------------------------- runner
CASES = [
    case_a_a_member_cannot_end_the_conversation,
    case_b_the_requester_can_end,
    case_c_a_requester_cannot_release,
    case_d_release_on_open_provenance_goes_back_to_the_channel,
    case_e_release_on_directed_provenance_never_touches_the_channel,
    case_f_a_lost_release_race_tells_the_requester_nothing,
    case_g_release_requires_mongo,
    case_h_a_released_request_can_be_claimed_again,
    case_i_closing_text_appends_only_when_configured,
    case_j_closing_copy_follows_the_session_role_not_the_roster,
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
    assert len(CASES) >= 10, (
        "expected at least 10 cases, collected %d (%s). Test discovery has "
        "regressed -- fix the discovery, do not lower this number."
        % (len(CASES), ", ".join(c.__name__ for c in CASES) or "none")
    )

    real = (handlers_mod.db_mgr, queue_mod.db_mgr, session_mod.db_mgr,
            restore_mod.db_mgr, expiry_mod.db_mgr)
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]
    saved = (hf.channel_id, hf.enabled, pss.channel_id, pss.enabled,
             pss.closing_extra, pss.closing_extra_member)
    try:
        asyncio.run(run())
        print("\nAll %d release/end assertions passed!" % len(CASES))
    finally:
        (handlers_mod.db_mgr, queue_mod.db_mgr, session_mod.db_mgr,
         restore_mod.db_mgr, expiry_mod.db_mgr) = real
        (hf.channel_id, hf.enabled, pss.channel_id, pss.enabled,
         pss.closing_extra, pss.closing_extra_member) = saved
        # replace_records([]) FIRST: replace() with the same id set is a no-op that
        # keeps existing profiles.
        hf.roster.replace_records([])
        hf.roster.replace(config.DEFAULT_HEARTFELT_MEMBERS)
        pss.roster.replace_records([])
        pss.roster.replace([])
        reset_state()
