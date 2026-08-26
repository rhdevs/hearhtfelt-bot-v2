#!/usr/bin/env python3
"""
Database utility functions for querying MongoDB data
Usage: python db_utils.py [command]
"""

import datetime
import argparse
from typing import Optional
from src.database.manager import db_mgr
from src.timeutil import UTC, ensure_aware_utc, utcnow
from config import get_service

def get_anonymous_name(session_doc, for_user_type='user'):
    """Helper to get anonymous display name from session document"""
    if for_user_type == 'user':
        return get_service(session_doc.get('service')).member_label
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
        # Legacy transcripts predate several schema additions, and a document can
        # reach here with no timestamp at all. A bare msg['timestamp'].strftime()
        # raises KeyError/AttributeError and kills the whole transcript dump.
        ts = ensure_aware_utc(msg.get('timestamp'))
        timestamp = ts.strftime('%H:%M:%S') if ts else '--:--:--'
        
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
    now = utcnow()
    start_of_month = datetime.datetime(now.year, now.month, 1, tzinfo=UTC)
    
    sessions = db_mgr.get_sessions_in_date_range(start_of_month, now)
    
    print(f"\n📅 Sessions This Month ({now.strftime('%B %Y')})")
    print("-" * 60)
    
    if not sessions:
        print("No sessions found this month.")
        return
    
    for session in sessions:
        status_emoji = {"pending": "⏳", "active": "🟢", "ended": "✅"}.get(session['status'], "❓")
        created_dt = ensure_aware_utc(session.get('created_at'))
        created = created_dt.strftime('%m/%d %H:%M') if created_dt else '--/-- --:--'
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

def manage_authorized_members(action: str, telegram_id: Optional[int] = None,
                              username: Optional[str] = None,
                              include_inactive: bool = False, service: str = 'hf',
                              display_name: Optional[str] = None,
                              blurb: Optional[str] = None):
    """CLI helper to manage authorized members for a given service (hf/pss)."""
    if not db_mgr.initialize():
        print("❌ Database not available")
        return

    svc = get_service(service)
    collection = svc.members_collection
    label = svc.member_label

    if svc.default_members:
        db_mgr.ensure_authorized_members_seed(svc.default_members, collection=collection)

    if action == 'list':
        records = db_mgr.get_authorized_member_records(include_inactive=include_inactive, collection=collection)
        if not records:
            print(f"No authorized members found for service '{svc.key}'.")
            return

        print(f"\n💚 Authorized Members — {label} ({svc.key})")
        print("-" * 40)
        for doc in records:
            member_id = doc.get('telegram_id')
            status = 'active' if doc.get('active', True) else 'inactive'
            # available absent means available -- do not invert this.
            avail = 'available' if doc.get('available', True) else 'unavailable'
            started = 'started' if doc.get('has_started_bot') else 'NOT SET'
            display_name_val = doc.get('display_name') or '-'
            username_val = doc.get('username')
            # username_display already carries its own '@' (or is a bare '-' when
            # absent), so it is not preceded by a separate literal '@' below --
            # that avoids both double-'@' on stored "@handle" values and a stray
            # '@' when there is no username at all.
            if username_val:
                username_display = username_val if str(username_val).startswith('@') else f"@{username_val}"
            else:
                username_display = '-'
            print(f"{member_id}: {status:8} | {avail:11} | {started:7} | {display_name_val} | {username_display}")
            blurb_val = doc.get('blurb')
            if blurb_val:
                print(f"   {blurb_val}")
        return

    if telegram_id is None:
        print("❌ --telegram-id is required for this action")
        return

    if action == 'add':
        success = db_mgr.add_authorized_member(telegram_id, username=username, active=True, collection=collection)
        if success:
            print(f"✅ Added/updated {label} {telegram_id}")
        else:
            print(f"❌ Failed to add {label} {telegram_id}")
    elif action == 'deactivate':
        success = db_mgr.deactivate_authorized_member(telegram_id, collection=collection)
        if success:
            print(f"✅ Deactivated {label} {telegram_id}")
        else:
            print(f"❌ Failed to deactivate {label} {telegram_id}")
    elif action == 'remove':
        success = db_mgr.remove_authorized_member(telegram_id, collection=collection)
        if success:
            print(f"✅ Removed {label} {telegram_id}")
        else:
            print(f"❌ Failed to remove {label} {telegram_id}")
    elif action == 'available':
        success = db_mgr.set_member_availability(telegram_id, True, collection=collection)
        if success:
            print(f"✅ Marked {label} {telegram_id} as available")
        else:
            print(f"❌ Failed to mark {label} {telegram_id} as available")
    elif action == 'unavailable':
        success = db_mgr.set_member_availability(telegram_id, False, collection=collection)
        if success:
            print(f"✅ Marked {label} {telegram_id} as unavailable")
        else:
            print(f"❌ Failed to mark {label} {telegram_id} as unavailable")
    elif action == 'set-profile':
        if display_name is None and blurb is None:
            print("❌ --display-name or --blurb is required for set-profile")
            return
        success = db_mgr.set_member_profile(telegram_id, collection=collection, display_name=display_name, blurb=blurb)
        if success:
            print(f"✅ Updated profile for {label} {telegram_id}")
            doc = db_mgr.get_member_profile_doc(telegram_id, collection=collection)
            if doc:
                print(f"   telegram_id:    {doc.get('telegram_id')}")
                print(f"   display_name:   {doc.get('display_name') or '-'}")
                print(f"   blurb:          {doc.get('blurb') or '-'}")
                print(f"   available:      {doc.get('available', True)}")
                print(f"   has_started_bot: {doc.get('has_started_bot', False)}")
                print(f"   active:         {doc.get('active', True)}")
        else:
            print(f"❌ Failed to update profile for {label} {telegram_id}")
    elif action == 'set-started':
        print("⚠️  This claims the supporter has an open chat with the bot. If they "
              "don't, a directed request will fail and the requester will be asked "
              "to choose again. Prefer asking them to press /start.")
        success = db_mgr.mark_member_started(telegram_id, True, collection=collection)
        if success:
            print(f"✅ Marked {label} {telegram_id} as started")
        else:
            print(f"❌ Failed to mark {label} {telegram_id} as started")
    elif action == 'clear-started':
        success = db_mgr.mark_member_started(telegram_id, False, collection=collection)
        if success:
            print(f"✅ Cleared started flag for {label} {telegram_id}")
        else:
            print(f"❌ Failed to clear started flag for {label} {telegram_id}")
    else:
        print(f"❌ Unsupported action: {action}")


