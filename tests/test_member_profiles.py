#!/usr/bin/env python3
"""
Member-profile and roster-refresh guards (Phase 4/5 supporter picker).

This suite protects the pieces that decide WHO a requester is ever shown, and
whether that person can safely be DMed:

  - config.MemberProfile / config._PROFILE_KEYS / AuthorizedMembersStore: the
    shape-stable profile store (D14). Unknown Mongo keys must land verbatim in
    `fields`; a bare-id roster (no profile data) must stay authorized-but-invisible,
    which is the exact code path tests/test_pss_flow.py and tests/test_restore.py
    already depend on.
  - config.is_supporter_available / available_supporters: the seven-clause
    exclusion predicate that keeps a directed request from stranding or
    double-stacking on one supporter.
  - main.refresh_all_rosters: the periodic Mongo sync. include_inactive=False is
    the one line that keeps a deactivated member from being silently
    re-authorized on the next refresh -- a security regression with no
    user-visible symptom, so this suite drives the REAL coroutine against a stub
    that honours the filter the way Mongo actually would.
  - src/bot/handlers.py's has_started_bot bookkeeping: the private-chat gate that
    stands between the bot and Telegram's "bot can't initiate conversation" error.
  - src/database/manager.py's matched_count-vs-modified_count contract for
    availability toggles.

Run directly: `python tests/test_member_profiles.py`
"""

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from config import MESSAGES, ServiceType

import main
import src.bot.handlers as handlers_mod
import src.database.manager as db_manager_mod


# --------------------------------------------------------------------------- fixtures
def reset_state():
    for d in (config.user_states, config.user_to_service_map, config.queue_entries,
              config.user_to_queue_map, config.active_sessions, config.user_to_session_map,
              config.session_warnings):
        d.clear()
    config.queue_order.clear()
    config.used_anonymous_ids.clear()
    config.safety_logs.clear()
    # Phase 5 indices. A suite that forgets these leaks a directed request or a
    # rendered picker view into whatever runs next, and available_supporters()
    # silently starts hiding people.
    config.directed_by_member.clear()
    config.picker_views.clear()
    # Every test in this file builds its OWN roster from scratch. `replace()` with
    # the same id set is a no-op that KEEPS existing profiles, so we must go
    # through `replace_records([])` to actually drop profile data -- otherwise the
    # driver's alphabetical ordering lets one test's scratch profiles leak into the
    # next.
    config.SERVICES[ServiceType.HF.value].roster.replace_records([])
    config.SERVICES[ServiceType.PSS.value].roster.replace_records([])


# --------------------------------------------------------------------------- (a)
def test_a_replace_records_sorted_profiles_and_membership_intact():
    reset_state()
    pss = config.SERVICES[ServiceType.PSS.value]
    docs = [
        {'telegram_id': 100, 'display_name': 'Bea'},
        {'telegram_id': 101, 'display_name': 'alex'},     # casefold ordering matters:
                                                            # 'alex' < 'Bea' only once
                                                            # casefolded -- ASCII 'B'
                                                            # (66) < 'a' (97) otherwise.
        {'telegram_id': 103, 'display_name': 'charlie'},
        {'telegram_id': 105, 'display_name': 'Sam'},
        {'telegram_id': 104, 'display_name': 'Sam'},       # shares a display_name with
                                                            # 105 -- exercises the
                                                            # telegram_id tiebreak.
    ]
    changed = pss.roster.replace_records(docs)
    assert changed is True

    assert len(pss.roster) == 5, len(pss.roster)
    for doc in docs:
        assert doc['telegram_id'] in pss.roster
    assert pss.roster.snapshot() == [100, 101, 103, 104, 105]

    ordered_ids = [p.telegram_id for p in pss.roster.profiles()]
    assert ordered_ids == [101, 100, 103, 104, 105], ordered_ids


