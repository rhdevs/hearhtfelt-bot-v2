#!/usr/bin/env python3
"""
Supporter names: the rules, the listed labels, the capture and /name.

A PSS supporter is shown to a student BY NAME, next to a number the student types
to choose them. That name is either the supporter's own /name override or, by
default, the Telegram first name the bot captures from their private messages. So
everything a supporter can type there ends up in front of somebody who may be in
distress, and two things matter more than the rest:

  * the rules (cases a-e, l) -- a name cannot carry a link, a phone number, a
    username, markup, invisible characters or the bot's own button words, and two
    supporters can never look identical in the list;
  * the precedence (case f) -- an override that fails the rules shows NOTHING. It
    never falls back to the real first name, because somebody who chose an
    override may have chosen it precisely so their real name is not shown.

The harness below (NamesStubDB, FakeBot, text_update, cb_update, catching) is
built in full now because the capture and /name cases drive the REAL handlers
with it.

Run directly: `python tests/test_supporter_names.py`
"""

import asyncio
import contextlib
import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymongo import ReturnDocument

import config
from config import MESSAGES, ServiceType
from src.supporter_names import (
    NAME_MAX_LENGTH,
    REJECTION_KEYS,
    clean_name,
    disambiguate,
    name_key,
    name_problem,
)
from src.timeutil import utcnow

import main
import src.bot.handlers as handlers_mod
import src.database.manager as manager_mod
import src.database.utils as utils_mod

HF_CHANNEL = "-100HF"
PSS_CHANNEL = "-100PSS"
HF_COLL = "heartfelt_members"
PSS_COLL = "peer_supporters"

# Deliberately distinctive, so a leak scan cannot pass for the wrong reason.
SECRET_ID = 987654321


# --------------------------------------------------------------------------- stubs
class NamesStubDB:
    """Recording stand-in for db_mgr covering the member-document writers.

    `members` is {collection: {telegram_id: doc}}. set_member_display_name honours
    the same `active: {'$ne': False}` filter the real one sends, and returns the
    same (outcome, before) tuples; `force_display_outcome` makes it return
    'offline' / 'error' / 'missing' on demand. get_authorized_member_records
    honours include_inactive the way Mongo does.
    """

    def __init__(self, db_available=True):
        self.db_available = db_available
        self.members = {HF_COLL: {}, PSS_COLL: {}}
        self.calls = []
        self.force_display_outcome = None

    def put(self, collection, doc):
        self.members.setdefault(collection, {})[int(doc['telegram_id'])] = dict(doc)

    def _doc(self, collection, member_id):
        return self.members.get(collection or HF_COLL, {}).get(int(member_id))

    # --- boot / refresh
    def initialize(self):
        self.calls.append(('initialize',))
        return True

    def ensure_authorized_members_seed(self, default_members, collection=None):
        self.calls.append(('ensure_authorized_members_seed', collection))

    def get_authorized_member_records(self, include_inactive=True, collection=None):
        self.calls.append(('get_authorized_member_records', include_inactive, collection))
        if not self.db_available:
            return None
        docs = list(self.members.get(collection or HF_COLL, {}).values())
        if not include_inactive:
            docs = [d for d in docs if d.get('active') is not False]
        return [dict(d) for d in docs]

    # --- member writers
    def mark_member_started(self, member_id, started=True, collection=None):
        self.calls.append(('mark_member_started', member_id, started, collection))
        if not self.db_available:
            return False
        doc = self._doc(collection, member_id)
        if doc is None:
            return False
        doc['has_started_bot'] = bool(started)
        return True

    def set_member_first_name(self, member_id, first_name, collection=None):
        self.calls.append(('set_member_first_name', member_id, first_name, collection))
        if not self.db_available:
            return False
        doc = self._doc(collection, member_id)
        if doc is None:
            return False
        doc['telegram_first_name'] = str(first_name)
        doc['telegram_first_name_at'] = utcnow()
        return True

    def set_member_display_name(self, member_id, display_name, collection=None):
        self.calls.append(('set_member_display_name', member_id, display_name,
                           collection))
        if self.force_display_outcome is not None:
            return (self.force_display_outcome, None)
        if not self.db_available:
            return ('offline', None)
        doc = self._doc(collection, member_id)
        if doc is None or doc.get('active') is False:
            return ('missing', None)
        before = dict(doc)
        doc['display_name'] = str(display_name)
        doc['display_name_set_at'] = utcnow()
        return ('ok', before)

    def set_member_profile(self, member_id, collection=None, display_name=None,
                           blurb=None):
        self.calls.append(('set_member_profile', member_id, collection,
                           display_name, blurb))
        doc = self._doc(collection, member_id)
        if not self.db_available or doc is None:
            return False
        if display_name is not None:
            doc['display_name'] = str(display_name).strip()
        if blurb is not None:
            doc['blurb'] = str(blurb).strip()
        return True

    def get_member_profile_doc(self, member_id, collection=None):
        self.calls.append(('get_member_profile_doc', member_id, collection))
        doc = self._doc(collection, member_id)
        return dict(doc) if doc is not None else None

    # --- assertion helpers
    def called(self, method):
        return [c for c in self.calls if c[0] == method]


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
        self.edited.append((str(chat_id), message_id, text, reply_markup))
        return None

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, **kw):
        return None

    async def delete_message(self, chat_id=None, message_id=None, **kw):
        return None

    def texts_to(self, user_id):
        return [t for c, t, _m in self.sent if c == str(user_id)]


