#!/usr/bin/env python3
"""
Test script to verify MongoDB integration works correctly
"""

import os
import sys
import datetime
from db_manager import db_mgr

def test_fallback_mode():
    """Test that the system works without MongoDB"""
    print("🧪 Testing fallback mode (no MongoDB)...")
    
    # Temporarily disable MongoDB
    original_uri = os.environ.get('MONGODB_URI')
    if 'MONGODB_URI' in os.environ:
        del os.environ['MONGODB_URI']
    
    try:
        # Re-import to get updated config
        import importlib
        import config
        importlib.reload(config)
        
        # Try to initialize - should fail gracefully
        result = db_mgr.initialize()
        if not result:
            print("✅ Fallback mode working - database initialization failed gracefully")
        else:
            print("❌ Expected fallback mode but database connected")
        
        # Try database operations - should return None/False gracefully
        session_id = db_mgr.create_session(12345, "test description", "RHesident #1234")
        if session_id is None:
            print("✅ Database operations return None in fallback mode")
        else:
            print("❌ Expected None but got session_id")
        
    finally:
        # Restore original URI
        if original_uri:
            os.environ['MONGODB_URI'] = original_uri

def test_database_operations():
    """Test basic database operations if MongoDB is available"""
    print("\n🧪 Testing database operations...")
    
    if not db_mgr.initialize():
        print("🟡 MongoDB not available - skipping database tests")
        return
    
    print("✅ Database connection successful")
    
    # Test session creation
    print("Testing session creation...")
    session_id = db_mgr.create_session(
        user_id=12345,
        description="Test help request",
        anonymous_user_id="RHesident #1234"
    )
    
    if session_id:
        print(f"✅ Session created: {session_id}")
        
        # Test session retrieval
        session = db_mgr.get_session(session_id)
        if session and session['status'] == 'pending':
            print("✅ Session retrieved successfully")
            
            # Test session claiming
            if db_mgr.claim_session(session_id, 67890):
                print("✅ Session claimed successfully")
                
                # Test message logging
                if db_mgr.log_message(
                    session_id=session_id,
                    from_user_id=12345,
                    to_user_id=67890,
                    message_type="text",
                    content="Hello, I need help with something"
                ):
                    print("✅ Message logged successfully")
                    
                    # Test message retrieval
                    messages = db_mgr.get_session_messages(session_id)
                    if messages and len(messages) > 0:
                        print(f"✅ Retrieved {len(messages)} messages")
                        
                        # Test session ending
                        if db_mgr.end_session(session_id, 12345):
                            print("✅ Session ended successfully")
                            
                            # Verify session is ended
                            final_session = db_mgr.get_session(session_id)
                            if final_session and final_session['status'] == 'ended':
                                print("✅ Session status updated to 'ended'")
                                print(f"✅ Session duration: {final_session.get('duration_minutes', 0)} minutes")
                            else:
                                print("❌ Session status not updated properly")
                        else:
                            print("❌ Failed to end session")
                    else:
                        print("❌ Failed to retrieve messages")
                else:
                    print("❌ Failed to log message")
            else:
                print("❌ Failed to claim session")
        else:
            print("❌ Failed to retrieve session or wrong status")
    else:
        print("❌ Failed to create session")

def test_analytics_queries():
    """Test analytics queries"""
    print("\n🧪 Testing analytics queries...")
    
    if not db_mgr.db_available:
        print("🟡 Database not available - skipping analytics tests")
        return
    
    # Test session stats
    stats = db_mgr.get_session_stats()
    print(f"✅ Session stats: {stats}")
    
    # Test date range query
    end_date = datetime.datetime.utcnow()
    start_date = end_date - datetime.timedelta(days=30)
    sessions = db_mgr.get_sessions_in_date_range(start_date, end_date)
    print(f"✅ Found {len(sessions)} sessions in last 30 days")

def main():
    print("🚀 Starting MongoDB Integration Tests")
    print("=" * 50)
    
    # Test fallback mode
    test_fallback_mode()
    
    # Test database operations
    test_database_operations()
    
    # Test analytics
    test_analytics_queries()
    
    print("\n✅ All tests completed!")
    print("\n📋 Usage examples:")
    print("  python db_utils.py stats")
    print("  python db_utils.py monthly")
    print("  python db_utils.py transcript --session-id <session_id>")

if __name__ == "__main__":
    main()