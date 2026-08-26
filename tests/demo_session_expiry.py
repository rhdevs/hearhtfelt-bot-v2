#!/usr/bin/env python3
"""
DEMONSTRATION SCRIPT -- NOT A TEST. IT CONTAINS ZERO ASSERTIONS.

It narrates the session-expiry lifecycle (warn at 25 min idle, close at 30 for
HF) so a new maintainer can read one page of output and understand the flow. It
ALWAYS EXITS 0 unless it crashes, so it proves nothing and is deliberately NOT
part of the CI gate.

The behaviour it narrates is asserted for real in:
  - tests/test_session_expiry.py   (warn at 26 min, expire at 31, spam guard)
  - tests/test_per_service_timers.py (per-track HF and PSS windows)
  - tests/test_restore.py::case_aa_activity_rearms_the_warning

It used to print an unconditional summary: with `_send_session_warning` stubbed
out to a no-op it still exited 0 and still printed
"- Warning sent after 25+ minutes of inactivity" and
"- Both parties notified appropriately". Every line below now reports what was
OBSERVED, and prints a ❌ line when the observation does not match the
narration.

Run directly: `python tests/demo_session_expiry.py`
"""

import asyncio
import datetime
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.timeutil import utcnow
from src.bot.managers.session import SessionManager
from src.bot.managers.expiry import SessionExpiryManager
from config import active_sessions, session_warnings, user_states, UserState

# Findings recorded by report(); the footer reprints them if anything failed.
_failures = []


def report(observed_ok: bool, yes: str, no: str) -> bool:
    """Print ✅ or ❌ based on what actually happened, never on what we expected."""
    if observed_ok:
        print(f"   ✅ {yes}")
    else:
        print(f"   ❌ {no}")
        _failures.append(no)
    return observed_ok


class MockBot:
    """Mock bot for the demonstration"""

    def __init__(self):
        self.sent_messages = []

    async def send_message(self, chat_id, text):
        self.sent_messages.append({
            'chat_id': chat_id,
            'text': text,
            'timestamp': utcnow()
        })
        print(f"📱 Message to {chat_id}: {text}")


async def demonstrate_session_expiry():
    """Demonstrate the complete session expiry flow"""
    print("🚀 Session Expiry Demonstration")
    print("=" * 50)

    # Clear state
    active_sessions.clear()
    session_warnings.clear()
    user_states.clear()

    # NOTE: no db_mgr stub is installed anywhere here. That is safe ONLY because
    # nothing in this process calls db_mgr.initialize() first, so db_mgr.db_available
    # is False and every DB branch is skipped. Run this in the same process as
    # tests/test_db_integration.py and the sweep below would talk to whatever
    # MONGODB_URI points at. It is a demo; do not restructure it, just do not do that.
    mock_bot = MockBot()
    session_manager = SessionManager(mock_bot)
    expiry_manager = SessionExpiryManager(mock_bot, session_manager)

    session_id = "demo-session-123"
    user_id = 12345
    heartfelt_id = 67890

    print(f"\n1. Creating session {session_id[:8]}...")
    session_manager.create_session(user_id, heartfelt_id, session_id)
    user_states[user_id] = UserState.IN_CONVERSATION
    user_states[heartfelt_id] = UserState.IN_CONVERSATION

    report(session_id in active_sessions,
           f"Session created with users {user_id} and {heartfelt_id}",
           f"Session {session_id} is NOT in active_sessions after create_session")
    print(f"   📊 Active sessions: {len(active_sessions)}")

    # Simulate message exchange
    print("\n2. Simulating message exchange...")
    before_exchange = len(mock_bot.sent_messages)
    await session_manager.forward_message(session_id, user_id, "Hello, I need help")
    await session_manager.forward_message(session_id, heartfelt_id, "Hi! How can I assist you?")
    exchanged = len(mock_bot.sent_messages) - before_exchange

    report(exchanged == 2,
           f"Messages exchanged ({exchanged} forwarded), activity updated",
           f"expected 2 forwarded messages, observed {exchanged}")
    print(f"   🕐 Last activity: {active_sessions[session_id]['last_activity_at']}")

    # Fast-forward to the warning band. HF warns from 25 minutes of inactivity.
    print("\n3. Fast-forwarding to 26 minutes of inactivity (warning threshold)...")
    old_time = utcnow() - datetime.timedelta(minutes=26)
    active_sessions[session_id]['last_activity_at'] = old_time

    print(f"   🕐 Simulated last activity: {old_time}")

    print("\n4. Running cleanup cycle...")
    before_warning = len(mock_bot.sent_messages)
    await expiry_manager._cleanup_expired_sessions()
    warning_messages = len(mock_bot.sent_messages) - before_warning

    print(f"   📊 Warning flags: {session_warnings}")
    print(f"   📱 Messages sent this cycle: {warning_messages}")

    warned = bool(session_warnings.get(session_id))
    report(warned,
           "Session is flagged as warned",
           f"session_warnings has no flag for {session_id}: the warning never fired")
    report(warning_messages == 2,
           "Warning delivered to both parties",
           f"expected 2 warning messages, observed {warning_messages}")
    still_active_after_warning = session_id in active_sessions
    report(still_active_after_warning,
           "Session is still active -- warned, not closed",
           "session was CLOSED at 26 minutes; it should only have been warned")

    # Fast-forward past the 30-minute HF timeout. This is 5 more minutes on top of
    # the 26 above, i.e. 31 minutes of inactivity in total.
    print("\n5. Fast-forwarding 5 more minutes, to 31 minutes total (expiry threshold)...")
    old_time = utcnow() - datetime.timedelta(minutes=31)
    active_sessions[session_id]['last_activity_at'] = old_time

    print(f"   🕐 Simulated last activity: {old_time}")

    print("\n6. Running cleanup cycle again...")
    before_expiry = len(mock_bot.sent_messages)
    await expiry_manager._cleanup_expired_sessions()
    expiry_messages = len(mock_bot.sent_messages) - before_expiry

    print(f"   📊 Active sessions: {len(active_sessions)}")
    print(f"   📊 User states: user={user_states.get(user_id)}, "
          f"heartfelt={user_states.get(heartfelt_id)}")

    expired = session_id not in active_sessions
    report(expired,
           "Session expired and cleaned up",
           f"session {session_id} is STILL in active_sessions at 31 minutes idle")
    report(expiry_messages == 2,
           "Both parties told the conversation closed",
           f"expected 2 closure messages, observed {expiry_messages}")

    states_reset = (user_states.get(user_id) == UserState.IDLE
                    and user_states.get(heartfelt_id) == UserState.IDLE)
    report(states_reset,
           "Both user states reset to IDLE",
           f"user states are user={user_states.get(user_id)}, "
           f"heartfelt={user_states.get(heartfelt_id)}, expected both IDLE")

    # Show all messages that would have been sent
    print("\n📱 Complete Message Log:")
    print("-" * 30)
    for i, msg in enumerate(mock_bot.sent_messages, 1):
        print(f"{i}. To {msg['chat_id']}: {msg['text']}")

    print("\n🎉 Demonstration complete!")
    print("Summary (each line reports what was OBSERVED, not what was intended):")
    print(f"  {'✅' if exchanged == 2 else '❌'} Session created and {exchanged} messages exchanged")
    print(f"  {'✅' if warned and warning_messages == 2 else '❌'} Warning after 25+ minutes of "
          f"inactivity: flag={warned}, messages={warning_messages}")
    print(f"  {'✅' if expired else '❌'} Session expired after 30+ minutes of inactivity: "
          f"removed from active_sessions={expired}")
    print(f"  {'✅' if expiry_messages == 2 else '❌'} Both parties notified on closure: "
          f"{expiry_messages} messages")
    print(f"  {'✅' if states_reset else '❌'} User states reset to IDLE: "
          f"user={user_states.get(user_id)}, heartfelt={user_states.get(heartfelt_id)}")