# --------------------------------------------------------------------------- (b)
def test_b_unknown_keys_land_in_fields_verbatim():
    reset_state()
    pss = config.SERVICES[ServiceType.PSS.value]
    doc = {
        'telegram_id': 200,
        'display_name': 'Known Fields',
        'blurb': 'a blurb',
        'available': False,
        'has_started_bot': True,
        'username': 'someone',
        'active': True,
        '_id': 'mongo-oid',
        'created_at': 'x',
        'updated_at': 'y',
        'started_bot_at': 'z',
        # The two keys nobody has heard of yet.
        'pronouns': 'they/them',
        'year': 2,
    }
    pss.roster.replace_records([doc])
    profile = pss.roster.profile(200)

    # The shape-stability guarantee (D14): every reserved key is accounted for
    # elsewhere on the dataclass, and ONLY the unknown keys survive into `fields`.
    assert profile.fields == {'pronouns': 'they/them', 'year': 2}, profile.fields
    for key in config._PROFILE_KEYS:
        assert key not in profile.fields, key


# --------------------------------------------------------------------------- (c)
def test_c_bare_int_roster_stays_on_the_unforked_code_path():
    reset_state()
    pss = config.SERVICES[ServiceType.PSS.value]
    pss.roster.replace([1, 2, 3])

    for mid in (1, 2, 3):
        assert mid in pss.roster
        profile = pss.roster.profile(mid)
        assert profile is None or profile.display_name == "", profile

    # This is exactly what keeps tests/test_pss_flow.py's enable_both_services()
    # and tests/test_restore.py's enable_both_services() -- both of which build
    # rosters with bare `roster.replace([...])` -- on today's un-forked code path:
    # a bare-id roster is authorized-but-invisible, never accidentally pickable.
    assert config.available_supporters('pss') == []


# --------------------------------------------------------------------------- (d)
def test_d_legacy_doc_defaults_absent_means_available_not_started():
    reset_state()
    pss = config.SERVICES[ServiceType.PSS.value]
    N = 300
    pss.roster.replace_records([{'telegram_id': N, 'active': True}])
    profile = pss.roster.profile(N)

    assert profile.available is True, "ABSENT available must default to True"
    assert profile.has_started_bot is False, (
        "ABSENT has_started_bot must default to False -- fail closed, Telegram "
        "forbids the bot from messaging first")
    assert profile.display_name == ""
    assert profile.blurb == ""