class Rec:
    def __init__(self):
        self.replies = []   # (text, reply_markup)
        self.answers = []   # (text, show_alert)


def text_update(user_id, text, rec, first_name="Robin", chat_type="private",
                with_chat=True, username=None):
    """A message update. with_chat=False builds one with NO effective_chat, the
    shape a private-chat check must fail CLOSED on."""
    async def reply_text(t, reply_markup=None, **kw):
        rec.replies.append((t, reply_markup))

    user = SimpleNamespace(id=user_id, username=username, first_name=first_name,
                           last_name=None)
    msg = SimpleNamespace(text=text, reply_text=reply_text, photo=[], sticker=None)
    fields = dict(effective_user=user, message=msg, callback_query=None)
    if with_chat:
        fields['effective_chat'] = SimpleNamespace(id=user_id, type=chat_type)
    return SimpleNamespace(**fields)


def cb_update(user_id, data, rec, first_name="Robin", username=None,
              chat_type="private"):
    async def answer(t=None, show_alert=False):
        rec.answers.append((t, show_alert))

    async def edit_message_text(t, reply_markup=None, **kw):
        rec.replies.append((t, reply_markup))

    async def edit_message_reply_markup(reply_markup=None, **kw):
        return None

    fu = SimpleNamespace(id=user_id, username=username, first_name=first_name,
                         last_name=None)
    q = SimpleNamespace(from_user=fu, data=data, answer=answer,
                        edit_message_text=edit_message_text,
                        edit_message_reply_markup=edit_message_reply_markup)
    return SimpleNamespace(effective_user=fu, message=None, callback_query=q,
                           effective_chat=SimpleNamespace(id=user_id, type=chat_type))


class LogCatcher(logging.Handler):
    """Collects records at or above `level`, so a case can assert a log line."""

    def __init__(self, level=logging.INFO):
        logging.Handler.__init__(self, level)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self):
        return [r.getMessage() for r in self.records]


@contextlib.contextmanager
def catching(level=logging.INFO):
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
    # A REBIND, not a .clear(): see test_registration.reset_state.
    config.REGISTRATION_ADMINS = frozenset()
    config.SERVICES[ServiceType.HF.value].roster.replace_records([])
    config.SERVICES[ServiceType.PSS.value].roster.replace_records([])


def member_doc(member_id, display_name="", first_name="", **overrides):
    doc = {
        'telegram_id': member_id,
        'display_name': display_name,
        'telegram_first_name': first_name,
        'available': True,
        'has_started_bot': True,
        'active': True,
    }
    doc.update(overrides)
    return doc


def pss_roster(docs):
    pss = config.SERVICES[ServiceType.PSS.value]
    pss.roster.replace_records(docs)
    return pss