def manage_registrations(action: str, registration_id: Optional[str] = None,
                         status: str = 'pending', limit: int = 50):
    """CLI helper to inspect/close supporter registrations.

    Deliberately has no 'approve' action. Approval has to go through
    decide_registration's atomic {'status': 'pending'} gate and the applicant
    notification, both of which live in the bot process, not this CLI. A CLI
    approve would bypass both: nobody would ever tell the applicant, and the
    row would be left carrying live Approve/Reject buttons in two admins' chats
    pointing at an already-settled decision. (Those buttons stay SAFE -- the
    gate refuses them -- but an admin tapping one would be told "already
    handled" with no idea why.) The supported out-of-band path is
    `admins --action add` to grant the roster slot directly, followed by
    `registrations --action close` to retire the now-redundant pending row.
    """
    if not db_mgr.initialize():
        print("❌ Database not available")
        return

    if action == 'list':
        records = db_mgr.list_registrations(status=status, limit=limit)
        if not records:
            print(f"No registrations found for status '{status}'.")
            return

        print(f"\n📝 Registrations — status={status}")
        print("-" * 40)
        for doc in records:
            # A single malformed created_at must not kill the whole dump --
            # same fallback pattern as show_sessions_this_month.
            created_dt = ensure_aware_utc(doc.get('created_at'))
            created = created_dt.strftime('%m/%d %H:%M') if created_dt else '--/-- --:--'
            row_status = doc.get('status') or '-'
            telegram_id = doc.get('telegram_id')
            name = ' '.join(part for part in [doc.get('first_name'), doc.get('last_name')] if part).strip() or '-'
            username_val = doc.get('username')
            # Same '@'-normalisation as manage_authorized_members: username_display
            # already carries its own '@' (or is a bare '-'), so it is not preceded
            # by a separate literal '@' in the f-string below.
            if username_val:
                username_display = username_val if str(username_val).startswith('@') else f"@{username_val}"
            else:
                username_display = '-'
            decided_by = doc.get('decided_by') or '-'
            registration_id_val = doc.get('registration_id')
            print(f"{created} | {row_status:8} | {telegram_id} | {name} | {username_display} | {decided_by} | {registration_id_val}")
        return

    if action == 'close':
        if registration_id is None:
            print("❌ --registration-id is required for this action")
            return
        result = db_mgr.close_registration(registration_id, 'closed_by_admin')
        if result:
            print(f"✅ Closed registration {registration_id}")
        else:
            print(f"❌ Failed to close registration {registration_id}")
    else:
        print(f"❌ Unsupported action: {action}")


def main():
    parser = argparse.ArgumentParser(description='Database utility for Heartfelt Bot')
    parser.add_argument('command', choices=['transcript', 'monthly', 'stats', 'admins', 'registrations'],
                       help='Command to execute')
    parser.add_argument('--session-id', help='Session ID for transcript command')
    parser.add_argument('--action', choices=['list', 'add', 'deactivate', 'remove',
                                              'available', 'unavailable',
                                              'set-profile', 'set-started', 'clear-started',
                                              'close'],
                        help='Action for admins/registrations command')
    parser.add_argument('--telegram-id', type=int, help='Telegram ID for admins command')
    parser.add_argument('--username', help='Optional username when adding an admin')
    parser.add_argument('--include-inactive', action='store_true',
                        help='Include inactive members when listing admins')
    parser.add_argument('--service', choices=['hf', 'pss'], default='hf',
                        help='Which service roster to manage (default: hf)')
    parser.add_argument('--display-name', help='Picker display name to set for set-profile')
    parser.add_argument('--blurb', help='One-line free-text blurb to set for set-profile')
    parser.add_argument('--registration-id', help='Registration ID for registrations command')
    parser.add_argument('--status', choices=['pending', 'approved', 'rejected', 'closed'], default='pending',
                        help='Status filter for registrations --action list (default: pending)')

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
            service=args.service,
            display_name=args.display_name,
            blurb=args.blurb,
        )
    elif args.command == 'registrations':
        if not args.action:
            print("❌ --action required for registrations command")
            return
        manage_registrations(
            action=args.action,
            registration_id=args.registration_id,
            status=args.status,
        )

if __name__ == "__main__":
    main()