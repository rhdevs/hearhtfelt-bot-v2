#!/usr/bin/env python3
"""
Database utility functions for querying MongoDB data
Usage: python db_utils.py [command]
"""

import datetime
import argparse
from src.database.manager import db_mgr
from config import DEFAULT_HEARTFELT_MEMBERS

def get_anonymous_name(session_doc, for_user_type='user'):
    """Helper to get anonymous display name from session document"""
    if for_user_type == 'user':
        return "Hearhtfelt Member"
    else:
        return session_doc.get('anonymous_user_id', 'Anonymous User')

def format_session_transcript(session_id):
    """Format and display session transcript"""
    if not db_mgr.initialize():
        print("❌ Database not available")
        return
    
    # Get session info
    session = db_mgr.get_session(session_id)
    if not session:
        print(f"❌ Session {session_id} not found")
        return
    
    # Get messages
    messages = db_mgr.get_session_messages(session_id)
    
    print(f"\n📋 Session Transcript: {session_id}")
    print(f"Status: {session['status']}")
    print(f"User ID: {session['user_id']}")
    print(f"Heartfelt Member ID: {session.get('heartfelt_member_id', 'N/A')}")
    print(f"Created: {session['created_at']}")
    if session.get('claimed_at'):
        print(f"Claimed: {session['claimed_at']}")
    if session.get('ended_at'):
        print(f"Ended: {session['ended_at']}")
        print(f"Duration: {session.get('duration_minutes', 'N/A')} minutes")
    print(f"Description: {session.get('description', 'N/A')}")
    print("\n💬 Messages:")
    print("-" * 60)
    
    if not messages:
        print("No messages found.")
        return
    
    for msg in messages:
        timestamp = msg['timestamp'].strftime('%H:%M:%S')
        
        # Determine sender display name
        if msg['from_user_id'] == session['user_id']:
            sender = get_anonymous_name(session, 'member')  # User sees "HeaRHtfelt Member"
        else:
            sender = get_anonymous_name(session, 'user')    # Member sees "RHesident #1234"
        
        if msg['message_type'] == 'text':
            print(f"[{timestamp}] {sender}: {msg['content']}")
        elif msg['message_type'] == 'file':
            file_type = msg.get('file_type', 'file')
            print(f"[{timestamp}] {sender}: *sent a {file_type}*")

def show_sessions_this_month():
    """Show all sessions from this month"""
    if not db_mgr.initialize():
        print("❌ Database not available")
        return
    
    # Get start of current month
    now = datetime.datetime.utcnow()
    start_of_month = datetime.datetime(now.year, now.month, 1)
    
    sessions = db_mgr.get_sessions_in_date_range(start_of_month, now)
    
    print(f"\n📅 Sessions This Month ({now.strftime('%B %Y')})")
    print("-" * 60)
    
    if not sessions:
        print("No sessions found this month.")
        return
    
    for session in sessions:
        status_emoji = {"pending": "⏳", "active": "🟢", "ended": "✅"}.get(session['status'], "❓")
        created = session['created_at'].strftime('%m/%d %H:%M')
        duration = f"{session.get('duration_minutes', 0)}m" if session['status'] == 'ended' else 'N/A'
        
        print(f"{status_emoji} {session['session_id'][:8]}... | {created} | {duration} | {session['status']}")

def show_session_stats():
    """Show session statistics"""
    if not db_mgr.initialize():
        print("❌ Database not available")
        return
    
    stats = db_mgr.get_session_stats()
    
    print("\n📊 Session Statistics")
    print("-" * 30)
    
    if not stats:
        print("No session data found.")
        return
    
    total = stats.get('total', 0)
    pending = stats.get('pending', 0)
    active = stats.get('active', 0)
    ended = stats.get('ended', 0)
    
    print(f"Total Sessions: {total}")
    print(f"Pending: {pending}")
    print(f"Active: {active}")
    print(f"Completed: {ended}")
    
    if total > 0:
        completion_rate = (ended / total) * 100
        print(f"Completion Rate: {completion_rate:.1f}%")

def manage_authorized_members(action: str, telegram_id: int = None, username: str = None, include_inactive: bool = False):
    """CLI helper to manage authorized heartfelt members."""
    if not db_mgr.initialize():
        print("❌ Database not available")
        return

    db_mgr.ensure_authorized_members_seed(DEFAULT_HEARTFELT_MEMBERS)

    if action == 'list':
        records = db_mgr.get_authorized_member_records(include_inactive=include_inactive)
        if not records:
            print("No authorized heartfelt members found.")
            return

        print("\n💚 Authorized Heartfelt Members")
        print("-" * 40)
        for doc in records:
            member_id = doc.get('telegram_id')
            status = 'active' if doc.get('active', True) else 'inactive'
            username_display = doc.get('username') or '—'
            print(f"{member_id}: {status} (username: {username_display})")
        return

    if telegram_id is None:
        print("❌ --telegram-id is required for this action")
        return

    if action == 'add':
        success = db_mgr.add_authorized_member(telegram_id, username=username, active=True)
        if success:
            print(f"✅ Added/updated heartfelt member {telegram_id}")
        else:
            print(f"❌ Failed to add heartfelt member {telegram_id}")
    elif action == 'deactivate':
        success = db_mgr.deactivate_authorized_member(telegram_id)
        if success:
            print(f"✅ Deactivated heartfelt member {telegram_id}")
        else:
            print(f"❌ Failed to deactivate heartfelt member {telegram_id}")
    elif action == 'remove':
        success = db_mgr.remove_authorized_member(telegram_id)
        if success:
            print(f"✅ Removed heartfelt member {telegram_id}")
        else:
            print(f"❌ Failed to remove heartfelt member {telegram_id}")
    else:
        print(f"❌ Unsupported action: {action}")


def main():
    parser = argparse.ArgumentParser(description='Database utility for Heartfelt Bot')
    parser.add_argument('command', choices=['transcript', 'monthly', 'stats', 'admins'],
                       help='Command to execute')
    parser.add_argument('--session-id', help='Session ID for transcript command')
    parser.add_argument('--action', choices=['list', 'add', 'deactivate', 'remove'],
                        help='Action for admins command')
    parser.add_argument('--telegram-id', type=int, help='Telegram ID for admins command')
    parser.add_argument('--username', help='Optional username when adding an admin')
    parser.add_argument('--include-inactive', action='store_true',
                        help='Include inactive members when listing admins')

    args = parser.parse_args()

    if args.command == 'transcript':
        if not args.session_id:
            print("❌ --session-id required for transcript command")
            return
        format_session_transcript(args.session_id)
    elif args.command == 'monthly':
        show_sessions_this_month()
    elif args.command == 'stats':
        show_session_stats()
    elif args.command == 'admins':
        if not args.action:
            print("❌ --action required for admins command")
            return
        manage_authorized_members(
            action=args.action,
            telegram_id=args.telegram_id,
            username=args.username,
            include_inactive=args.include_inactive,
        )

if __name__ == "__main__":
    main()