# --------------------------------------------------------------------------- cases
ACCEPTED = ('Sam', 'Mary-Ann', "O'Brien", 'J. Tan', 'A.J.', 'José', '李明', 'Nguyễn',
            'Sunny 🌻', 'Sup 00', 'Sup 3000', 'Member9000', 'Anne-Marie O’Neil',
            '❤️ Mei', 'x' * 32)

REJECTED = (
    ('Sam\nLee', 'name_multiline'),
    ('​Sam', 'name_invisible_chars'),
    ('Sam‮', 'name_invisible_chars'),
    ('Sam\t', 'name_invisible_chars'),
    ('\U0001F469‍⚕️ Ann', 'name_invisible_chars'),
    ('', 'name_empty'),
    ('   ', 'name_empty'),
    ('x' * 33, 'name_too_long'),
    ('sam@x', 'name_has_at'),
    ('＠sam', 'name_has_at'),
    ('t.me/sam', 'name_looks_like_link'),
    ('www.sam', 'name_looks_like_link'),
    ('sam.com', 'name_looks_like_link'),
    ('Dr.Who', 'name_looks_like_link'),
    ('J.Tan', 'name_looks_like_link'),
    ('http://x.io', 'name_looks_like_link'),
    ('Call 91234567', 'name_has_phone'),
    ('9123 4567', 'name_has_phone'),
    ('12-345', 'name_has_phone'),
    ('+65 9123 4567', 'name_has_phone'),
    ('Sam!', 'name_bad_chars'),
    ('Sam#1', 'name_bad_chars'),
    ('<b>Sam</b>', 'name_bad_chars'),
    ('Sam (2)', 'name_bad_chars'),
    ('Alex (he/him)', 'name_bad_chars'),
    ('Ś̂̃am', 'name_bad_chars'),
    ('1234', 'name_numeric'),
    ('\U0001F33B', 'name_needs_letter'),
    ('...', 'name_needs_letter'),
)


def case_a_accepted_names():
    for name in ACCEPTED:
        assert name_problem(name) is None, (name, name_problem(name))
        assert config.supporter_name_problem(name) is None, name
    assert clean_name('  Sam   Lee ') == 'Sam Lee'
    assert clean_name(None) == ''
    print("OK  a. ordinary names in several scripts, with emoji and - ' . are accepted")


def case_b_rejected_names_give_their_exact_key():
    for raw, key in REJECTED:
        got = name_problem(raw)
        assert got == key, (raw, got, key)
        assert got in REJECTION_KEYS, got
    print("OK  b. every rejection example returns its exact, first-matching key")


def case_c_every_rejection_has_plain_copy():
    for key in REJECTION_KEYS:
        value = MESSAGES.get(key)
        assert isinstance(value, str) and value.strip(), key
        assert '{' not in value, (key, value)
    assert str(NAME_MAX_LENGTH) in MESSAGES['name_too_long']
    print("OK  c. every rejection key has plain, template-free copy")


def case_d_name_key_collapses_look_alikes():
    same = ('Alex', 'ALEX', ' a l e x ', 'Álex', 'Аlex', 'ΑLEX', 'A1ex',
            'Ａｌｅｘ', '\U0001D400\U0001D425\U0001D41E\U0001D431')
    keys = {name_key(n) for n in same}
    assert keys == {name_key('Alex')}, {n: name_key(n) for n in same}
    assert name_key('Mary Ann') == name_key('MaryAnn') == name_key('Mary-Ann')
    assert name_key('Hanna') == name_key('ΗΑΝΝΑ')
    assert name_key('Alex') != name_key('Alexa')
    assert len({name_key('Sup %02d' % n) for n in range(20)}) == 20, \
        "the directed suite's 'Sup NN' fixtures must not collide with each other"
    print("OK  d. name_key collapses case, accents, spacing and homoglyphs, and no more")