# --------------------------------------------------------------------------- (e)
def test_e_exclusion_matrix_one_reason_per_case_with_a_live_control():
    reset_state()
    pss = config.SERVICES[ServiceType.PSS.value]

    CONTROL = 9000
    M_NOT_ON_ROSTER = 9001
    M_NO_NAME = 9002
    M_NOT_STARTED = 9003
    M_UNAVAILABLE = 9004
    M_IN_SESSION = 9005
    M_DIRECTED = 9006
    M_IN_QUEUE = 9007

    def full_doc(mid, **overrides):
        d = {'telegram_id': mid, 'display_name': f'Member{mid}',
             'available': True, 'has_started_bot': True}
        d.update(overrides)
        return d

    docs = [
        full_doc(CONTROL),
        full_doc(M_NOT_ON_ROSTER),
        full_doc(M_NO_NAME, display_name=""),   # no post-hoc setter for display_name
        full_doc(M_NOT_STARTED),                # starts pickable; broken below
        full_doc(M_UNAVAILABLE),                # starts pickable; broken below
        full_doc(M_IN_SESSION),
        full_doc(M_DIRECTED),
        full_doc(M_IN_QUEUE),
    ]
    pss.roster.replace_records(docs)

    # Sanity: everyone that will be broken via a global-state mutation, or via a
    # setter, is genuinely pickable BEFORE the one thing breaks.
    assert config.is_supporter_available('pss', M_NOT_STARTED) is True
    assert config.is_supporter_available('pss', M_UNAVAILABLE) is True
    assert config.is_supporter_available('pss', M_IN_SESSION) is True
    assert config.is_supporter_available('pss', M_DIRECTED) is True
    assert config.is_supporter_available('pss', M_IN_QUEUE) is True
    assert config.is_supporter_available('pss', M_NOT_ON_ROSTER) is True

    # Now break exactly one thing per supporter.
    pss.roster.remove(M_NOT_ON_ROSTER)                          # 1. not on the roster
    # 2. empty display_name: already baked into the doc above.
    pss.roster.mark_started(M_NOT_STARTED, False)                # 3. has_started_bot False
    pss.roster.set_available(M_UNAVAILABLE, False)               # 4. available False
    config.user_to_session_map[M_IN_SESSION] = "sess-x"          # 5. already in a session
    config.directed_by_member[M_DIRECTED] = "queue-x"            # 6. holding a directed req
    config.user_to_queue_map[M_IN_QUEUE] = "queue-y"             # 7. waiting themselves

    # The control: nothing about it was ever touched. If this fails, the fixture
    # itself is broken and every exclusion below would pass vacuously.
    assert config.is_supporter_available('pss', CONTROL) is True
    avail_ids = [p.telegram_id for p in config.available_supporters('pss')]
    assert CONTROL in avail_ids

    cases = [
        ("1. not on the roster", M_NOT_ON_ROSTER),
        ("2. empty display_name", M_NO_NAME),
        ("3. has_started_bot False", M_NOT_STARTED),
        ("4. available False", M_UNAVAILABLE),
        ("5. member_id in user_to_session_map", M_IN_SESSION),
        ("6. member_id in directed_by_member", M_DIRECTED),
        ("7. member_id in user_to_queue_map", M_IN_QUEUE),
    ]
    for reason, mid in cases:
        assert config.is_supporter_available('pss', mid) is False, reason
        assert mid not in avail_ids, reason


# --------------------------------------------------------------------------- (f)
def test_f_refresh_all_rosters_passes_include_inactive_false():
    reset_state()
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]

    ACTIVE_ID = 8001
    INACTIVE_ID = 8002

    class RosterRefreshStub:
        """Emulates Mongo: honours the include_inactive filter exactly as
        DBManager._fetch_authorized_member_docs does. This is load-bearing --
        if the stub ignored the filter and always returned every doc, dropping
        the `include_inactive=False` kwarg in main.refresh_all_rosters would
        leave the inactive member in the roster and this test would stay green."""

        def __init__(self, by_collection):
            self.db_available = True
            self.calls = []
            self.by_collection = by_collection

        def get_authorized_member_records(self, include_inactive=True, collection=None):
            self.calls.append({'include_inactive': include_inactive, 'collection': collection})
            docs = self.by_collection.get(collection, [])
            if not include_inactive:
                docs = [d for d in docs if d.get('active') is not False]
            return list(docs)

    stub = RosterRefreshStub({
        hf.members_collection: [],
        pss.members_collection: [
            {'telegram_id': ACTIVE_ID, 'display_name': 'Active', 'active': True},
            {'telegram_id': INACTIVE_ID, 'display_name': 'Inactive', 'active': False},
        ],
    })

    real_db_mgr = main.db_mgr
    main.db_mgr = stub
    try:
        asyncio.run(main.refresh_all_rosters())
    finally:
        main.db_mgr = real_db_mgr

    assert stub.calls, "refresh_all_rosters must have called get_authorized_member_records"
    for call in stub.calls:
        # Identity, not truthiness: `False` must be the value, not merely falsy.
        assert call['include_inactive'] is False, call

    assert INACTIVE_ID not in pss.roster, "a deactivated member must not be re-authorized"
    assert ACTIVE_ID in pss.roster


