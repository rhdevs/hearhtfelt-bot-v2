#!/usr/bin/env python3
"""
Session auto-expiry: activity tracking, the inactivity warning, expiry, and the
full cleanup cycle.

House style, matching the other seven suites: module-level functions, plain
`assert`, one entry point (`python tests/test_session_expiry.py`).

This file used to define `unittest.TestCase` subclasses whose six `async def`
tests were collected and "passed" by `unittest`/`pytest` WITHOUT EVER BEING
AWAITED -- `python -m unittest tests.test_session_expiry -v` reported
"Ran 8 tests ... OK" while emitting six `RuntimeWarning: coroutine ... was never
awaited`. Two ways to run one file, one of which silently asserted nothing.

The TestCase is gone rather than repaired (e.g. with IsolatedAsyncioTestCase)
so that discovery collects NOTHING here and can never again report a
misleading pass. `unittest.mock` is orthogonal to `TestCase` and stays.
"""

import asyncio
import datetime
import os
import sys
import time
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from config import UserState, active_sessions, session_warnings, user_states
from src.bot.managers import expiry as expiry_mod
from src.bot.managers import queue as queue_mod
from src.bot.managers import session as session_mod
from src.bot.managers.expiry import SessionExpiryManager
from src.bot.managers.session import SessionManager
from src.timeutil import utcnow


# --------------------------------------------------------------------------- fixtures
def reset_state():
    """Clear every global these tests can touch.

    The old `setUp` cleared only active_sessions/session_warnings/user_states.
    `create_session` feeds the SHARED `config.used_anonymous_ids` set and appends
    to `config.safety_logs`, so those leaked from test to test and would leak into
    whatever suite ran next in the same process.
    """
    for d in (config.user_states, config.user_to_service_map, config.queue_entries,
              config.user_to_queue_map, config.active_sessions,
              config.user_to_session_map, config.session_warnings):
        d.clear()
    config.queue_order.clear()
    config.used_anonymous_ids.clear()
    config.safety_logs.clear()
    # Phase 5 indices. A suite that forgets these leaks a directed request or a
    # rendered picker view into whatever runs next, and available_supporters()
    # silently starts hiding people.
    config.directed_by_member.clear()
    config.picker_views.clear()


def install(stub):
    """Swap `db_mgr` on EVERY module that holds a reference to it.

    This replaces five near-identical `with patch(...)` blocks, each of which had
    to remember both `session` and `expiry`. `src.bot.managers.expiry.db_mgr` was
    once left pointing at the real DBManager, so the mocked
    `get_sessions_by_activity` was never called and the sweep's DB-fallback branch
    ran against whatever `db_mgr` held -- the live production database, had
    anything in the same process called `db_mgr.initialize()` first
    (tests/test_db_integration.py does). Two of these tests patched NOTHING at all
    and survived only by that accident. A module-level swap makes forgetting one
    module impossible.
    """
    session_mod.db_mgr = stub
    expiry_mod.db_mgr = stub
    queue_mod.db_mgr = stub


def make_db_stub():
    """The stub the old `setUp` built, plus the two reads `create_session` makes.

    `get_session` returning None is deliberate: it is the "no pre-existing Mongo
    row" case, so `create_session` generates a REAL anonymous id. Left as a bare
    auto-Mock (as it was), `get_session(...)` returns a truthy Mock and the
    session's `anonymous_user_id` becomes a Mock object rather than a string.
    """
    stub = Mock()
    stub.db_available = True
    stub.get_session = Mock(return_value=None)
    stub.claim_session = Mock(return_value=True)
    stub.update_session_activity = Mock(return_value=True)
    stub.log_message = Mock(return_value=True)
    stub.end_session = Mock(return_value=True)
    stub.get_sessions_by_activity = Mock(return_value=[])
    return stub


def build():
    """-> (bot, session_manager, expiry_manager, db_stub), with db_mgr installed."""
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    session_manager = SessionManager(bot)
    expiry_manager = SessionExpiryManager(bot, session_manager)
    stub = make_db_stub()
    install(stub)
    return bot, session_manager, expiry_manager, stub


# ----------------------------------------------------------------------------- tests
def test_session_activity_tracking():
    """Session activity is tracked and can be bumped."""
    reset_state()
    bot, session_manager, expiry_manager, stub = build()

    session_id = "test-session-1"
    user_id = 12345
    heartfelt_id = 67890

    session_manager.create_session(user_id, heartfelt_id, session_id)

    assert session_id in active_sessions
    session = active_sessions[session_id]
    assert 'last_activity_at' in session
    assert isinstance(session['last_activity_at'], datetime.datetime)

    old_activity = session['last_activity_at']

    # The only way to observe a monotonic timestamp bump.
    time.sleep(0.1)
    session_manager.update_session_activity(session_id)

    new_activity = active_sessions[session_id]['last_activity_at']
    assert new_activity > old_activity