def case_e_disambiguate_numbers_every_member_of_a_collision():
    expected = {10: 'Alex (1)', 20: 'Alex (2)'}
    assert disambiguate([(20, 'Alex'), (10, 'Alex')]) == expected
    assert disambiguate([(10, 'Alex'), (20, 'Alex')]) == expected, "input order is irrelevant"

    mixed = disambiguate([(20, 'Alex'), (10, 'Аlex'), (5, 'Bo')])
    assert mixed == {10: 'Аlex (1)', 20: 'Alex (2)', 5: 'Bo'}, mixed

    three = disambiguate([(3, 'sam'), (SECRET_ID, 'Sam'), (1, 'SAM')])
    assert three == {1: 'SAM (1)', 3: 'sam (2)', SECRET_ID: 'Sam (3)'}, three
    for label in three.values():
        assert str(SECRET_ID) not in label and '@' not in label, label
    # The suffix cannot be typed in: parentheses fail the rules.
    assert name_problem(three[SECRET_ID]) == 'name_bad_chars'
    print("OK  e. collisions are numbered by id order; no id or username is ever shown")


def case_f_listed_name_precedence():
    P = config.MemberProfile
    assert config.listed_name(None) == ''
    assert config.listed_name(P(1, display_name='Zed', telegram_first_name='Robin')) == 'Zed'
    assert config.listed_name(P(1, display_name='  ', telegram_first_name='Robin')) == 'Robin'
    assert config.listed_name(P(1, display_name='Sam@x', telegram_first_name='Robin')) == '', \
        "an invalid override must NEVER fall back to the real first name"
    assert config.listed_name(P(1, telegram_first_name='Alex (he/him)')) == ''
    # 40 chars with a space at index 31: cut to 32, and the trailing space goes.
    forty = 'A' * 31 + ' ' + 'B' * 8
    assert len(forty) == 40
    assert config.listed_name(P(1, telegram_first_name=forty)) == 'A' * 31
    # A long OVERRIDE is not truncated: the supporter typed it, and /name refuses it.
    assert config.listed_name(P(1, display_name=forty, telegram_first_name='Robin')) == ''
    assert config.listed_name(P(1)) == ''
    print("OK  f. override > Telegram first name > nothing; a bad override hides them")


def case_g_store_holds_the_first_name():
    reset_state()
    Store = config.AuthorizedMembersStore
    prof = Store._profile_from_doc({'telegram_id': 1, 'telegram_first_name': '  Robin '})
    assert prof.telegram_first_name == 'Robin'
    for key in ('telegram_first_name', 'telegram_first_name_at', 'display_name_set_at'):
        assert key in config._PROFILE_KEYS, key
    prof = Store._profile_from_doc({'telegram_id': 1, 'telegram_first_name': 'R',
                                    'telegram_first_name_at': 'x',
                                    'display_name_set_at': 'y'})
    assert prof.fields == {}, prof.fields

    store = Store()
    store.replace([7])                       # a member with NO profile
    assert store.set_display_name(7, 'Sam') is False
    assert store.set_first_name(7, 'Robin') is False
    assert store.set_first_name(8, 'Robin', create_if_missing=True) is False, \
        "a NON-member never gets a profile, flag or not"
    assert store.profile(8) is None
    assert store.set_first_name('bad', 'Robin', create_if_missing=True) is False
    assert store.set_first_name(7, 'Robin', create_if_missing=True) is True
    created = store.profile(7)
    assert (created.has_started_bot, created.available, created.display_name,
            created.telegram_first_name) == (True, True, '', 'Robin'), created
    assert store.set_display_name(7, 'Sam') is True
    assert store.profile(7).display_name == 'Sam'
    assert store.set_first_name(7, 'Robyn') is True
    assert store.profile(7).telegram_first_name == 'Robyn'

    store2 = Store()
    assert store2.replace_records([member_doc(9, first_name='A')]) is True
    assert store2.replace_records([member_doc(9, first_name='A')]) is False
    assert store2.replace_records([member_doc(9, first_name='B')]) is True, \
        "a first-name-only change is a change, or a refresh would never pick up a rename"
    print("OK  g. the store reads, sets and creates-on-capture the first name, members only")


