#!/usr/bin/env python3
"""
The PSS directed-support sub-branch (Phase 5).

Drives the REAL BotHandlers / QueueManager / SessionManager / restore_state against
a recording stub db_mgr whose transitions ENFORCE THEIR FILTERS, so a lost race
really does return None the way Mongo would.

This is the suite that decides whether the bot messages people it should not. Five
new send paths land in this feature, and the two that matter most here are:

  * case (i) -- a double-tapped picker button must produce exactly ONE supporter DM;
  * case (s) -- a bot that has been down for days must, on boot, close every
    mid-request row SILENTLY. Without the stale horizon it wakes up and DMs every
    person who was mid-request, months after the fact.

Run directly: `python tests/test_directed_requests.py`
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

import src.bot.handlers as handlers_mod
import src.bot.restore as restore_mod
import src.bot.managers.expiry as expiry_mod
import src.bot.managers.queue as queue_mod
import src.bot.managers.session as session_mod
from src.bot.handlers import BotHandlers
from src.bot.managers.queue import QueueManager
from src.bot.managers.session import SessionManager
from src.bot.restore import restore_state

REQUESTER = 5000
OTHER_MEMBER = 2999
PSS_CHANNEL = "-100PSS"
HF_CHANNEL = "-100HF"

# Words that must never reach the person who asked for help. A decline and a
# 24-hour silence are the same event as far as they are concerned.
FORBIDDEN_TO_REQUESTER = ("declin", "rejected", "turned you down", "said no",
                          "unavailable to you", "refused", "turned down")


# --------------------------------------------------------------------------- stubs
class StubDB:
    """Recording stand-in for db_mgr.

    Every routing transition enforces the SAME filter its real counterpart does. That
    is the whole point: if direct_session returned a document unconditionally, case
    (i) would pass while the production code sent two DMs.
    """

    def __init__(self, db_available=True):
        self.db_available = db_available
        self.docs = {}
        self.calls = []
        self.ended = {}
        self.started_flags = {}       # member_id -> bool, from mark_member_started

    # --- session lifecycle
    def create_session(self, user_id, description, anonymous_user_id, session_id=None,
                       user_telehandle=None, service="hf", routing="open"):
        self.calls.append(('create_session', session_id, routing))
        now = utcnow()
        self.docs[session_id] = {
            'session_id': session_id, 'user_id': user_id, 'service': service,
            'status': 'pending', 'description': description,
            'anonymous_user_id': anonymous_user_id,
            'created_at': now, 'waiting_since': now, 'last_activity_at': now,
            'claimed_at': None, 'heartfelt_member_id': None,
            'heartfelt_member_telehandle': None,
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
        doc['status'] = 'active'
        doc['heartfelt_member_id'] = member_id
        doc['claimed_at'] = utcnow()
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

    # --- routing transitions, each enforcing its filter
    def direct_session(self, session_id, member_id):
        self.calls.append(('direct_session', session_id, member_id))
        doc = self.docs.get(session_id)
        if (doc is None or doc.get('status') != 'pending'
                or doc.get('routing') != 'choosing'):
            return None
        doc.update(routing='directed', target_member_id=int(member_id),
                   directed_at=utcnow(), directed_message_id=None)
        return dict(doc)

    def undirect_session(self, session_id, member_id, reason=None):
        self.calls.append(('undirect_session', session_id, member_id, reason))
        doc = self.docs.get(session_id)
        if (doc is None or doc.get('status') != 'pending'
                or doc.get('routing') != 'directed'
                or doc.get('target_member_id') != int(member_id)):
            return None
        doc.update(routing='choosing', target_member_id=None, directed_at=None,
                   directed_message_id=None, waiting_since=utcnow(),
                   last_undirect_reason=reason)
        if int(member_id) not in doc['declined_by']:
            doc['declined_by'].append(int(member_id))
        return dict(doc)

    def open_session(self, session_id):
        self.calls.append(('open_session', session_id))
        doc = self.docs.get(session_id)
        if (doc is None or doc.get('status') != 'pending'
                or doc.get('routing') not in ('choosing', 'directed')):
            return None
        doc.update(routing='open', target_member_id=None, directed_at=None,
                   waiting_since=utcnow())
        return dict(doc)

    # --- best-effort setters
    def set_directed_message(self, session_id, message_id):
        self.calls.append(('set_directed_message', session_id, message_id))
        doc = self.docs.get(session_id)
        if doc is None:
            return False
        doc['directed_message_id'] = message_id
        return True

    def set_directed_notice(self, session_id, channel_id, message_id):
        self.calls.append(('set_directed_notice', session_id, channel_id, message_id))
        doc = self.docs.get(session_id)
        if doc is None:
            return False
        doc['notice_channel_id'] = str(channel_id)
        doc['notice_message_id'] = message_id
        return True

    def set_queue_message(self, session_id, channel_id, message_id):
        self.calls.append(('set_queue_message', session_id, channel_id, message_id))
        doc = self.docs.get(session_id)
        if doc is not None:
            doc['queue_channel_id'] = str(channel_id)
            doc['queue_message_id'] = message_id
        return True

    # --- member profile writes
    def mark_member_started(self, member_id, started=True, collection=None):
        self.calls.append(('mark_member_started', int(member_id), bool(started)))
        self.started_flags[int(member_id)] = bool(started)
        return True

    def set_member_availability(self, member_id, available, collection=None):
        self.calls.append(('set_member_availability', int(member_id), bool(available)))
        return True

    # --- reads
    def get_session(self, session_id):
        self.calls.append(('get_session', session_id))
        return self.docs.get(session_id)

    def get_pending_sessions(self):
        self.calls.append(('get_pending_sessions',))
        out = [d for d in self.docs.values() if d.get('status') == 'pending']
        return sorted(out, key=lambda d: d.get('created_at')
                      or datetime.datetime.min.replace(tzinfo=UTC))

    def get_active_sessions(self):
        self.calls.append(('get_active_sessions',))
        return [d for d in self.docs.values() if d.get('status') == 'active']

    def get_sessions_by_activity(self, cutoff):
        self.calls.append(('get_sessions_by_activity', cutoff))
        return []

    def log_message(self, **kw):
        self.calls.append(('log_message', kw.get('session_id')))
        return True

    def update_session_activity(self, session_id):
        self.calls.append(('update_session_activity', session_id))
        return True

    # --- assertion helpers
    def called(self, method):
        return [c for c in self.calls if c[0] == method]

    def count(self, method):
        return len(self.called(method))


class FakeBot:
    def __init__(self):
        self.sent = []          # (chat_id, text, reply_markup)
        self.edited = []        # (chat_id, message_id, text)
        self.markup_edits = []  # (chat_id, message_id)
        self.fail_for = {}      # chat_id (str) -> exception message
        self._mid = 0

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if str(chat_id) in self.fail_for:
            raise RuntimeError(self.fail_for[str(chat_id)])
        self._mid += 1
        self.sent.append((str(chat_id), text, reply_markup))
        return SimpleNamespace(message_id=self._mid)

    async def edit_message_text(self, chat_id=None, message_id=None, text=None, **kw):
        self.edited.append((str(chat_id), message_id, text))
        return None

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, **kw):
        self.markup_edits.append((str(chat_id), message_id))
        return None

    async def delete_message(self, chat_id=None, message_id=None, **kw):
        return None

    def texts_to(self, user_id):
        return [t for c, t, _m in self.sent if c == str(user_id)]


class Rec:
    def __init__(self):
        self.replies = []   # (text, reply_markup) sent back to the acting user
        self.answers = []   # (text, show_alert)


def text_update(user_id, text, rec, username=None, chat_type="private"):
    async def reply_text(t, reply_markup=None, **kw):
        rec.replies.append((t, reply_markup))
    user = SimpleNamespace(id=user_id, username=username, first_name="User",
                           last_name=None)
    msg = SimpleNamespace(text=text, reply_text=reply_text, photo=[], sticker=None)
    return SimpleNamespace(effective_user=user, message=msg, callback_query=None,
                           effective_chat=SimpleNamespace(id=user_id, type=chat_type))


def cb_update(user_id, data, rec, username="mem", first="Mem"):
    async def answer(t=None, show_alert=False):
        rec.answers.append((t, show_alert))

    async def edit_message_text(t, reply_markup=None, **kw):
        rec.replies.append((t, reply_markup))

    async def edit_message_reply_markup(reply_markup=None, **kw):
        return None

    fu = SimpleNamespace(id=user_id, username=username, first_name=first, last_name=None)
    q = SimpleNamespace(from_user=fu, data=data, answer=answer,
                        edit_message_text=edit_message_text,
                        edit_message_reply_markup=edit_message_reply_markup)
    return SimpleNamespace(effective_user=fu, message=None, callback_query=q,
                           effective_chat=SimpleNamespace(id=user_id, type="private"))


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


def install(stub):
    handlers_mod.db_mgr = stub
    queue_mod.db_mgr = stub
    session_mod.db_mgr = stub
    restore_mod.db_mgr = stub
    expiry_mod.db_mgr = stub


def supporter_id(n):
    """Stable, readable supporter ids: 3000, 3001, ..."""
    return 3000 + n


def supporter_doc(n, **overrides):
    doc = {
        'telegram_id': supporter_id(n),
        'display_name': "Sup %02d" % n,
        'blurb': "",
        'available': True,
        'has_started_bot': True,
        'active': True,
    }
    doc.update(overrides)
    return doc


def setup(supporters=1, service="pss", db_available=True, **profile_overrides):
    """PSS-only (or HF-only) world with `supporters` fully pickable profiles."""
    reset_state()
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]

    hf.channel_id, hf.enabled = HF_CHANNEL, (service == "hf")
    pss.channel_id, pss.enabled = PSS_CHANNEL, (service == "pss")

    docs = [supporter_doc(n, **profile_overrides) for n in range(supporters)]
    target = config.SERVICES[service]
    target.roster.replace_records(docs)
    other = hf if service == "pss" else pss
    other.roster.replace_records([])

    assert [s.key for s in config.enabled_services()] == [service], (
        "exactly one runnable service keeps the landing-page chooser out of the way")

    stub = StubDB(db_available=db_available)
    install(stub)
    bot = FakeBot()
    sm = SessionManager(bot)
    qm = QueueManager(bot)
    handlers = BotHandlers(sm, qm)
    return bot, sm, qm, handlers, stub


async def ask_for_help(handlers, rec, ctx, text="I could use someone to talk to",
                       user_id=REQUESTER):
    """Drive /chat then the description, i.e. everything up to the fork."""
    await handlers.chat_command(text_update(user_id, "/chat", rec), ctx)
    await handlers.handle_message(text_update(user_id, text, rec), ctx)


def ctx_for(bot):
    return SimpleNamespace(bot=bot)


def requester_texts(bot, rec, user_id=REQUESTER):
    """Everything the REQUESTER could have read: DMs, replies and message edits."""
    out = [t for c, t, _m in bot.sent if c == str(user_id)]
    out += [t for t, _m in rec.replies]
    out += [t for c, _mid, t in bot.edited if c == str(user_id) and t]
    return out


# --------------------------------------------------------------------------- cases
async def case_a_fork_appears_and_nothing_reaches_the_channel():
    bot, sm, qm, handlers, stub = setup(supporters=1)
    rec = Rec()
    await ask_for_help(handlers, rec, ctx_for(bot))

    assert config.user_states[REQUESTER] == UserState.CHOOSING_SUPPORTER, \
        config.user_states.get(REQUESTER)
    queue_id = config.user_to_queue_map[REQUESTER]
    entry = config.queue_entries[queue_id]
    assert entry['routing'] == 'choosing', entry['routing']
    assert queue_id not in config.queue_order, \
        "a request nobody outside the chat can see has no queue position"
    assert stub.docs[queue_id]['routing'] == 'choosing', \
        "the Mongo row is created BEFORE the question, so a restart is recoverable"
    assert not [s for s in bot.sent if s[0] == PSS_CHANNEL], \
        "the comfort question must not put anything in the channel: %r" % (bot.sent,)

    text, markup = rec.replies[-1]
    assert text == config.MESSAGES["comfort_question"], text
    datas = sorted(b.callback_data for row in markup.inline_keyboard for b in row)
    assert datas == ["pk_l:0", "pk_o"], datas
    print("OK  a. the fork appears, the row is 'choosing', and the channel sees nothing")


async def case_b_no_pickable_supporters_means_no_fork():
    """THE LOAD-BEARING PROPERTY. A roster of bare ints has no display names, so
    available_supporters() is empty, so the fork never fires -- which is exactly why
    test_pss_flow.py and test_restore.py needed nothing but a reset_state() clear."""
    bot, sm, qm, handlers, stub = setup(supporters=0)
    config.SERVICES["pss"].roster.replace([supporter_id(0)])
    assert config.available_supporters("pss") == []

    rec = Rec()
    await ask_for_help(handlers, rec, ctx_for(bot))

    assert config.user_states[REQUESTER] == UserState.IN_QUEUE
    queue_id = config.user_to_queue_map[REQUESTER]
    assert config.queue_entries[queue_id]['routing'] == 'open'
    assert queue_id in config.queue_order
    posts = [s for s in bot.sent if s[0] == PSS_CHANNEL]
    assert len(posts) == 1 and posts[0][2] is not None, "today's channel post, unchanged"
    assert rec.replies[-1][0] == config.MESSAGES["queue_added"]
    print("OK  b. zero pickable supporters => no fork, byte-identical to today")


async def case_c_hf_never_forks():
    bot, sm, qm, handlers, stub = setup(supporters=2, service="hf")
    assert config.SERVICES["hf"].directed_enabled is False, \
        "directed support is a PSS feature; HF must never opt in"
    # Even with fully pickable HF profiles present:
    assert len(config.available_supporters("hf")) == 2

    rec = Rec()
    await ask_for_help(handlers, rec, ctx_for(bot))

    assert config.user_states[REQUESTER] == UserState.IN_QUEUE
    assert config.queue_entries[config.user_to_queue_map[REQUESTER]]['routing'] == 'open'
    assert [s for s in bot.sent if s[0] == HF_CHANNEL], "HF still posts to its channel"
    print("OK  c. HF never forks even with pickable profiles on its roster")


async def case_d_picker_renders_and_records_what_was_shown():
    bot, sm, qm, handlers, stub = setup(supporters=3)
    config.SERVICES["pss"].roster.replace_records([
        supporter_doc(0, display_name="bea", blurb="Second-year."),
        supporter_doc(1, display_name="Alex"),
        supporter_doc(2, display_name="Alex"),   # duplicate name: id breaks the tie
    ])
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]

    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    text, markup = rec.replies[-1]
    assert config.MESSAGES["picker_header"] in text, text
    assert "1. Alex" in text and "2. Alex" in text and "3. bea" in text, text
    assert "Second-year." in text, "a blurb renders under its name"
    assert config.MESSAGES["picker_page"].split("{")[0] not in text, \
        "a single page must not print a page counter"

    view = config.picker_views[REQUESTER]
    assert view['queue_id'] == queue_id
    assert view['page'] == 0
    # casefold ordering, id tiebreak: Alex(3001), Alex(3002), bea(3000)
    assert view['ids'] == [supporter_id(1), supporter_id(2), supporter_id(0)], view['ids']
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels[:3] == ["1. Alex", "2. Alex", "3. bea"], labels
    print("OK  d. the picker renders in a stable order and records exactly what it showed")


async def case_e_pagination_maps_a_typed_number_to_that_page():
    bot, sm, qm, handlers, stub = setup(supporters=20)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)

    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:1", rec), ctx)
    text, _markup = rec.replies[-1]
    assert "Page 2 of 3" in text, text
    view = config.picker_views[REQUESTER]
    assert view['page'] == 1
    assert view['ids'] == [supporter_id(n) for n in range(8, 16)], view['ids']

    await handlers.handle_message(text_update(REQUESTER, "3", rec), ctx)
    directed = stub.called('direct_session')
    assert len(directed) == 1, stub.calls
    assert directed[0][2] == supporter_id(10), (
        "typed numbers are 1-based and PAGE-LOCAL: '3' on page 2 is the 11th "
        "supporter overall, got %r" % (directed[0][2],))
    print("OK  e. a typed number is read against the page the requester is looking at")


async def case_f_a_missing_view_never_guesses():
    """The post-restart path. picker_views is deliberately not persisted, so a number
    typed after a restart must re-render rather than select somebody unseen."""
    bot, sm, qm, handlers, stub = setup(supporters=4)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)

    config.picker_views.clear()          # <- the restart
    before = stub.count('direct_session')
    await handlers.handle_message(text_update(REQUESTER, "2", rec), ctx)

    assert stub.count('direct_session') == before, \
        "a number typed with no recorded view must select NOBODY"
    assert not bot.texts_to(supporter_id(1)), "and must DM nobody"
    # No callback query to edit, so the re-render arrives as a fresh DM.
    text = bot.texts_to(REQUESTER)[-1]
    assert config.MESSAGES["picker_lost_view"] in text, text
    assert config.MESSAGES["picker_header"] in text, "the picker is re-rendered"
    assert config.picker_views[REQUESTER]['page'] == 0
    print("OK  f. no recorded view => nothing is selected, the picker re-renders")


async def case_g_garbage_numbers_re_render_the_same_page():
    bot, sm, qm, handlers, stub = setup(supporters=20)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:1", rec), ctx)

    for bad in ("banana", "0", "99", "-1", "2.5", "", "   "):
        before = stub.count('direct_session')
        await handlers.handle_message(text_update(REQUESTER, bad, rec), ctx)
        assert stub.count('direct_session') == before, "%r selected somebody" % (bad,)
        text = bot.texts_to(REQUESTER)[-1]
        assert config.MESSAGES["picker_not_a_number"] in text, (bad, text)
        assert "Page 2 of 3" in text, ("must re-render the SAME page", bad, text)
        assert config.picker_views[REQUESTER]['page'] == 1, bad
    print("OK  g. garbage, 0 and out-of-range all re-render the same page and select nobody")


async def case_h_a_tap_sends_one_dm_and_one_contentless_note():
    bot, sm, qm, handlers, stub = setup(supporters=2)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx, text="my flatmate situation is awful")
    queue_id = config.user_to_queue_map[REQUESTER]
    anon = config.queue_entries[queue_id]['anonymous_id']

    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % supporter_id(0), rec), ctx)

    assert stub.count('direct_session') == 1, stub.calls
    dms = [s for s in bot.sent if s[0] == str(supporter_id(0))]
    assert len(dms) == 1, dms
    assert "my flatmate situation is awful" in dms[0][1]
    assert anon not in dms[0][1], \
        "the anonymous id is withheld until the conversation actually starts"
    assert str(REQUESTER) not in dms[0][1]
    accept_datas = sorted(b.callback_data for row in dms[0][2].inline_keyboard
                          for b in row)
    assert accept_datas == ["dr_a:%s" % queue_id, "dr_d:%s" % queue_id], accept_datas

    notes = [s for s in bot.sent if s[0] == PSS_CHANNEL]
    assert len(notes) == 1, notes
    note_text, note_markup = notes[0][1], notes[0][2]
    assert "Sup 00" in note_text
    assert "my flatmate situation is awful" not in note_text, \
        "the channel note carries NO description"
    assert anon not in note_text, (
        "the channel note carries NO anonymous id -- with it the channel could "
        "correlate this note against a later open-queue post of the same request "
        "and read off that a named supporter said no")
    assert note_markup is None, "the channel note carries NO claim button"

    entry = config.queue_entries[queue_id]
    assert entry['routing'] == 'directed'
    assert entry['target_member_id'] == supporter_id(0)
    assert config.directed_by_member[supporter_id(0)] == queue_id
    assert config.user_states[REQUESTER] == UserState.IN_QUEUE, (
        "the choosing is over and they ARE waiting; this must match what "
        "restore._restore_pending sets for a 'directed' row, or the state silently "
        "changes across a restart")

    # ... so anything they type now is an ordinary message, NOT a picker number read
    # against a view that no longer exists.
    before = len(rec.replies)
    await handlers.handle_message(text_update(REQUESTER, "2", rec), ctx)
    assert rec.replies[-1][0] == config.MESSAGES["unknown_command"], rec.replies[-1]
    assert len(rec.replies) == before + 1
    assert stub.count('direct_session') == 1, (
        "and it certainly must not select a second supporter", stub.calls)
    print("OK  h. one tap => one DM, one note with no description, no id and no button")


async def case_i_a_double_tap_sends_exactly_one_dm():
    """THE EXACTLY-ONCE GATE, both halves of it.

    TWO guards stand in front of the DM, and they cover different failures:

      * the in-memory routing check catches an impatient requester tapping the same
        name twice in one process. It sits BEFORE the first await, so a concurrent
        pair cannot interleave past it either;
      * the routing:'choosing' clause in direct_session's own filter catches the case
        memory cannot see -- two CONTAINERS briefly running at once during a deploy
        rollback, each with its own queue_entries, both convinced the request is
        still choosable. That is the guard mutation M12 removes.
    """
    bot, sm, qm, handlers, stub = setup(supporters=2)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)

    tap = "pk_s:%d" % supporter_id(0)
    await asyncio.gather(
        handlers.handle_callback_query(cb_update(REQUESTER, tap, rec), ctx),
        handlers.handle_callback_query(cb_update(REQUESTER, tap, rec), ctx),
    )
    await handlers.handle_callback_query(cb_update(REQUESTER, tap, rec), ctx)

    assert stub.count('direct_session') == 1, (
        "the in-memory guard sits before the first await, so the second and third "
        "taps must not even reach Mongo", stub.calls)
    assert stub.docs[queue_id]['routing'] == 'directed'
    dms = [s for s in bot.sent if s[0] == str(supporter_id(0))]
    assert len(dms) == 1, ("exactly ONE supporter DM, got %d" % len(dms), bot.sent)
    notes = [s for s in bot.sent if s[0] == PSS_CHANNEL]
    assert len(notes) == 1, ("and exactly one channel note", notes)

    # Now the case memory CANNOT see: another instance already directed this request,
    # so Mongo says 'directed' while our in-memory copy still says 'choosing'.
    entry = config.queue_entries[queue_id]
    entry['routing'] = 'choosing'
    entry['target_member_id'] = None
    entry['directed_at'] = None
    config.directed_by_member.pop(supporter_id(0), None)
    assert stub.docs[queue_id]['routing'] == 'directed', "Mongo still knows"

    outcome = await qm.send_directed_request(queue_id, supporter_id(0))

    assert outcome == 'gone', outcome
    assert stub.count('direct_session') == 2, (
        "this one DOES reach the gate", stub.calls)
    dms = [s for s in bot.sent if s[0] == str(supporter_id(0))]
    assert len(dms) == 1, (
        "and the gate refuses it, so there is still exactly ONE DM", bot.sent)
    assert entry['routing'] == 'choosing', "a lost race changes nothing locally"
    print("OK  i. neither a double tap nor a second instance can send a second DM")


async def case_j_a_failed_dm_rolls_everything_back():
    bot, sm, qm, handlers, stub = setup(supporters=2)
    bot.fail_for[str(supporter_id(0))] = "Forbidden: bot can't initiate conversation with a user"
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % supporter_id(0), rec), ctx)

    entry = config.queue_entries[queue_id]
    assert entry['routing'] == 'choosing', (
        "a row left 'directed' at a supporter who was never messaged burns 24 hours "
        "of silence on someone in distress")
    assert entry['target_member_id'] is None
    assert supporter_id(0) not in config.directed_by_member
    assert stub.docs[queue_id]['routing'] == 'choosing'
    assert ('mark_member_started', supporter_id(0), False) in stub.calls, stub.calls
    assert config.SERVICES["pss"].roster.profile(supporter_id(0)).has_started_bot is False
    assert not [s for s in bot.sent if s[0] == PSS_CHANNEL], \
        "a DM that never landed must not produce a channel note"

    text, _m = rec.replies[-1]
    assert config.MESSAGES["picker_unreachable"] in text, text
    assert "Sup 00" not in text, "and the unreachable supporter is off the list"
    assert "Sup 01" in text
    print("OK  j. a failed DM rolls back the row, clears has_started_bot and posts no note")


async def case_k_accept_by_a_non_target_is_refused():
    """M11. claim_queue authorizes against the WHOLE roster; that is exactly wrong
    for a request one person was chosen for."""
    bot, sm, qm, handlers, stub = setup(supporters=3)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % supporter_id(0), rec), ctx)

    before_claims = stub.count('claim_session')
    outcome, requester_id = await qm.accept_directed(queue_id, supporter_id(1), "Nope")
    assert (outcome, requester_id) == ('not_yours', None), (outcome, requester_id)
    assert stub.count('claim_session') == before_claims, \
        "claim_session must never be reached by a non-target"

    # ... and through the real handler too.
    await handlers.handle_callback_query(
        cb_update(supporter_id(1), "dr_a:%s" % queue_id, rec), ctx)
    assert stub.count('claim_session') == before_claims, stub.calls
    assert config.queue_entries[queue_id]['routing'] == 'directed'
    assert sm.get_session_by_user(REQUESTER) is None
    print("OK  k. a non-targeted member cannot accept, and never reaches claim_session")


async def case_l_accept_by_the_target_starts_the_conversation():
    bot, sm, qm, handlers, stub = setup(supporters=2)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]
    anon = config.queue_entries[queue_id]['anonymous_id']
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % supporter_id(0), rec), ctx)
    dm_message_id = config.queue_entries[queue_id]['directed_message_id']

    await handlers.handle_callback_query(
        cb_update(supporter_id(0), "dr_a:%s" % queue_id, rec), ctx)

    assert sm.get_session_by_user(REQUESTER) == queue_id
    assert sm.get_session_by_user(supporter_id(0)) == queue_id
    assert config.user_states[REQUESTER] == UserState.IN_CONVERSATION
    assert config.user_states[supporter_id(0)] == UserState.IN_CONVERSATION
    assert sm.get_session_info(queue_id)['anonymous_user_id'] == anon, \
        "the requester keeps the same anonymous id across the whole request"
    assert queue_id not in config.queue_entries
    assert REQUESTER not in config.user_to_queue_map
    assert supporter_id(0) not in config.directed_by_member
    assert REQUESTER not in config.picker_views
    assert (str(supporter_id(0)), dm_message_id) in bot.markup_edits, \
        "the Accept / Not right now buttons must be stripped"
    accepted = [t for c, _mid, t in bot.edited if c == PSS_CHANNEL]
    assert accepted and "accepted" in accepted[-1], accepted
    assert config.MESSAGES["conversation_started"] in bot.texts_to(REQUESTER)
    print("OK  l. the targeted supporter accepts: session, stripped buttons, note updated")


async def case_m_a_channel_claim_button_cannot_take_a_directed_request():
    bot, sm, qm, handlers, stub = setup(supporters=3)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % supporter_id(0), rec), ctx)

    before = stub.count('claim_session')
    await handlers.handle_callback_query(
        cb_update(supporter_id(2), "claim_%s" % queue_id, rec), ctx)
    assert stub.count('claim_session') == before, stub.calls
    assert any("specific supporter" in (a[0] or "") for a in rec.answers), rec.answers
    assert config.queue_entries[queue_id]['routing'] == 'directed'
    print("OK  m. a stale channel Claim button is refused on a directed request")


async def case_n_a_decline_hands_the_choice_back_exactly_once():
    bot, sm, qm, handlers, stub = setup(supporters=3)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % supporter_id(0), rec), ctx)
    before_to_requester = len(bot.texts_to(REQUESTER))

    await handlers.handle_callback_query(
        cb_update(supporter_id(0), "dr_d:%s" % queue_id, rec), ctx)

    assert stub.count('undirect_session') == 1, stub.calls
    new_texts = bot.texts_to(REQUESTER)[before_to_requester:]
    assert len(new_texts) == 1, ("the requester hears about this EXACTLY once", new_texts)
    assert "isn't free right now" in new_texts[0], new_texts[0]
    assert config.MESSAGES["next_step_question"] in new_texts[0]

    entry = config.queue_entries[queue_id]
    assert entry['routing'] == 'choosing'
    assert entry['declined_by'] == [supporter_id(0)]
    assert stub.docs[queue_id]['declined_by'] == [supporter_id(0)]
    assert supporter_id(0) not in config.directed_by_member
    assert config.user_states[REQUESTER] == UserState.CHOOSING_SUPPORTER
    assert config.MESSAGES["decline_ack"] in bot.texts_to(supporter_id(0))
    print("OK  n. a decline hands the choice back, once, and records who declined")


async def case_o_nothing_the_requester_reads_mentions_a_decline():
    """The forbidden-words scan. A decline and a 24-hour silence must be
    indistinguishable to the person who asked for help."""
    bot, sm, qm, handlers, stub = setup(supporters=3)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % supporter_id(0), rec), ctx)
    await handlers.handle_callback_query(
        cb_update(supporter_id(0), "dr_d:%s" % queue_id, rec), ctx)
    # ... and a lapse on the next one, so both outcomes are in the scan.
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % supporter_id(1), rec), ctx)
    config.queue_entries[queue_id]['directed_at'] = (
        utcnow() - datetime.timedelta(minutes=1441))
    await qm.sweep_directed_requests()

    scanned = requester_texts(bot, rec)
    assert scanned, "nothing was captured; the scan would be vacuous"
    for text in scanned:
        lowered = text.lower()
        for word in FORBIDDEN_TO_REQUESTER:
            assert word not in lowered, (
                "the requester must never be told a supporter said no: %r in %r"
                % (word, text))
        assert str(supporter_id(0)) not in text, \
            "and must never see a supporter's Telegram id: %r" % (text,)
        assert "@mem" not in text, "nor their @username: %r" % (text,)
    print("OK  o. no requester-bound string names a decline, an id or a username")


async def case_p_a_repick_never_offers_the_decliner_again():
    bot, sm, qm, handlers, stub = setup(supporters=3)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % supporter_id(0), rec), ctx)
    await handlers.handle_callback_query(
        cb_update(supporter_id(0), "dr_d:%s" % queue_id, rec), ctx)

    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    text, _m = rec.replies[-1]
    assert "Sup 00" not in text, text
    assert "Sup 01" in text and "Sup 02" in text, text
    assert config.picker_views[REQUESTER]['ids'] == [supporter_id(1), supporter_id(2)]

    # And the send path refuses them even if a stale button is tapped.
    assert await qm.send_directed_request(queue_id, supporter_id(0)) == 'busy'
    print("OK  p. a re-pick excludes the decliner, and a stale button for them is refused")


async def case_q_a_lapsed_request_comes_back_after_the_window():
    bot, sm, qm, handlers, stub = setup(supporters=3)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % supporter_id(0), rec), ctx)

    # Still inside the window: nothing happens.
    config.queue_entries[queue_id]['directed_at'] = (
        utcnow() - datetime.timedelta(minutes=1439))
    assert await qm.sweep_directed_requests() == []
    assert config.queue_entries[queue_id]['routing'] == 'directed'

    before = len(bot.texts_to(REQUESTER))
    config.queue_entries[queue_id]['directed_at'] = (
        utcnow() - datetime.timedelta(minutes=1441))
    acted = await qm.sweep_directed_requests()
    assert len(acted) == 1 and acted[0]['outcome'] == 'lapsed', acted

    new_texts = bot.texts_to(REQUESTER)[before:]
    assert len(new_texts) == 1 and "isn't free right now" in new_texts[0], new_texts
    assert config.MESSAGES["directed_lapsed_member"] in bot.texts_to(supporter_id(0)), \
        "otherwise their DM sits looking live forever"
    assert config.queue_entries[queue_id]['routing'] == 'choosing'
    assert config.user_states[REQUESTER] == UserState.CHOOSING_SUPPORTER
    print("OK  q. a request nobody answered inside the window comes back to the requester")


async def case_r_a_second_sweep_sends_nothing():
    bot, sm, qm, handlers, stub = setup(supporters=3)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % supporter_id(0), rec), ctx)
    config.queue_entries[queue_id]['directed_at'] = (
        utcnow() - datetime.timedelta(minutes=1441))
    await qm.sweep_directed_requests()

    sent_after_first = list(bot.sent)
    assert await qm.sweep_directed_requests() == []
    assert bot.sent == sent_after_first, \
        "a second sweep pass must send absolutely nothing: %r" % (
            bot.sent[len(sent_after_first):],)
    print("OK  r. sweeping twice sends nothing the second time")


async def case_s_a_bot_that_was_down_for_days_messages_nobody():
    """M13. THE most dangerous path in this feature. Without the stale horizon the
    first boot after a multi-day outage DMs every person who was mid-request."""
    bot, sm, qm, handlers, stub = setup(supporters=3)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % supporter_id(0), rec), ctx)

    # Three days pass with the container down.
    config.queue_entries[queue_id]['directed_at'] = (
        utcnow() - datetime.timedelta(minutes=3 * 24 * 60))
    bot.sent.clear()

    acted = await qm.sweep_directed_requests()

    assert len(acted) == 1 and acted[0]['outcome'] == 'stale', acted
    assert bot.sent == [], (
        "NOBODY may be messaged this long after the fact -- not the requester, not "
        "the supporter, not the channel: %r" % (bot.sent,))
    assert stub.ended.get(queue_id) == 'stale_startup_sweep', stub.ended
    assert queue_id not in config.queue_entries
    assert REQUESTER not in config.user_to_queue_map
    assert config.user_states[REQUESTER] == UserState.IDLE
    assert supporter_id(0) not in config.directed_by_member
    assert not stub.called('undirect_session'), \
        "a stale request is CLOSED, not handed back"
    print("OK  s. a three-day-old directed request is closed silently, messaging nobody")


async def case_t_ask_anyone_moves_a_choosing_request_to_the_channel():
    bot, sm, qm, handlers, stub = setup(supporters=2)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]

    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_o", rec), ctx)

    posts = [s for s in bot.sent if s[0] == PSS_CHANNEL and s[2] is not None]
    assert len(posts) == 1, posts
    assert posts[0][2].inline_keyboard[0][0].callback_data == "claim_%s" % queue_id
    assert config.queue_entries[queue_id]['routing'] == 'open'
    assert stub.docs[queue_id]['routing'] == 'open'
    assert queue_id in config.queue_order
    assert config.user_states[REQUESTER] == UserState.IN_QUEUE
    assert REQUESTER not in config.picker_views
    assert config.MESSAGES["queue_added"] in bot.texts_to(REQUESTER)
    print("OK  t. 'anyone who's free' posts to the channel and rejoins the open queue")


async def case_u_a_failed_channel_post_leaves_the_request_choosable():
    bot, sm, qm, handlers, stub = setup(supporters=2)
    bot.fail_for[PSS_CHANNEL] = "Bad Request: chat not found"
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]

    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_o", rec), ctx)

    assert config.queue_entries[queue_id]['routing'] == 'choosing', (
        "flipping Mongo first would leave a live 'open' row with no channel post -- "
        "invisible to every member until it expires")
    assert not stub.called('open_session'), stub.calls
    assert queue_id not in config.queue_order
    assert config.MESSAGES["channel_error"] in bot.texts_to(REQUESTER)
    print("OK  u. a failed channel post reverts to 'choosing' and never flips Mongo")


async def case_v_ask_anyone_from_directed_tidies_up_before_it_posts():
    bot, sm, qm, handlers, stub = setup(supporters=2)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % supporter_id(0), rec), ctx)
    dm_message_id = config.queue_entries[queue_id]['directed_message_id']
    sent_before = len(bot.sent)

    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_o", rec), ctx)

    assert stub.count('undirect_session') == 1, stub.calls
    assert (str(supporter_id(0)), dm_message_id) in bot.markup_edits
    closed = [t for c, _mid, t in bot.edited if c == PSS_CHANNEL]
    assert closed and "no longer waiting" in closed[-1], closed
    assert "declin" not in " ".join(closed).lower(), \
        "the channel is the supporter's own peers; a decline is never named there"
    # Ordering: the tidy-up happened before the new channel post went out.
    posts = [i for i, s in enumerate(bot.sent) if s[0] == PSS_CHANNEL and s[2] is not None]
    assert posts and posts[-1] >= sent_before
    assert config.queue_entries[queue_id]['routing'] == 'open'
    assert supporter_id(0) not in config.directed_by_member
    print("OK  v. rerouting from 'directed' strips the DM and closes the note first")


async def case_w_cancelling_a_directed_request_tidies_the_supporter_up():
    bot, sm, qm, handlers, stub = setup(supporters=2)
    rec = Rec()
    ctx = ctx_for(bot)
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % supporter_id(0), rec), ctx)
    dm_message_id = config.queue_entries[queue_id]['directed_message_id']

    await handlers.cancel_command(text_update(REQUESTER, "/cancel", rec), ctx)

    assert queue_id not in config.queue_entries
    assert REQUESTER not in config.user_to_queue_map
    assert supporter_id(0) not in config.directed_by_member
    assert REQUESTER not in config.picker_views
    assert stub.ended.get(queue_id) == 'user_cancelled', stub.ended
    assert (str(supporter_id(0)), dm_message_id) in bot.markup_edits
    closed = [t for c, _mid, t in bot.edited if c == PSS_CHANNEL]
    assert closed and "no longer waiting" in closed[-1], closed
    assert rec.replies[-1][0] == config.MESSAGES["queue_cancelled"]
    assert config.user_states[REQUESTER] == UserState.IDLE
    print("OK  w. /cancel on a directed request closes the row and tidies the supporter")


async def case_x_expiry_skips_directed_and_uses_the_right_words_for_choosing():
    bot, sm, qm, handlers, stub = setup(supporters=3)
    rec = Rec()
    ctx = ctx_for(bot)

    # A 'directed' request, far past the QUEUE window but inside its own.
    await ask_for_help(handlers, rec, ctx)
    directed_id = config.user_to_queue_map[REQUESTER]
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % supporter_id(0), rec), ctx)
    config.queue_entries[directed_id]['waiting_since'] = (
        utcnow() - datetime.timedelta(minutes=5000))

    # A 'choosing' request from somebody else, past the queue window.
    other = REQUESTER + 1
    await ask_for_help(handlers, rec, ctx, user_id=other)
    choosing_id = config.user_to_queue_map[other]
    assert config.queue_entries[choosing_id]['routing'] == 'choosing'
    config.queue_entries[choosing_id]['waiting_since'] = (
        utcnow() - datetime.timedelta(minutes=5000))

    expired = qm.cleanup_expired_queues()
    ids = [e['queue_id'] for e in expired]
    assert directed_id not in ids, \
        "a directed request is owned entirely by sweep_directed_requests()"
    assert ids == [choosing_id], ids
    assert expired[0]['routing'] == 'choosing'
    assert expired[0]['notify'] is True, "CHOOSING_SUPPORTER is owed the news too"

    # Restore it and go through the real sweep to check the WORDS.
    config.queue_entries[choosing_id] = {**expired[0]}
    config.user_to_queue_map[other] = choosing_id
    config.user_states[other] = UserState.CHOOSING_SUPPORTER
    before = len(bot.texts_to(other))
    await qm.sweep_expired_queues()
    new_texts = bot.texts_to(other)[before:]
    assert len(new_texts) == 1, new_texts
    assert new_texts[0] == config.MESSAGES["choosing_expired"], new_texts[0]
    assert "was available in time" not in new_texts[0], (
        "queue_expired is false for a request nobody was ever asked about")
    print("OK  x. expiry skips directed requests and gives 'choosing' its own wording")


async def case_y_a_handed_back_request_gets_a_fresh_window():
    """M18, and the double-message guard.

    An old request that was re-picked moments ago must survive BOTH the boot horizon
    and the expiry sweep. Measuring either against created_at instead of
    waiting_since silently closes it -- or tells the requester "choose again" and
    "your request expired" one sweep apart.
    """
    bot, sm, qm, handlers, stub = setup(supporters=3)

    # A PSS request created 1700 minutes ago (past the 1440 + 120 boot horizon) whose
    # decision was handed back 5 minutes ago.
    sid = "old-but-repicked"
    now = utcnow()
    stub.docs[sid] = {
        'session_id': sid, 'user_id': REQUESTER, 'service': 'pss', 'status': 'pending',
        'description': 'still waiting', 'anonymous_user_id': 'RHesident #4242',
        'created_at': now - datetime.timedelta(minutes=1700),
        'waiting_since': now - datetime.timedelta(minutes=5),
        'last_activity_at': now - datetime.timedelta(minutes=5),
        'claimed_at': None, 'heartfelt_member_id': None,
        'queue_channel_id': PSS_CHANNEL, 'queue_message_id': None,
        'routing': 'choosing', 'target_member_id': None, 'directed_at': None,
        'directed_message_id': None, 'notice_channel_id': None,
        'notice_message_id': None, 'declined_by': [supporter_id(0)],
    }
    stats = restore_state(qm, sm)
    assert stats['pending_restored'] == 1, (
        "an old request that was re-picked five minutes ago must NOT be stale-closed "
        "at boot", stats, stub.ended)
    assert stats['pending_choosing'] == 1, stats
    assert stub.ended.get(sid) is None, stub.ended
    assert config.user_states[REQUESTER] == UserState.CHOOSING_SUPPORTER
    assert sid not in config.queue_order

    # And the live sweep leaves it alone as well.
    assert qm.cleanup_expired_queues() == []
    assert sid in config.queue_entries

    # The same guard on the live path: a request past its window that is handed back
    # gets a fresh budget rather than expiring one sweep later.
    config.queue_entries[sid]['waiting_since'] = now - datetime.timedelta(minutes=1500)
    config.queue_entries[sid]['declined_by'] = []
    assert await qm.send_directed_request(sid, supporter_id(0)) == 'ok'
    assert await qm.undirect(sid, supporter_id(0), reason='declined') is True
    assert qm.cleanup_expired_queues() == [], (
        "undirect resets waiting_since, so the very next sweep must NOT also expire it")
    print("OK  y. a handed-back request gets a fresh window at boot and on the live path")


async def case_z_availability_hides_the_busy_and_the_already_asked():
    bot, sm, qm, handlers, stub = setup(supporters=3)
    rec = Rec()
    ctx = ctx_for(bot)
    assert len(config.available_supporters("pss")) == 3

    # One of them is mid-conversation.
    config.user_to_session_map[supporter_id(2)] = "some-session"
    # ... and one is holding a directed request.
    await ask_for_help(handlers, rec, ctx)
    queue_id = config.user_to_queue_map[REQUESTER]
    await handlers.handle_callback_query(cb_update(REQUESTER, "pk_l:0", rec), ctx)
    await handlers.handle_callback_query(
        cb_update(REQUESTER, "pk_s:%d" % supporter_id(0), rec), ctx)

    remaining = [p.telegram_id for p in config.available_supporters("pss")]
    assert remaining == [supporter_id(1)], remaining
    assert config.is_supporter_available("pss", supporter_id(0)) is False, \
        "never stack two directed requests on one person"
    assert config.is_supporter_available("pss", supporter_id(2)) is False, \
        "somebody mid-conversation is not choosable"

    # A SECOND requester, still choosing, can reach neither of them: one is
    # mid-conversation, the other already holds a directed request.
    other = REQUESTER + 1
    await ask_for_help(handlers, rec, ctx, user_id=other)
    other_id = config.user_to_queue_map[other]
    assert config.queue_entries[other_id]['routing'] == 'choosing'
    assert await qm.send_directed_request(other_id, supporter_id(2)) == 'busy'
    assert await qm.send_directed_request(other_id, supporter_id(0)) == 'busy'
    assert not bot.texts_to(supporter_id(2)), "and neither of them is DMed"
    print("OK  z. availability hides anyone mid-conversation or already holding a request")


class FakeCollection:
    """A hand-rolled stand-in for one pymongo collection.

    It exists so the REAL DBManager transitions can be driven for once. Everything
    else in this file uses StubDB, which enforces the same filters -- but a stub
    that enforces the filter it was TOLD about cannot notice the day the real filter
    loses a clause. This matches filters the way Mongo does for the handful of
    operators this code uses, so a filter missing a clause really does match a
    document it should not.
    """

    def __init__(self, docs):
        self.docs = {d['session_id']: d for d in docs}
        self.filters = []

    @staticmethod
    def _matches(doc, flt):
        for key, cond in flt.items():
            value = doc.get(key)
            if isinstance(cond, dict):
                if '$in' in cond and value not in cond['$in']:
                    return False
                if '$ne' in cond and value == cond['$ne']:
                    return False
            elif value != cond:
                return False
        return True

    @staticmethod
    def _apply(doc, update):
        for key, value in (update.get('$set') or {}).items():
            doc[key] = value
        for key, value in (update.get('$inc') or {}).items():
            doc[key] = doc.get(key, 0) + value
        for key, value in (update.get('$addToSet') or {}).items():
            doc.setdefault(key, [])
            if value not in doc[key]:
                doc[key].append(value)

    def find_one_and_update(self, flt, update, return_document=None, **kw):
        self.filters.append(dict(flt))
        for doc in self.docs.values():
            if self._matches(doc, flt):
                self._apply(doc, update)
                return dict(doc)
        return None

    def update_one(self, flt, update, upsert=False, **kw):
        assert upsert is False, (
            "no writer in this feature may upsert: the only creator of a session row "
            "is create_session and the only creator of a roster row is "
            "add_authorized_member")
        self.filters.append(dict(flt))
        matched = 0
        for doc in self.docs.values():
            if self._matches(doc, flt):
                self._apply(doc, update)
                matched = 1
                break
        return SimpleNamespace(matched_count=matched, modified_count=matched,
                               acknowledged=True)


async def case_ab_the_real_mongo_filters_name_the_state_they_leave():
    """Drive the REAL DBManager, not the stub.

    Every routing transition is an exactly-once gate on a message to a real person,
    and the gate IS the filter. A filter that has lost the clause naming the state it
    is leaving still returns a document, and the caller then happily sends a second
    DM. This is the case mutation M12 exists for.
    """
    import src.database.manager as manager_mod
    real_db_mgr = manager_mod.db_mgr
    saved_conn = manager_mod.db_manager
    saved_available = real_db_mgr.db_available

    doc = {
        'session_id': 'real-1', 'user_id': REQUESTER, 'status': 'pending',
        'routing': 'choosing', 'target_member_id': None, 'directed_at': None,
        'declined_by': [], 'heartfelt_member_id': None, 'claimed_at': 'STALE',
    }
    collection = FakeCollection([doc])
    manager_mod.db_manager = SimpleNamespace(db=SimpleNamespace(sessions=collection))
    real_db_mgr.db_available = True
    try:
        # choosing -> directed wins exactly once.
        assert real_db_mgr.direct_session('real-1', 4242) is not None
        assert doc['routing'] == 'directed' and doc['target_member_id'] == 4242
        assert real_db_mgr.direct_session('real-1', 4243) is None, (
            "the routing:'choosing' clause is what makes a double-tap send exactly "
            "ONE DM; without it this returns a document and a second DM goes out")
        assert doc['target_member_id'] == 4242, "and the loser changes nothing"

        # directed -> choosing, but only for the member it was actually sent to.
        assert real_db_mgr.undirect_session('real-1', 4243, reason='declined') is None, (
            "a supporter who was never asked cannot hand back somebody else's request")
        assert real_db_mgr.undirect_session('real-1', 4242, reason='declined') is not None
        assert doc['routing'] == 'choosing'
        assert doc['declined_by'] == [4242], doc['declined_by']
        assert doc['target_member_id'] is None and doc['directed_at'] is None
        assert doc['waiting_since'] is not None, "a fresh window, or it expires at once"

        # choosing -> open, once.
        assert real_db_mgr.open_session('real-1') is not None
        assert doc['routing'] == 'open'
        assert real_db_mgr.open_session('real-1') is None, "already open"

        # active -> pending, and only by the member who holds it.
        assert real_db_mgr.release_session('real-1', 4242) is None, (
            "a pending row is not releasable; only an ACTIVE one is")
        doc.update(status='active', heartfelt_member_id=4242, claimed_at='STALE')
        assert real_db_mgr.release_session('real-1', 9999) is None, (
            "and only by the member actually in the conversation")
        assert real_db_mgr.release_session('real-1', 4242) is not None
        assert doc['status'] == 'pending', "NOT 'ended': the request is not over"
        assert doc['claimed_at'] is None, (
            "claimed_at MUST reset, or the eventual end_session measures duration "
            "from the abandoned stint")
        assert doc['heartfelt_member_id'] is None
        assert doc['release_count'] == 1
        assert 4242 in doc['released_by'] and 4242 in doc['declined_by']

        # Every filter named the state it was leaving.
        assert all('status' in f for f in collection.filters), collection.filters
    finally:
        manager_mod.db_manager = saved_conn
        real_db_mgr.db_available = saved_available
    print("OK  ab. the real Mongo filters each name the state they are leaving")


# --------------------------------------------------------------------------- runner
CASES = [
    case_a_fork_appears_and_nothing_reaches_the_channel,
    case_b_no_pickable_supporters_means_no_fork,
    case_c_hf_never_forks,
    case_d_picker_renders_and_records_what_was_shown,
    case_e_pagination_maps_a_typed_number_to_that_page,
    case_f_a_missing_view_never_guesses,
    case_g_garbage_numbers_re_render_the_same_page,
    case_h_a_tap_sends_one_dm_and_one_contentless_note,
    case_i_a_double_tap_sends_exactly_one_dm,
    case_j_a_failed_dm_rolls_everything_back,
    case_k_accept_by_a_non_target_is_refused,
    case_l_accept_by_the_target_starts_the_conversation,
    case_m_a_channel_claim_button_cannot_take_a_directed_request,
    case_n_a_decline_hands_the_choice_back_exactly_once,
    case_o_nothing_the_requester_reads_mentions_a_decline,
    case_p_a_repick_never_offers_the_decliner_again,
    case_q_a_lapsed_request_comes_back_after_the_window,
    case_r_a_second_sweep_sends_nothing,
    case_s_a_bot_that_was_down_for_days_messages_nobody,
    case_t_ask_anyone_moves_a_choosing_request_to_the_channel,
    case_u_a_failed_channel_post_leaves_the_request_choosable,
    case_v_ask_anyone_from_directed_tidies_up_before_it_posts,
    case_w_cancelling_a_directed_request_tidies_the_supporter_up,
    case_x_expiry_skips_directed_and_uses_the_right_words_for_choosing,
    case_y_a_handed_back_request_gets_a_fresh_window,
    case_z_availability_hides_the_busy_and_the_already_asked,
    case_ab_the_real_mongo_filters_name_the_state_they_leave,
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
    assert len(CASES) >= 24, (
        "expected at least 24 cases, collected %d (%s). Test discovery has "
        "regressed -- fix the discovery, do not lower this number."
        % (len(CASES), ", ".join(c.__name__ for c in CASES) or "none")
    )

    real = (handlers_mod.db_mgr, queue_mod.db_mgr, session_mod.db_mgr,
            restore_mod.db_mgr, expiry_mod.db_mgr)
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]
    saved = (hf.channel_id, hf.enabled, pss.channel_id, pss.enabled)
    try:
        asyncio.run(run())
        print("\nAll %d directed-request assertions passed!" % len(CASES))
    finally:
        (handlers_mod.db_mgr, queue_mod.db_mgr, session_mod.db_mgr,
         restore_mod.db_mgr, expiry_mod.db_mgr) = real
        hf.channel_id, hf.enabled, pss.channel_id, pss.enabled = saved
        # replace_records([]) FIRST: replace() with the same id set is a no-op that
        # keeps existing profiles, so without this the scratch profiles would leak
        # into any suite sharing this process.
        hf.roster.replace_records([])
        hf.roster.replace(config.DEFAULT_HEARTFELT_MEMBERS)
        pss.roster.replace_records([])
        pss.roster.replace([])
        reset_state()