# --------------------------------------------------------------------------- (g)
def test_g_refresh_all_rosters_leaves_roster_untouched_on_db_error():
    reset_state()
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]

    seed_doc = {'telegram_id': 8101, 'display_name': 'Keep Me',
                'available': True, 'has_started_bot': True}
    pss.roster.replace_records([seed_doc])
    before_ids = pss.roster.snapshot()
    before_profile = pss.roster.profile(8101)

    class NoneOnErrorStub:
        db_available = True

        def get_authorized_member_records(self, include_inactive=True, collection=None):
            return None   # DB error, distinct from a genuinely empty roster ([])

    real_db_mgr = main.db_mgr
    main.db_mgr = NoneOnErrorStub()
    try:
        asyncio.run(main.refresh_all_rosters())
    finally:
        main.db_mgr = real_db_mgr

    assert hf.roster.snapshot() == [], "hf must also be untouched (it too got None)"
    assert pss.roster.snapshot() == before_ids, "a DB error must not empty the roster"
    after_profile = pss.roster.profile(8101)
    assert after_profile == before_profile, (after_profile, before_profile)


# --------------------------------------------------------------------------- (h)
def test_h_record_bot_started_and_is_private_chat():
    reset_state()
    hf = config.SERVICES[ServiceType.HF.value]

    class RecordingDb:
        def __init__(self):
            self.db_available = True
            self.calls = []

        def mark_member_started(self, member_id, started=True, collection=None):
            self.calls.append((member_id, started, collection))
            return True

    class FakeSessionManager:
        def get_session_by_user(self, user_id):
            return None

    stub_db = RecordingDb()
    real_db = handlers_mod.db_mgr
    handlers_mod.db_mgr = stub_db
    try:
        handlers = handlers_mod.BotHandlers(FakeSessionManager(), object())
        ctx = SimpleNamespace(bot=None)

        replies = []

        async def reply_text(t, **kw):
            replies.append(t)

        # -- Case 1: a genuine private chat from a roster member -> the Mongo
        # write happens, via the REAL start_command.
        MID1 = 8301
        hf.roster.replace_records([
            {'telegram_id': MID1, 'display_name': 'Hf Member 1',
             'available': True, 'has_started_bot': False},
        ])
        user1 = SimpleNamespace(id=MID1, username="m1", first_name="M1", last_name=None)
        message1 = SimpleNamespace(reply_text=reply_text)
        chat_private = SimpleNamespace(type="private")
        update1 = SimpleNamespace(effective_user=user1, effective_chat=chat_private,
                                   message=message1, callback_query=None)
        asyncio.run(handlers.start_command(update1, ctx))
        assert stub_db.calls == [(MID1, True, hf.members_collection)], stub_db.calls
        assert hf.roster.profile(MID1).has_started_bot is True

        # -- Case 2: an update with NO effective_chat attribute at all -> nothing
        # recorded, flag unchanged. Driven through the REAL handle_message.
        stub_db.calls.clear()
        MID2 = 8302
        hf.roster.replace_records([
            {'telegram_id': MID1, 'display_name': 'Hf Member 1',
             'available': True, 'has_started_bot': True},
            {'telegram_id': MID2, 'display_name': 'Hf Member 2',
             'available': True, 'has_started_bot': False},
        ])
        user2 = SimpleNamespace(id=MID2, username="m2", first_name="M2", last_name=None)
        message2 = SimpleNamespace(text="hi", reply_text=reply_text)
        update2 = SimpleNamespace(effective_user=user2, message=message2, callback_query=None)
        assert not hasattr(update2, "effective_chat")
        asyncio.run(handlers.handle_message(update2, ctx))
        assert stub_db.calls == []
        assert hf.roster.profile(MID2).has_started_bot is False

        # -- Case 3: effective_chat.type == "channel" -> nothing recorded.
        stub_db.calls.clear()
        MID3 = 8303
        hf.roster.replace_records([
            {'telegram_id': MID1, 'display_name': 'Hf Member 1',
             'available': True, 'has_started_bot': True},
            {'telegram_id': MID3, 'display_name': 'Hf Member 3',
             'available': True, 'has_started_bot': False},
        ])
        user3 = SimpleNamespace(id=MID3, username="m3", first_name="M3", last_name=None)
        message3 = SimpleNamespace(text="hi", reply_text=reply_text)
        chat_channel = SimpleNamespace(type="channel")
        update3 = SimpleNamespace(effective_user=user3, effective_chat=chat_channel,
                                   message=message3, callback_query=None)
        asyncio.run(handlers.handle_message(update3, ctx))
        assert stub_db.calls == []
        assert hf.roster.profile(MID3).has_started_bot is False

        # -- Hot-path short circuit: a profile that ALREADY has has_started_bot=True
        # must produce ZERO mark_member_started calls, across two handle_message
        # calls in a row.
        stub_db.calls.clear()
        MID4 = 8304
        hf.roster.replace_records([
            {'telegram_id': MID4, 'display_name': 'Hf Member 4',
             'available': True, 'has_started_bot': True},
        ])
        user4 = SimpleNamespace(id=MID4, username="m4", first_name="M4", last_name=None)
        message4 = SimpleNamespace(text="hi", reply_text=reply_text)
        update4 = SimpleNamespace(effective_user=user4, effective_chat=chat_private,
                                   message=message4, callback_query=None)
        asyncio.run(handlers.handle_message(update4, ctx))
        asyncio.run(handlers.handle_message(update4, ctx))
        assert stub_db.calls == [], stub_db.calls
    finally:
        handlers_mod.db_mgr = real_db