def case_h_listing_labels_order_and_omissions():
    reset_state()
    pss = pss_roster([
        member_doc(101, display_name='Zed', first_name='Robin'),   # override
        member_doc(102, first_name='Alex'),                        # auto
        member_doc(103),                                           # no name at all
        member_doc(104, display_name='sam@x', first_name='Sam'),   # invalid override
        member_doc(105, first_name='Mei'),                         # removed below
        member_doc(106, first_name='bob'),                         # casefold ordering
    ])
    pss.roster.remove(105)

    labels = config.supporter_labels('pss')
    assert labels == {101: 'Zed', 102: 'Alex', 106: 'bob'}, labels
    assert config.supporter_label('pss', 101) == 'Zed'
    assert config.supporter_label('pss', 105) == '', "removed from the roster"
    assert config.supporter_label('pss', 'nope') == ''
    order = [(p.telegram_id, label) for p, label in config.listable_supporters('pss')]
    assert order == [(102, 'Alex'), (106, 'bob'), (101, 'Zed')], order
    assert config.omitted_supporters('pss') == [103, 104], config.omitted_supporters('pss')

    # One member per busy reason, plus a free control. Every reason reads the same.
    reset_state()
    pss_roster([
        member_doc(201, first_name='Ann'),                          # free control
        member_doc(202, first_name='Ben', has_started_bot=False),
        member_doc(203, first_name='Cai', available=False),
        member_doc(204, first_name='Dee'),
        member_doc(205, first_name='Eve'),
        member_doc(206, first_name='Fay'),
        member_doc(207, first_name='Gus'),
        member_doc(208, first_name='Hal'),                          # the requester
    ])
    config.user_to_session_map[204] = 'sess-1'
    config.directed_by_member[205] = 'q-1'
    config.user_to_queue_map[206] = 'q-2'
    entries = config.picker_entries('pss', requester_id=208, declined=['207', 'junk'])
    got = [(p.telegram_id, label, free) for p, label, free in entries]
    assert got == [
        (201, 'Ann', True), (202, 'Ben', False), (203, 'Cai', False),
        (204, 'Dee', False), (205, 'Eve', False), (206, 'Fay', False),
        (207, 'Gus', False),
    ], got
    assert all(len(e) == 3 for e in entries), "free is one bit; the reason is not exposed"
    print("OK  h. labels, (label, id) order, omissions and every busy reason as free=False")


def case_i_fork_is_offered_only_when_someone_else_is_listable():
    reset_state()
    pss = config.SERVICES['pss']
    saved = pss.directed_enabled
    try:
        pss_roster([member_doc(301, first_name='Ann', available=False),
                    member_doc(302, first_name='Ben', has_started_bot=False)])
        pss.directed_enabled = False
        assert config.directed_fork_offered('pss', 999) is False
        pss.directed_enabled = True
        assert config.directed_fork_offered('pss', 999) is True, \
            "every listable member busy still shows the list"
        pss_roster([member_doc(301, first_name='Ann')])
        assert config.directed_fork_offered('pss', 301) is False, \
            "the only listable member is the requester themselves"
        pss_roster([member_doc(301)])
        assert config.directed_fork_offered('pss', 999) is False, "nobody has a name"
    finally:
        pss.directed_enabled = saved
    print("OK  i. the fork needs directed mode AND someone else listable, busy or not")


def case_j_availability_uses_the_listed_name():
    reset_state()
    pss_roster([member_doc(401, first_name='Robin'),
                member_doc(402, first_name='Alex (he/him)')])
    assert config.is_supporter_available('pss', 401) is True, \
        "no display_name, but a usable Telegram first name"
    assert config.is_supporter_available('pss', 402) is False
    assert [p.telegram_id for p in config.available_supporters('pss')] == [401]
    print("OK  j. is_supporter_available accepts a supporter named only by Telegram")


