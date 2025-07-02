#!/usr/bin/env python3
"""
Manual test script to demonstrate session expiry functionality
This script simulates the behavior without requiring a real Telegram bot
"""

import asyncio
import datetime
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.bot.managers.session import SessionManager
from src.bot.managers.expiry import SessionExpiryManager
from config import active_sessions, session_warnings, user_states, UserState
from unittest.mock import AsyncMock

class MockBot:
    """Mock bot for manual testing"""
    
    def __init__(self):
        self.sent_messages = []
    
    async def send_message(self, chat_id, text):
        self.sent_messages.append({
            'chat_id': chat_id,
            'text': text,
            'timestamp': datetime.datetime.now()
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
    
    # Create mock bot and managers
    mock_bot = MockBot()
    session_manager = SessionManager(mock_bot)
    expiry_manager = SessionExpiryManager(mock_bot, session_manager)
    
    # Create test session
    session_id = "demo-session-123"
    user_id = 12345
    heartfelt_id = 67890
    
    print(f"\n1. Creating session {session_id[:8]}...")
    session_manager.create_session(user_id, heartfelt_id, session_id)
    user_states[user_id] = UserState.IN_CONVERSATION
    user_states[heartfelt_id] = UserState.IN_CONVERSATION
    
    print(f"   ✅ Session created with users {user_id} and {heartfelt_id}")
    print(f"   📊 Active sessions: {len(active_sessions)}")
    
    # Simulate message exchange
    print(f"\n2. Simulating message exchange...")
    await session_manager.forward_message(session_id, user_id, "Hello, I need help")
    await session_manager.forward_message(session_id, heartfelt_id, "Hi! How can I assist you?")
    
    print(f"   ✅ Messages exchanged, activity updated")
    print(f"   🕐 Last activity: {active_sessions[session_id]['last_activity_at']}")
    
    # Fast-forward time to trigger warning (simulate 6 minutes of inactivity)
    print(f"\n3. Fast-forwarding 6 minutes (warning threshold)...")
    old_time = datetime.datetime.now() - datetime.timedelta(minutes=6)
    active_sessions[session_id]['last_activity_at'] = old_time
    
    print(f"   🕐 Simulated last activity: {old_time}")
    
    # Check for warnings
    print(f"\n4. Running cleanup cycle...")
    await expiry_manager._cleanup_expired_sessions()
    
    print(f"   📊 Warning flags: {session_warnings}")
    print(f"   📱 Messages sent: {len(mock_bot.sent_messages)}")
    
    if session_warnings.get(session_id):
        print("   ✅ Warning successfully sent to both parties")
    
    # Fast-forward time to trigger expiry (simulate 11 minutes total)
    print(f"\n5. Fast-forwarding 5 more minutes (expiry threshold)...")
    old_time = datetime.datetime.now() - datetime.timedelta(minutes=11)
    active_sessions[session_id]['last_activity_at'] = old_time
    
    print(f"   🕐 Simulated last activity: {old_time}")
    
    # Check for expiry
    print(f"\n6. Running cleanup cycle again...")
    await expiry_manager._cleanup_expired_sessions()
    
    print(f"   📊 Active sessions: {len(active_sessions)}")
    print(f"   📊 User states: user={user_states.get(user_id)}, heartfelt={user_states.get(heartfelt_id)}")
    
    if session_id not in active_sessions:
        print("   ✅ Session successfully expired and cleaned up")
    
    # Show all messages that would have been sent
    print(f"\n📱 Complete Message Log:")
    print("-" * 30)
    for i, msg in enumerate(mock_bot.sent_messages, 1):
        print(f"{i}. To {msg['chat_id']}: {msg['text']}")
    
    print(f"\n🎉 Demonstration complete!")
    print(f"Summary:")
    print(f"  - Session created and messages exchanged")
    print(f"  - Warning sent after 5+ minutes of inactivity") 
    print(f"  - Session expired after 10+ minutes of inactivity")
    print(f"  - Both parties notified appropriately")
    print(f"  - User states reset to IDLE")

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
    
    # Create session
    session_id = "demo-session-456"
    user_id = 11111
    heartfelt_id = 22222
    
    print(f"\n1. Creating session and simulating 6 minutes of inactivity...")
    session_manager.create_session(user_id, heartfelt_id, session_id)
    
    # Simulate inactivity to trigger warning
    old_time = datetime.datetime.now() - datetime.timedelta(minutes=6)
    active_sessions[session_id]['last_activity_at'] = old_time
    
    # Run cleanup to send warning
    await expiry_manager._cleanup_expired_sessions()
    
    print(f"   ✅ Warning sent, flag set: {session_warnings.get(session_id)}")
    
    # Simulate new activity
    print(f"\n2. User sends a message (activity detected)...")
    await session_manager.forward_message(session_id, user_id, "Sorry, I was away for a moment")
    
    print(f"   🕐 Activity updated: {active_sessions[session_id]['last_activity_at']}")
    print(f"   🔄 Warning flag reset: {session_warnings.get(session_id)}")
    
    # Wait and check - should not expire now
    print(f"\n3. Running cleanup after activity reset...")
    await expiry_manager._cleanup_expired_sessions()
    
    if session_id in active_sessions:
        print("   ✅ Session still active - warning properly reset!")
    
    print(f"\n🎉 Activity reset demonstration complete!")

if __name__ == "__main__":
    async def main():
        await demonstrate_session_expiry()
        await demonstrate_activity_reset()
    
    asyncio.run(main())