# --------------------------------------------------------------------------- (i)
def test_i_available_command_round_trip():
    reset_state()
    hf = config.SERVICES[ServiceType.HF.value]
    MEMBER = 8401
    NON_MEMBER = 8402
    hf.roster.replace_records([
        {'telegram_id': MEMBER, 'display_name': 'Toggle Me',
         'available': True, 'has_started_bot': True},
    ])

    class RecordingDb:
        def __init__(self):
            self.db_available = True
            self.calls = []

        def set_member_availability(self, member_id, available, collection=None):
            self.calls.append((member_id, available, collection))
            return True

    stub_db = RecordingDb()
    real_db = handlers_mod.db_mgr
    handlers_mod.db_mgr = stub_db
    try:
        handlers = handlers_mod.BotHandlers(object(), object())
        ctx = SimpleNamespace(bot=None)
        replies = []

        async def reply_text(t, **kw):
            replies.append(t)

        user = SimpleNamespace(id=MEMBER, username="m", first_name="M", last_name=None)
        message = SimpleNamespace(reply_text=reply_text)
        update = SimpleNamespace(effective_user=user, message=message, callback_query=None)

        asyncio.run(handlers.unavailable_command(update, ctx))
        assert hf.roster.profile(MEMBER).available is False
        assert replies[-1] == MESSAGES["now_unavailable"]

        asyncio.run(handlers.available_command(update, ctx))
        assert hf.roster.profile(MEMBER).available is True
        assert replies[-1] == MESSAGES["now_available"]

        assert stub_db.calls == [
            (MEMBER, False, hf.members_collection),
            (MEMBER, True, hf.members_collection),
        ], stub_db.calls

        # A non-member gets exactly the neutral unknown_command string, and the
        # toggle never reaches the DB at all.
        stub_db.calls.clear()
        replies.clear()
        user2 = SimpleNamespace(id=NON_MEMBER, username="x", first_name="X", last_name=None)
        message2 = SimpleNamespace(reply_text=reply_text)
        update2 = SimpleNamespace(effective_user=user2, message=message2, callback_query=None)
        asyncio.run(handlers.available_command(update2, ctx))
        assert replies == [MESSAGES["unknown_command"]]
        assert stub_db.calls == [], "a non-member toggle must never reach the DB"
    finally:
        handlers_mod.db_mgr = real_db


