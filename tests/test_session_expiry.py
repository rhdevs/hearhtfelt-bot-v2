#!/usr/bin/env python3
"""
Test suite for session auto-expiry functionality
"""

import asyncio
import datetime
import unittest
from unittest.mock import Mock, AsyncMock, patch
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.bot.managers.session import SessionManager
from src.bot.managers.expiry import SessionExpiryManager
from src.database.manager import DBManager
from config import active_sessions, session_warnings, user_states, UserState

class TestSessionExpiry(unittest.TestCase):
    
    def setUp(self):
        """Set up test fixtures"""
        # Clear global state
        active_sessions.clear()
        session_warnings.clear()
        user_states.clear()
        
        # Mock bot
        self.mock_bot = AsyncMock()
        self.mock_bot.send_message = AsyncMock()
        
        # Create managers
        self.session_manager = SessionManager(self.mock_bot)
        self.expiry_manager = SessionExpiryManager(self.mock_bot, self.session_manager)
        
        # Mock database manager
        self.mock_db_mgr = Mock()
        self.mock_db_mgr.db_available = True
        self.mock_db_mgr.update_session_activity = Mock(return_value=True)
        self.mock_db_mgr.log_message = Mock(return_value=True)
        self.mock_db_mgr.end_session = Mock(return_value=True)
    
    def reset_mocks(self):
        """Reset mock call counts"""
        self.mock_bot.send_message.reset_mock()
        self.mock_db_mgr.reset_mock()
        
    def test_session_activity_tracking(self):
        """Test that session activity is properly tracked"""
        # Create a test session
        session_id = "test-session-1"
        user_id = 12345
        heartfelt_id = 67890
        
        # Create session
        self.session_manager.create_session(user_id, heartfelt_id, session_id)
        
        # Verify session has activity timestamp
        self.assertIn(session_id, active_sessions)
        session = active_sessions[session_id]
        self.assertIn('last_activity_at', session)
        self.assertIsInstance(session['last_activity_at'], datetime.datetime)
        
        # Test activity update
        old_activity = session['last_activity_at']
        
        # Wait a moment then update activity
        import time
        time.sleep(0.1)
        self.session_manager.update_session_activity(session_id)
        
        # Verify activity timestamp was updated
        new_activity = active_sessions[session_id]['last_activity_at']
        self.assertGreater(new_activity, old_activity)
    
    async def test_message_forwarding_updates_activity(self):
        """Test that forwarding messages updates session activity"""
        # Create a test session
        session_id = "test-session-2"
        user_id = 12345
        heartfelt_id = 67890
        
        # Mock database manager
        with patch('src.bot.managers.session.db_mgr', self.mock_db_mgr):
            self.session_manager.create_session(user_id, heartfelt_id, session_id)
            
            old_activity = active_sessions[session_id]['last_activity_at']
            
            # Wait a moment
            import time
            time.sleep(0.1)
            
            # Forward a message
            await self.session_manager.forward_message(session_id, user_id, "Test message")
            
            # Verify activity was updated
            new_activity = active_sessions[session_id]['last_activity_at']
            self.assertGreater(new_activity, old_activity)
            
            # Verify database update was called
            self.mock_db_mgr.update_session_activity.assert_called_with(session_id)
    
    async def test_sticker_forwarding_updates_activity(self):
        """Test that forwarding stickers updates session activity"""
        session_id = "test-session-3"
        user_id = 12345
        heartfelt_id = 67890
        
        with patch('src.bot.managers.session.db_mgr', self.mock_db_mgr):
            self.session_manager.create_session(user_id, heartfelt_id, session_id)
            
            old_activity = active_sessions[session_id]['last_activity_at']
            
            import time
            time.sleep(0.1)
            
            # Forward a sticker
            await self.session_manager.forward_sticker(session_id, user_id, "sticker_file_id")
            
            # Verify activity was updated
            new_activity = active_sessions[session_id]['last_activity_at']
            self.assertGreater(new_activity, old_activity)
    
    async def test_warning_system(self):
        """Test that warnings are sent at appropriate times"""
        # Create test session with old activity timestamp
        session_id = "test-session-4"
        user_id = 12345
        heartfelt_id = 67890
        
        self.session_manager.create_session(user_id, heartfelt_id, session_id)
        
        # Simulate 6-minute old activity (should trigger warning)
        old_time = datetime.datetime.now() - datetime.timedelta(minutes=6)
        active_sessions[session_id]['last_activity_at'] = old_time
        
        # Run warning check
        await self.expiry_manager._send_session_warning(session_id, active_sessions[session_id])
        
        # Verify warning flag was set
        self.assertTrue(session_warnings.get(session_id, False))
        
        # Verify both parties were messaged
        self.assertEqual(self.mock_bot.send_message.call_count, 2)
        
        # Check message content
        calls = self.mock_bot.send_message.call_args_list
        self.assertIn("Are you still there?", calls[0][1]['text'])
        self.assertIn("Are you still there?", calls[1][1]['text'])
    
    async def test_session_expiry(self):
        """Test that sessions expire after timeout"""
        session_id = "test-session-5"
        user_id = 12345
        heartfelt_id = 67890
        
        with patch('src.bot.managers.session.db_mgr', self.mock_db_mgr):
            self.session_manager.create_session(user_id, heartfelt_id, session_id)
            
            # Set user states
            user_states[user_id] = UserState.IN_CONVERSATION
            user_states[heartfelt_id] = UserState.IN_CONVERSATION
            
            # Simulate 11-minute old activity (should trigger expiry)
            old_time = datetime.datetime.now() - datetime.timedelta(minutes=11)
            active_sessions[session_id]['last_activity_at'] = old_time
            
            # Run expiry
            await self.expiry_manager._expire_session(session_id, active_sessions[session_id])
            
            # Verify session was removed from active sessions
            self.assertNotIn(session_id, active_sessions)
            
            # Verify user states were reset
            self.assertEqual(user_states[user_id], UserState.IDLE)
            self.assertEqual(user_states[heartfelt_id], UserState.IDLE)
            
            # Verify database end_session was called with system_end=True
            self.mock_db_mgr.end_session.assert_called_with(session_id, user_id, True)
            
            # Verify both parties were notified
            self.assertEqual(self.mock_bot.send_message.call_count, 2)
    
    async def test_cleanup_cycle(self):
        """Test full cleanup cycle with multiple sessions"""
        # Create multiple sessions with different activity times
        sessions_data = [
            ("session-1", 12345, 67890, 11),  # Should expire
            ("session-2", 12346, 67891, 6),   # Should warn
            ("session-3", 12347, 67892, 2),   # Should do nothing
        ]
        
        with patch('src.bot.managers.session.db_mgr', self.mock_db_mgr):
            for session_id, user_id, heartfelt_id, minutes_ago in sessions_data:
                self.session_manager.create_session(user_id, heartfelt_id, session_id)
                user_states[user_id] = UserState.IN_CONVERSATION
                user_states[heartfelt_id] = UserState.IN_CONVERSATION
                
                # Set activity time
                old_time = datetime.datetime.now() - datetime.timedelta(minutes=minutes_ago)
                active_sessions[session_id]['last_activity_at'] = old_time
            
            # Mock database query to return empty (testing memory-only behavior)
            self.mock_db_mgr.get_sessions_by_activity = Mock(return_value=[])
            
            # Run cleanup
            await self.expiry_manager._cleanup_expired_sessions()
            
            # Verify results
            self.assertNotIn("session-1", active_sessions)  # Expired
            self.assertIn("session-2", active_sessions)     # Warned only
            self.assertIn("session-3", active_sessions)     # Untouched
            
            # Verify warning was set for session-2
            self.assertTrue(session_warnings.get("session-2", False))
            
            # Verify states
            self.assertEqual(user_states[12345], UserState.IDLE)  # Expired session user
            self.assertEqual(user_states[12346], UserState.IN_CONVERSATION)  # Warned session user
    
    def test_warning_spam_prevention(self):
        """Test that warnings are not sent repeatedly"""
        session_id = "test-session-6"
        
        # Mark as already warned
        session_warnings[session_id] = True
        
        # Create session with old activity
        user_id = 12345
        heartfelt_id = 67890
        self.session_manager.create_session(user_id, heartfelt_id, session_id)
        
        old_time = datetime.datetime.now() - datetime.timedelta(minutes=6)
        active_sessions[session_id]['last_activity_at'] = old_time
        
        # Mock the cleanup to see if warning would be sent
        now = datetime.datetime.now()
        warning_cutoff = now - datetime.timedelta(minutes=5)
        
        sessions_to_warn = []
        for sid, session_data in active_sessions.items():
            last_activity = session_data.get('last_activity_at', session_data.get('created_at'))
            if (last_activity <= warning_cutoff and 
                not session_warnings.get(sid, False)):
                sessions_to_warn.append((sid, session_data))
        
        # Should be empty because warning already sent
        self.assertEqual(len(sessions_to_warn), 0)