async def test_message_forwarding_updates_activity():
    """Forwarding a message updates session activity, in memory and in Mongo."""
    reset_state()
    bot, session_manager, expiry_manager, stub = build()

    session_id = "test-session-2"
    user_id = 12345
    heartfelt_id = 67890

    session_manager.create_session(user_id, heartfelt_id, session_id)
    old_activity = active_sessions[session_id]['last_activity_at']

    time.sleep(0.1)
    await session_manager.forward_message(session_id, user_id, "Test message")

    new_activity = active_sessions[session_id]['last_activity_at']
    assert new_activity > old_activity

    stub.update_session_activity.assert_called_with(session_id)


async def test_sticker_forwarding_updates_activity():
    """Forwarding a sticker updates session activity."""
    reset_state()
    bot, session_manager, expiry_manager, stub = build()

    session_id = "test-session-3"
    user_id = 12345
    heartfelt_id = 67890

    session_manager.create_session(user_id, heartfelt_id, session_id)
    old_activity = active_sessions[session_id]['last_activity_at']

    time.sleep(0.1)
    await session_manager.forward_sticker(session_id, user_id, "sticker_file_id")

    new_activity = active_sessions[session_id]['last_activity_at']
    assert new_activity > old_activity


async def test_warning_system():
    """Both parties are warned once, and the session is flagged as warned."""
    reset_state()
    bot, session_manager, expiry_manager, stub = build()

    session_id = "test-session-4"
    user_id = 12345
    heartfelt_id = 67890

    session_manager.create_session(user_id, heartfelt_id, session_id)

    # 26 minutes idle: HF warns from 25 min.
    old_time = utcnow() - datetime.timedelta(minutes=26)
    active_sessions[session_id]['last_activity_at'] = old_time

    await expiry_manager._send_session_warning(session_id, active_sessions[session_id])

    assert session_warnings.get(session_id, False)

    assert bot.send_message.call_count == 2, (
        f"expected both parties to be warned, got {bot.send_message.call_count} sends")

    calls = bot.send_message.call_args_list
    assert "Are you still there?" in calls[0][1]['text']
    assert "Are you still there?" in calls[1][1]['text']


async def test_session_expiry():
    """A session past its idle timeout is closed, both states reset, both notified."""
    reset_state()
    bot, session_manager, expiry_manager, stub = build()

    session_id = "test-session-5"
    user_id = 12345
    heartfelt_id = 67890

    session_manager.create_session(user_id, heartfelt_id, session_id)

    user_states[user_id] = UserState.IN_CONVERSATION
    user_states[heartfelt_id] = UserState.IN_CONVERSATION

    # 31 minutes idle: HF expires at 30 min.
    old_time = utcnow() - datetime.timedelta(minutes=31)
    active_sessions[session_id]['last_activity_at'] = old_time

    await expiry_manager._expire_session(session_id, active_sessions[session_id])

    assert session_id not in active_sessions

    assert user_states[user_id] == UserState.IDLE
    assert user_states[heartfelt_id] == UserState.IDLE

    # system_end=True plus the additive end_reason tag introduced with restart
    # durability. assert_called_with checks the MOST RECENT call, which is the one
    # SessionManager.end_session makes (session.py passes system_end positionally);
    # expiry.py's own earlier close passes it as a keyword. Keep this shape.
    stub.end_session.assert_called_with(
        session_id, user_id, True, end_reason='idle_expired')

    assert bot.send_message.call_count == 2, (
        f"expected both parties to be notified, got {bot.send_message.call_count} sends")


async def test_cleanup_cycle():
    """One sweep over three sessions: expire, warn, leave alone."""
    reset_state()
    bot, session_manager, expiry_manager, stub = build()

    sessions_data = [
        ("session-1", 12345, 67890, 31),  # should expire (>= 30)
        ("session-2", 12346, 67891, 26),  # should warn   (>= 25, < 30)
        ("session-3", 12347, 67892, 2),   # should do nothing
    ]

    for session_id, user_id, heartfelt_id, minutes_ago in sessions_data:
        session_manager.create_session(user_id, heartfelt_id, session_id)
        user_states[user_id] = UserState.IN_CONVERSATION
        user_states[heartfelt_id] = UserState.IN_CONVERSATION

        old_time = utcnow() - datetime.timedelta(minutes=minutes_ago)
        active_sessions[session_id]['last_activity_at'] = old_time

    # Empty DB result: this exercises the memory-only path.
    stub.get_sessions_by_activity = Mock(return_value=[])

    await expiry_manager._cleanup_expired_sessions()

    assert "session-1" not in active_sessions, "31 min idle should have expired"
    assert "session-2" in active_sessions, "26 min idle should be warned, not expired"
    assert "session-3" in active_sessions, "2 min idle should be untouched"

    assert session_warnings.get("session-2", False)

    assert user_states[12345] == UserState.IDLE             # expired session's user
    assert user_states[12346] == UserState.IN_CONVERSATION  # warned session's user