async def demonstrate_activity_reset():
    """Demonstrate that activity resets warnings"""
    print("\n" + "=" * 50)
    print("🔄 Activity Reset Demonstration")
    print("=" * 50)

    # Clear state
    active_sessions.clear()
    session_warnings.clear()
    user_states.clear()

    mock_bot = MockBot()
    session_manager = SessionManager(mock_bot)
    expiry_manager = SessionExpiryManager(mock_bot, session_manager)

    session_id = "demo-session-456"
    user_id = 11111
    heartfelt_id = 22222

    print("\n1. Creating session and simulating 26 minutes of inactivity...")
    session_manager.create_session(user_id, heartfelt_id, session_id)

    old_time = utcnow() - datetime.timedelta(minutes=26)
    active_sessions[session_id]['last_activity_at'] = old_time

    await expiry_manager._cleanup_expired_sessions()

    warned = bool(session_warnings.get(session_id))
    report(warned,
           f"Warning sent, flag set: {session_warnings.get(session_id)}",
           f"no warning flag for {session_id}: the warning never fired, so there is "
           f"nothing for the activity below to reset")

    print("\n2. User sends a message (activity detected)...")
    await session_manager.forward_message(session_id, user_id, "Sorry, I was away for a moment")

    print(f"   🕐 Activity updated: {active_sessions[session_id]['last_activity_at']}")
    flag_after_activity = session_warnings.get(session_id)
    report(not flag_after_activity,
           f"Warning flag reset: {flag_after_activity}",
           f"warning flag is still {flag_after_activity} after activity; the next "
           f"idle period would close this conversation without re-warning")

    print("\n3. Running cleanup after activity reset...")
    await expiry_manager._cleanup_expired_sessions()

    report(session_id in active_sessions,
           "Session still active -- warning properly reset!",
           f"session {session_id} was closed even though the user had just replied")

    print("\n🎉 Activity reset demonstration complete!")


if __name__ == "__main__":
    async def main():
        await demonstrate_session_expiry()
        await demonstrate_activity_reset()

    asyncio.run(main())

    print("\n" + "=" * 50)
    if _failures:
        # Still exit 0: this is a demo, not a test, and CI does not run it. But a
        # human reading the output must not be able to miss that it misbehaved.
        print(f"❌ {len(_failures)} OBSERVATION(S) DID NOT MATCH THE NARRATION:")
        for f in _failures:
            print(f"   - {f}")
        print("\nThis script asserts nothing and exits 0 regardless. If you are "
              "seeing this, run tests/test_session_expiry.py and "
              "tests/test_per_service_timers.py -- those will fail properly.")
    else:
        print("✅ Every observation matched the narration.")
    print("Reminder: this is a demonstration with zero assertions and it is not "
          "part of the CI gate.")