def case_k_name_taken_by_other():
    reset_state()
    pss_roster([
        member_doc(501, first_name='Alex'),
        member_doc(502, display_name='Zed', first_name='Robin'),   # Robin is shadowed
        member_doc(503, display_name='bad@name'),                  # unlisted
        member_doc(504, first_name='Mei'),
    ])
    assert config.name_taken_by_other('pss', 504, 'Alex') is True
    assert config.name_taken_by_other('pss', 504, 'aLeX') is True
    assert config.name_taken_by_other('pss', 504, 'Аlex') is True, "homoglyph"
    assert config.name_taken_by_other('pss', 504, 'zed') is True
    assert config.name_taken_by_other('pss', 501, 'Alex') is False, "their own name"
    assert config.name_taken_by_other('pss', 504, 'bad@name') is False, \
        "an unlisted member's invalid raw name is shown to nobody"
    assert config.name_taken_by_other('pss', 504, 'Robin') is False, \
        "a Telegram name hidden behind an override is shown to nobody"
    print("OK  k. only another member's LISTED name counts as taken")


def case_l_reserved_words_cover_the_bots_own_ui():
    buttons = [v for k, v in MESSAGES.items() if k.endswith('_button')]
    assert len(buttons) >= 10, buttons
    for label in buttons:
        assert name_key(label) in config.RESERVED_NAME_KEYS, label
    for svc in config.SERVICES.values():
        assert name_key(svc.member_label) in config.RESERVED_NAME_KEYS, svc.key
        assert name_key(svc.display_name) in config.RESERVED_NAME_KEYS, svc.key
    assert name_key('busy') in config.RESERVED_NAME_KEYS
    assert '' not in config.RESERVED_NAME_KEYS
    assert config.supporter_name_problem('Cancel') == 'name_reserved'
    assert config.supporter_name_problem('ANYONE') == 'name_reserved'
    assert name_problem('Cancel') is None, "the pure rules know no reserved words"
    print("OK  l. button labels, track names and UI words cannot be a listed name")


class MemberCollection:
    """One pymongo collection keyed by telegram_id, matching filters the way Mongo
    does for the operators these writers use -- so a writer that lost its `active`
    clause really would match a deactivated member."""

    def __init__(self, docs, explode=False):
        self.docs = {d['telegram_id']: d for d in docs}
        self.explode = explode
        self.calls = []

    @staticmethod
    def _matches(doc, flt):
        for key, cond in flt.items():
            value = doc.get(key)
            if isinstance(cond, dict):
                if '$ne' in cond and value == cond['$ne']:
                    return False
            elif value != cond:
                return False
        return True

    def update_one(self, flt, update, upsert=False, **kw):
        self.calls.append(('update_one', dict(flt), update, upsert))
        if self.explode:
            raise RuntimeError("boom")
        assert upsert is False, "no member writer may upsert"
        for doc in self.docs.values():
            if self._matches(doc, flt):
                doc.update(update.get('$set') or {})
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    def find_one_and_update(self, flt, update, return_document=None, upsert=False, **kw):
        self.calls.append(('find_one_and_update', dict(flt), update, return_document,
                           upsert))
        if self.explode:
            raise RuntimeError("boom")
        for doc in self.docs.values():
            if self._matches(doc, flt):
                before = dict(doc)
                doc.update(update.get('$set') or {})
                return before
        return None