async def test_warning_spam_prevention():
    """An already-warned session is not warned again on the next sweep."""
    reset_state()
    bot, session_manager, expiry_manager, stub = build()

    session_id = "test-session-6"

    # Seed BEFORE create_session -- the point of the test is that the sweep honours
    # a pre-existing flag. Seeding afterwards would test nothing.
    session_warnings[session_id] = True

    user_id = 12345
    heartfelt_id = 67890
    session_manager.create_session(user_id, heartfelt_id, session_id)

    old_time = utcnow() - datetime.timedelta(minutes=26)
    active_sessions[session_id]['last_activity_at'] = old_time

    # Drive the REAL sweep. This used to re-implement expiry.py's selection logic
    # inline and then assert on the list the test itself had just built, so it
    # passed whether or not the spam guard existed at all.
    bot.send_message.reset_mock()
    await expiry_manager._cleanup_expired_sessions()

    assert bot.send_message.call_count == 0, (
        "session_warnings already marked this session warned; re-warning every "
        "sweep is a nag loop aimed at someone in a support conversation")


def test_constants_configuration():
    """The timer constants and the messages the expiry path formats."""
    reset_state()
    from config import (
        SESSION_TIMEOUT_MINUTES, SESSION_WARNING_MINUTES,
        SESSION_SWEEP_SECONDS, QUEUE_EXPIRE_MINUTES, SERVICES, MESSAGES
    )

    # DELIBERATE Phase 2 change: the HF idle timeout moved 10 -> 30 minutes,
    # so a conversation now warns at 25 min idle and closes at 30.
    assert SESSION_TIMEOUT_MINUTES == 30
    assert SESSION_WARNING_MINUTES == 5
    assert SESSION_SWEEP_SECONDS == 180
    assert QUEUE_EXPIRE_MINUTES == 60

    # The module-level constants are only deprecated aliases now; the values
    # that actually drive behaviour live on the HF Service.
    hf = SERVICES['hf']
    assert hf.session_timeout_minutes == SESSION_TIMEOUT_MINUTES
    assert hf.session_warning_minutes == SESSION_WARNING_MINUTES
    assert hf.queue_expire_minutes == QUEUE_EXPIRE_MINUTES

    required_messages = [
        "session_warning", "session_expired", "session_expired_heartfelt"
    ]
    for msg_key in required_messages:
        assert msg_key in MESSAGES
        assert isinstance(MESSAGES[msg_key], str)
        assert len(MESSAGES[msg_key]) > 0


# ------------------------------------------------------------------------------ driver
async def run():
    test_session_activity_tracking()
    print("OK  1. session activity is tracked and bumped")

    await test_message_forwarding_updates_activity()
    print("OK  2. forwarding a message updates activity, in memory and in Mongo")

    await test_sticker_forwarding_updates_activity()
    print("OK  3. forwarding a sticker updates activity")

    await test_warning_system()
    print("OK  4. both parties are warned once at 26 minutes idle")

    await test_session_expiry()
    print("OK  5. a 31-minute-idle session is closed and both parties notified")

    await test_cleanup_cycle()
    print("OK  6. one sweep expires, warns and ignores the right sessions")

    await test_warning_spam_prevention()
    print("OK  7. an already-warned session is not warned again")

    test_constants_configuration()
    print("OK  8. timer constants and expiry messages are configured")


if __name__ == "__main__":
    # Exceptions propagate: the other seven suites show a full traceback and exit
    # non-zero. The old `try/except Exception -> print -> return False` wrapper
    # threw the traceback away and reported a one-line message.
    real = (session_mod.db_mgr, expiry_mod.db_mgr, queue_mod.db_mgr)
    try:
        asyncio.run(run())
        print("\nAll session-expiry assertions passed!")
    finally:
        (session_mod.db_mgr, expiry_mod.db_mgr, queue_mod.db_mgr) = real
        reset_state()