# --------------------------------------------------------------------------- (j)
def test_j_matched_count_true_is_the_success_signal():
    reset_state()

    class FakeResult:
        # The no-op toggle Mongo really produces: the filter matched, but setting
        # `available` to the value it already had modifies nothing.
        matched_count = 1
        modified_count = 0

    class FakeCollection:
        def update_one(self, *a, **kw):
            return FakeResult()

    class FakeDB:
        def __getitem__(self, name):
            return FakeCollection()

    real_connection = db_manager_mod.db_manager
    real_available = db_manager_mod.db_mgr.db_available
    try:
        db_manager_mod.db_manager = SimpleNamespace(db=FakeDB())
        db_manager_mod.db_mgr.db_available = True
        result = db_manager_mod.db_mgr.set_member_availability(
            123, True, collection="peer_supporters")
        assert result is True, (
            "matched_count > 0 must count as success even when modified_count == 0, "
            "or toggling to an already-current value reports as a failure")
    finally:
        db_manager_mod.db_manager = real_connection
        db_manager_mod.db_mgr.db_available = real_available


# --------------------------------------------------------------------------- (k)
def test_k_profile_and_profiles_return_copies():
    reset_state()
    pss = config.SERVICES[ServiceType.PSS.value]
    MID = 8501
    pss.roster.replace_records([
        {'telegram_id': MID, 'display_name': 'Orig', 'available': True,
         'has_started_bot': True, 'pronoun': 'she'},
    ])

    p1 = pss.roster.profile(MID)
    p1.display_name = "Mutated"
    p1.available = False
    p1.fields["pronoun"] = "mutated"
    p1.fields["new_key"] = "sneaky"

    p2 = pss.roster.profile(MID)
    assert p2.display_name == "Orig"
    assert p2.available is True
    assert p2.fields == {"pronoun": "she"}, p2.fields

    plist = pss.roster.profiles()
    elem = plist[0]
    elem.display_name = "Mutated2"
    elem.fields["pronoun"] = "mutated2"

    plist2 = pss.roster.profiles()
    assert plist2[0].display_name == "Orig"
    assert plist2[0].fields == {"pronoun": "she"}, plist2[0].fields


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    # A driver that discovers its own tests reports success when it discovers
    # NOTHING. Verified: renaming the `test_` prefix in this file made it print
    # "All 0 tests passed!" and exit 0 -- a fully green CI step, in front of a
    # deploy to a live helpline, having run zero assertions. A refactor into a
    # class, a rename, an import shadow or a bad merge all reach that state.
    # Coverage here may grow; it may not silently shrink.
    assert len(tests) >= 11, (
        "expected at least 11 tests, collected %d (%s). Test discovery has "
        "regressed -- fix the discovery, do not lower this number."
        % (len(tests), ", ".join(t.__name__ for t in tests) or "none")
    )

    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]
    real_handlers_db_mgr = handlers_mod.db_mgr
    real_main_db_mgr = main.db_mgr
    real_db_manager = db_manager_mod.db_manager
    real_db_mgr_available = db_manager_mod.db_mgr.db_available
    try:
        for t in tests:
            t()
            print(f"OK  {t.__name__}")
        print(f"\nAll {len(tests)} member-profile tests passed!")
    finally:
        handlers_mod.db_mgr = real_handlers_db_mgr
        main.db_mgr = real_main_db_mgr
        db_manager_mod.db_manager = real_db_manager
        db_manager_mod.db_mgr.db_available = real_db_mgr_available
        # replace() with the SAME id set is a no-op that KEEPS existing profiles, so
        # replace_records([]) first to actually clear profile data before restoring
        # the real rosters -- otherwise the next suite run in this process inherits
        # scratch profiles.
        hf.roster.replace_records([])
        hf.roster.replace(config.DEFAULT_HEARTFELT_MEMBERS)
        pss.roster.replace_records([])
        pss.roster.replace([])
        reset_state()