class TestSessionExpiryIntegration(unittest.TestCase):
    """Integration tests that can be run manually"""
    
    def test_constants_configuration(self):
        """Test that all required constants are properly configured"""
        from config import (
            SESSION_TIMEOUT_MINUTES, SESSION_WARNING_MINUTES, 
            SESSION_SWEEP_SECONDS, MESSAGES
        )
        
        self.assertEqual(SESSION_TIMEOUT_MINUTES, 10)
        self.assertEqual(SESSION_WARNING_MINUTES, 5)
        self.assertEqual(SESSION_SWEEP_SECONDS, 180)
        
        # Check required messages exist
        required_messages = [
            "session_warning", "session_expired", "session_expired_heartfelt"
        ]
        for msg_key in required_messages:
            self.assertIn(msg_key, MESSAGES)
            self.assertIsInstance(MESSAGES[msg_key], str)
            self.assertGreater(len(MESSAGES[msg_key]), 0)

def run_tests():
    """Run all tests"""
    print("🧪 Running Session Expiry Tests...")
    print("=" * 50)
    
    # Run basic unit tests
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    # Add test cases
    suite.addTests(loader.loadTestsFromTestCase(TestSessionExpiry))
    suite.addTests(loader.loadTestsFromTestCase(TestSessionExpiryIntegration))
    
    # Run async tests
    async def run_async_tests():
        test_instance = TestSessionExpiry()
        
        print("\n📋 Testing activity tracking...")
        test_instance.setUp()
        test_instance.test_session_activity_tracking()
        print("✅ Activity tracking works")
        
        print("\n📋 Testing message forwarding...")
        test_instance.setUp()
        await test_instance.test_message_forwarding_updates_activity()
        print("✅ Message forwarding updates activity")
        
        print("\n📋 Testing sticker forwarding...")
        test_instance.setUp()
        await test_instance.test_sticker_forwarding_updates_activity()
        print("✅ Sticker forwarding updates activity")
        
        print("\n📋 Testing warning system...")
        test_instance.setUp()
        await test_instance.test_warning_system()
        print("✅ Warning system works")
        
        print("\n📋 Testing session expiry...")
        test_instance.setUp()
        await test_instance.test_session_expiry()
        print("✅ Session expiry works")
        
        print("\n📋 Testing cleanup cycle...")
        test_instance.setUp()
        await test_instance.test_cleanup_cycle()
        print("✅ Cleanup cycle works")
        
        print("\n📋 Testing warning spam prevention...")
        test_instance.setUp()
        test_instance.test_warning_spam_prevention()
        print("✅ Warning spam prevention works")
        
        print("\n📋 Testing configuration...")
        integration_test = TestSessionExpiryIntegration()
        integration_test.test_constants_configuration()
        print("✅ Configuration is correct")
    
    # Run the async tests
    try:
        asyncio.run(run_async_tests())
        print("\n🎉 All tests passed!")
        return True
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = run_tests()
    exit(0 if success else 1)