def case_m_the_real_mongo_writers():
    real = manager_mod.db_mgr
    saved_conn = manager_mod.db_manager
    saved_available = real.db_available

    def use(collection):
        manager_mod.db_manager = SimpleNamespace(db={PSS_COLL: collection})

    try:
        real.db_available = True
        coll = MemberCollection([{'telegram_id': 601, 'active': True,
                                  'display_name': 'Old'},
                                 {'telegram_id': 602, 'active': False}])
        use(coll)

        assert real.set_member_first_name(601, 'Robin', collection=PSS_COLL) is True
        op, flt, update, upsert = coll.calls[-1]
        assert (op, flt, upsert) == ('update_one', {'telegram_id': 601}, False), coll.calls[-1]
        assert set(update['$set']) == {'telegram_first_name', 'telegram_first_name_at',
                                       'updated_at'}, update
        assert coll.docs[601]['telegram_first_name'] == 'Robin'
        assert real.set_member_first_name(699, 'X', collection=PSS_COLL) is False, \
            "no match, and no upsert creating a stranger's roster row"
        assert 699 not in coll.docs

        outcome, before = real.set_member_display_name(601, 'Sam', collection=PSS_COLL)
        op, flt, update, return_document, upsert = coll.calls[-1]
        assert op == 'find_one_and_update'
        assert flt == {'telegram_id': 601, 'active': {'$ne': False}}, flt
        assert return_document is ReturnDocument.BEFORE
        assert not upsert
        assert set(update['$set']) == {'display_name', 'display_name_set_at',
                                       'updated_at'}, update
        assert outcome == 'ok' and before['display_name'] == 'Old', (outcome, before)
        assert coll.docs[601]['display_name'] == 'Sam'

        assert real.set_member_display_name(602, 'Sam', collection=PSS_COLL) == \
            ('missing', None), "a member deactivated in Mongo is refused"
        assert 'display_name' not in coll.docs[602]

        use(MemberCollection([], explode=True))
        with catching(logging.ERROR) as log:
            assert real.set_member_display_name(601, 'Sam', collection=PSS_COLL) == \
                ('error', None)
            assert real.set_member_first_name(601, 'Robin', collection=PSS_COLL) is False
        assert len(log.records) == 2, log.messages()

        untouched = MemberCollection([{'telegram_id': 601, 'active': True}])
        use(untouched)
        real.db_available = False
        assert real.set_member_display_name(601, 'Sam', collection=PSS_COLL) == \
            ('offline', None)
        assert real.set_member_first_name(601, 'Robin', collection=PSS_COLL) is False
        assert untouched.calls == [], "offline must not touch the collection"
    finally:
        manager_mod.db_manager = saved_conn
        real.db_available = saved_available
    print("OK  m. the real writers: exact filters, BEFORE, no upsert, four outcomes")


CASES = [
    case_a_accepted_names,
    case_b_rejected_names_give_their_exact_key,
    case_c_every_rejection_has_plain_copy,
    case_d_name_key_collapses_look_alikes,
    case_e_disambiguate_numbers_every_member_of_a_collision,
    case_f_listed_name_precedence,
    case_g_store_holds_the_first_name,
    case_h_listing_labels_order_and_omissions,
    case_i_fork_is_offered_only_when_someone_else_is_listable,
    case_j_availability_uses_the_listed_name,
    case_k_name_taken_by_other,
    case_l_reserved_words_cover_the_bots_own_ui,
    case_m_the_real_mongo_writers,
]


async def run():
    for case in CASES:
        result = case()
        if asyncio.iscoroutine(result):
            await result


if __name__ == "__main__":
    # A driver that discovers its own tests reports success when it discovers
    # NOTHING. A refactor, a rename, an import shadow or a bad merge all reach that
    # state. Coverage here may grow; it may not silently shrink.
    assert len(CASES) >= 13, (
        "expected at least 13 cases, collected %d (%s). Test discovery has "
        "regressed -- fix the discovery, do not lower this number."
        % (len(CASES), ", ".join(c.__name__ for c in CASES) or "none")
    )

    real_mgrs = (handlers_mod.db_mgr, main.db_mgr, utils_mod.db_mgr)
    real_connection = manager_mod.db_manager
    real_available = manager_mod.db_mgr.db_available
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]
    saved = (hf.channel_id, hf.enabled, hf.directed_enabled,
             pss.channel_id, pss.enabled, pss.directed_enabled)
    try:
        asyncio.run(run())
        print("\nAll %d supporter-name assertions passed!" % len(CASES))
    finally:
        handlers_mod.db_mgr, main.db_mgr, utils_mod.db_mgr = real_mgrs
        manager_mod.db_manager = real_connection
        manager_mod.db_mgr.db_available = real_available
        (hf.channel_id, hf.enabled, hf.directed_enabled,
         pss.channel_id, pss.enabled, pss.directed_enabled) = saved
        # reset_state() empties both rosters with replace_records([]) FIRST:
        # replace() with the same id set is a no-op that keeps existing profiles,
        # so scratch profiles would leak into any suite sharing this process.
        reset_state()
        hf.roster.replace(config.DEFAULT_HEARTFELT_MEMBERS)
        pss.roster.replace(config.DEFAULT_PSS_MEMBERS)
