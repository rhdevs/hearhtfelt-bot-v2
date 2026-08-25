#!/usr/bin/env python3
"""
MongoDB integration check. NEEDS A LIVE MONGO, AND IT WRITES DOCUMENTS.

This is the only file under tests/ that talks to a real database. It calls
create_session / claim_session / log_message / end_session against whatever
`MONGODB_URI` resolves to, so pointing it at production inserts real rows into
`sessions` and `messages`. It NEVER runs in CI and there must never be a
`MONGODB_URI` secret in the CI test job.

Because of that it refuses to run unless you opt in explicitly:

    ALLOW_DB_INTEGRATION_TEST=1 MONGODB_URI=mongodb://localhost:27017 \
        python tests/test_db_integration.py

Without the opt-in it exits NON-ZERO and connects to nothing -- a refusal must
never be mistakable for a pass.

Until this commit the file could not be run at all: it was the only test file
missing the `sys.path.insert` every other one has, so it died on
`ModuleNotFoundError: No module named 'src'` at import time. Fixing that alone
would have made a script that writes to $MONGODB_URI runnable for the first
time, so the guard below lands in the same change.
"""

import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database.manager import db_mgr
from src.timeutil import utcnow

OPT_IN_ENV = "ALLOW_DB_INTEGRATION_TEST"

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
    end_date = utcnow()
    start_date = end_date - datetime.timedelta(days=30)
    sessions = db_mgr.get_sessions_in_date_range(start_date, end_date)
    print(f"✅ Found {len(sessions)} sessions in last 30 days")

def main():
    # Opt-in guard. Nothing above this line has opened a connection: importing
    # src.database.manager only constructs the db_mgr singleton, and every
    # connection happens inside db_mgr.initialize(), which is called by the test
    # functions below.
    if os.environ.get(OPT_IN_ENV) != "1":
        print("❌ REFUSING TO RUN.")
        print()
        print("  This script is not a unit test. It connects to whatever MONGODB_URI")
        print("  resolves to and WRITES DOCUMENTS: it creates a session, claims it,")
        print("  logs a message and ends it, in the `sessions` and `messages`")
        print("  collections. Aimed at production it inserts real rows into a live")
        print("  mental-health helpline's database.")
        print()
        print(f"  Set {OPT_IN_ENV}=1 to confirm you know that, and point")
        print("  MONGODB_URI at a scratch database -- never at production:")
        print()
        print(f"    {OPT_IN_ENV}=1 MONGODB_URI=mongodb://localhost:27017 \\")
        print("        python tests/test_db_integration.py")
        print()
        print("  No connection was made. Exiting non-zero so this refusal cannot be")
        print("  mistaken for a pass.")
        return 2

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
    return 0

if __name__ == "__main__":
    sys.exit(main())