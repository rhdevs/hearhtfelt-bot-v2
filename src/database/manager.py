import datetime
import uuid
import logging
from typing import Iterable, Optional, List, Dict, Any
from pymongo import ReturnDocument
from src.database.connection import db_manager
from src.timeutil import ensure_aware_utc, utcnow

logger = logging.getLogger(__name__)

class DBManager:
    def __init__(self):
        self.db_available = False
        self._authorized_collection = 'heartfelt_members'
        
    def initialize(self):
        """Initialize database connection"""
        self.db_available = db_manager.connect()
        return self.db_available
    
    # SESSION MANAGEMENT
    
    def create_session(self, user_id: int, description: str, anonymous_user_id: str, session_id: str = None, user_telehandle: str = None, service: str = "hf", routing: str = "open") -> Optional[str]:
        """Create a new session in pending state.

        `status` deliberately keeps its three values ('pending' / 'active' / 'ended'),
        so every existing atomic gate -- claim_session, end_session,
        get_pending_sessions -- keeps working with no widened filters. The new
        `routing` field carries the directed-support lane instead. A fourth status
        would have meant editing every filter in this file, which is exactly the
        class of change where one gets missed. See D17.
        """
        if not self.db_available:
            return None

        try:
            if session_id is None:
                session_id = str(uuid.uuid4())

            now = utcnow()
            session_doc = {
                'session_id': session_id,
                'service': service,
                'user_telehandle': user_telehandle,
                'user_id': user_id,
                'heartfelt_member_telehandle': None, # null until claimed
                'heartfelt_member_id': None,  # null until claimed
                'anonymous_user_id': anonymous_user_id,
                'status': 'pending',
                'description': description,
                'created_at': now,
                'last_activity_at': now,  # Track activity for auto-expiry
                'claimed_at': None,
                'ended_at': None,
                'ended_by_user_id': None,
                # Where this request's channel post lives, so a restarted bot can
                # still edit or delete it. Filled in by set_queue_message.
                'queue_channel_id': None,
                'queue_message_id': None,

                # --- directed-support lane (Phase 5). All ADDITIVE: a legacy
                # document with none of these reads as routing 'open', which is
                # today's behaviour exactly.
                'routing': routing,
                # A SECOND clock. created_at stays immutable (audit, and the
                # channel post's "Requested:" line); waiting_since is the
                # requester's WAIT BUDGET and restarts whenever the decision is
                # handed back to them. Without it a request that lapses at 23h59m
                # would get "they're not free, pick again" and "your request
                # expired" one sweep apart. The open lane NEVER resets it, so
                # open-queue timing is byte-identical to before. See D18.
                'waiting_since': now,
                'target_member_id': None,
                'directed_at': None,
                'directed_message_id': None,
                'notice_channel_id': None,
                'notice_message_id': None,
                # Everyone who has declined, let this lapse, or released it. The
                # picker never re-offers them.
                'declined_by': [],
            }
            
            db_manager.db.sessions.insert_one(session_doc)
            logger.info(f"Created session {session_id} for user {user_id}")
            return session_id
            
        except Exception as e:
            logger.error(f"Error creating session: {e}")
            return None
    
    def claim_session(self, session_id: str, heartfelt_member_id: int, heartfelt_member_telehandle: str = None) -> bool:
        """Claim a pending session and activate it"""
        if not self.db_available:
            return False
            
        try:
            result = db_manager.db.sessions.update_one(
                {'session_id': session_id, 'status': 'pending'},
                {
                    '$set': {
                        'heartfelt_member_id': heartfelt_member_id,
                        'heartfelt_member_telehandle': heartfelt_member_telehandle,
                        'status': 'active',
                        'claimed_at': utcnow()
                    },
                    '$currentDate': {
                        'last_activity_at': True  # Atomic timestamp update
                    }
                }
            )
            
            success = result.modified_count > 0
            if success:
                logger.info(f"Session {session_id} claimed by member {heartfelt_member_id}")
            return success
            
        except Exception as e:
            logger.error(f"Error claiming session {session_id}: {e}")
            return False
    
    def end_session(self, session_id: str, ended_by_user_id: int, system_end: bool = False,
                    end_reason: str = None) -> bool:
        """End an active session and calculate duration.

        end_reason is purely additive metadata ('user_ended', 'queue_expired',
        'idle_expired', 'user_cancelled', 'stale_startup_sweep', 'duplicate_pending',
        'superseded_by_active'). status still becomes 'ended', so get_session_stats
        and src/database/utils.py are unaffected."""
        if not self.db_available:
            return False
            
        ended_at = utcnow()

        updates = {
            'status': 'ended',
            'ended_at': ended_at,
            'ended_by_user_id': ended_by_user_id if not system_end else None,
            'ended_by_system': system_end,
        }
        if end_reason is not None:
            updates['end_reason'] = end_reason

        # Use atomic operation to prevent double-termination
        try:
            result = db_manager.db.sessions.find_one_and_update(
                {'session_id': session_id, 'status': {'$in': ['pending', 'active']}},
                {'$set': updates},
                return_document=True
            )
        except Exception as e:
            logger.error(f"Error ending session {session_id}: {e}")
            return False

        if not result:
            return False  # Session already ended or doesn't exist

        # PAST THIS POINT THE TRANSITION IS COMMITTED, so nothing below may turn a
        # successful close into a False return. Callers use that return value as the
        # exactly-once gate on messaging real people (queue.sweep_expired_queues,
        # expiry._expire_session): a False here means "someone else already closed
        # it, stay quiet". If bookkeeping could produce the same False, we would end
        # the row in Mongo and then never tell the requester anything -- which is
        # exactly what used to happen, because the old duration math raised on every
        # pending row (`result.get('claimed_at', ...)` returns a present-but-None
        # value) AFTER find_one_and_update had already committed.
        duration_minutes = 0
        try:
            # Duration from claimed_at if available, otherwise created_at. Both are
            # normalised to aware UTC: a legacy naive value would otherwise raise
            # TypeError against the aware `ended_at`.
            start_time = ensure_aware_utc(result.get('claimed_at') or result.get('created_at'))
            if start_time is not None:
                duration_minutes = int((ended_at - start_time).total_seconds() / 60)

            db_manager.db.sessions.update_one(
                {'session_id': session_id},
                {'$set': {'duration_minutes': duration_minutes}}
            )
        except Exception as e:
            logger.warning(
                "Session %s was closed but its duration could not be recorded: %s",
                session_id, e)

        ended_by = "system auto-expiry" if system_end else f"user {ended_by_user_id}"
        logger.info(f"Session {session_id} ended by {ended_by}"
                    f"{' (' + end_reason + ')' if end_reason else ''}, "
                    f"duration: {duration_minutes}m")
        return True
    
    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session by session_id"""
        if not self.db_available:
            return None
            
        try:
            return db_manager.db.sessions.find_one({'session_id': session_id})
        except Exception as e:
            logger.error(f"Error getting session {session_id}: {e}")
            return None
    
    def get_active_session_by_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Get active session for a user"""
        if not self.db_available:
            return None
            
        try:
            return db_manager.db.sessions.find_one({
                'user_id': user_id,
                'status': {'$in': ['pending', 'active']}
            })
        except Exception as e:
            logger.error(f"Error getting active session for user {user_id}: {e}")
            return None
    
    def get_pending_sessions(self) -> List[Dict[str, Any]]:
        """Get all pending sessions"""
        if not self.db_available:
            return []
            
        try:
            return list(db_manager.db.sessions.find(
                {'status': 'pending'},
                sort=[('created_at', 1)]
            ))
        except Exception as e:
            logger.error(f"Error getting pending sessions: {e}")
            return []
    
    def set_queue_message(self, session_id: str, channel_id, message_id: int) -> bool:
        """Record where this request's channel post lives, so a restarted bot can
        edit or delete it.

        Deliberately NOT filtered on status: the post physically exists whether or
        not a member claimed it in the microsecond between send_message returning
        and this write. channel_id is stored as a string to match the env form used
        everywhere else."""
        if not self.db_available:
            return False

        try:
            result = db_manager.db.sessions.update_one(
                {'session_id': session_id},
                {'$set': {
                    'queue_channel_id': str(channel_id) if channel_id is not None else None,
                    'queue_message_id': int(message_id),
                }}
            )
            return result.matched_count > 0
        except Exception as e:
            logger.error(f"Error recording queue message for session {session_id}: {e}")
            return False

    def set_directed_message(self, session_id: str, message_id: int) -> bool:
        """Record the message_id of the DM sitting in the targeted supporter's chat,
        so a restarted bot can still strip its Accept/Decline buttons.

        Deliberately NOT filtered on routing: the DM physically exists whether or not
        the supporter tapped Decline in the microsecond between send_message returning
        and this write."""
        if not self.db_available:
            return False

        try:
            result = db_manager.db.sessions.update_one(
                {'session_id': session_id},
                {'$set': {'directed_message_id': int(message_id)}},
            )
            return result.matched_count > 0
        except Exception as e:
            logger.error(f"Error recording directed message for session {session_id}: {e}")
            return False

    def set_directed_notice(self, session_id: str, channel_id, message_id: int) -> bool:
        """Record where the no-detail channel note lives, so it can be edited later."""
        if not self.db_available:
            return False

        try:
            result = db_manager.db.sessions.update_one(
                {'session_id': session_id},
                {'$set': {
                    'notice_channel_id': str(channel_id) if channel_id is not None else None,
                    'notice_message_id': int(message_id),
                }},
            )
            return result.matched_count > 0
        except Exception as e:
            logger.error(f"Error recording directed notice for session {session_id}: {e}")
            return False

    # --- atomic routing transitions -----------------------------------------
    #
    # Each of these is the EXACTLY-ONCE GATE on a message to a real person, so each
    # is one find_one_and_update whose FILTER names the state it is leaving. PyMongo
    # is blocking, so a find_one_and_update is atomic against the event loop: two
    # concurrent taps both call it, exactly one gets a document back, and only that
    # one may send anything. Losing the race must be silent.
    #
    # ReturnDocument.AFTER, not the legacy `return_document=True` end_session still
    # uses. end_session is deliberately left alone: changing it would touch the one
    # gate every existing suite already depends on.

    def direct_session(self, session_id: str, member_id: int) -> Optional[Dict[str, Any]]:
        """choosing -> directed. The routing:'choosing' clause in the filter is what
        makes a double-tapped picker button send exactly ONE DM."""
        if not self.db_available:
            return None

        try:
            return db_manager.db.sessions.find_one_and_update(
                {'session_id': session_id, 'status': 'pending', 'routing': 'choosing'},
                {'$set': {
                    'routing': 'directed',
                    'target_member_id': int(member_id),
                    'directed_at': utcnow(),
                    'directed_message_id': None,
                }},
                return_document=ReturnDocument.AFTER,
            )
        except Exception as e:
            logger.error(f"Error directing session {session_id} to {member_id}: {e}")
            return None

    def undirect_session(self, session_id: str, member_id: int,
                         reason: str = None) -> Optional[Dict[str, Any]]:
        """directed -> choosing, for a decline, a lapse, a reroute or a release.

        $addToSet declined_by is why the same supporter is never re-offered, and
        waiting_since is reset so the requester gets a fresh window rather than
        being told to choose again and that their request expired, one sweep apart.
        """
        if not self.db_available:
            return None

        try:
            return db_manager.db.sessions.find_one_and_update(
                {'session_id': session_id, 'status': 'pending', 'routing': 'directed',
                 'target_member_id': int(member_id)},
                {'$set': {
                    'routing': 'choosing',
                    'target_member_id': None,
                    'directed_at': None,
                    'directed_message_id': None,
                    'waiting_since': utcnow(),
                    'last_undirect_reason': reason,
                 },
                 '$addToSet': {'declined_by': int(member_id)}},
                return_document=ReturnDocument.AFTER,
            )
        except Exception as e:
            logger.error(f"Error undirecting session {session_id} from {member_id}: {e}")
            return None

    def open_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """choosing/directed -> open: the requester chose to ask anyone who's free."""
        if not self.db_available:
            return None

        try:
            return db_manager.db.sessions.find_one_and_update(
                {'session_id': session_id, 'status': 'pending',
                 'routing': {'$in': ['choosing', 'directed']}},
                {'$set': {
                    'routing': 'open',
                    'target_member_id': None,
                    'directed_at': None,
                    'waiting_since': utcnow(),
                }},
                return_document=ReturnDocument.AFTER,
            )
        except Exception as e:
            logger.error(f"Error opening session {session_id} to the queue: {e}")
            return None

    def release_session(self, session_id: str, member_id: int) -> Optional[Dict[str, Any]]:
        """active -> pending, when the claiming member hands the conversation back.

        Deliberately NOT end_session: the request is not over, and the person who
        asked for help still needs someone. status returns to 'pending' so
        claim_session, end_session and get_pending_sessions keep working with no new
        special cases.

        claimed_at MUST be reset, or the eventual end_session measures duration from
        the abandoned stint. declined_by gets the releaser so the picker does not
        immediately re-offer the person who just stepped away.

        `routing` is deliberately NOT touched: it is the request's PROVENANCE, and
        release_command reads it to decide whether this request may go back to the
        channel at all. See D28.
        """
        if not self.db_available:
            return None

        now = utcnow()
        try:
            return db_manager.db.sessions.find_one_and_update(
                {'session_id': session_id, 'status': 'active',
                 'heartfelt_member_id': int(member_id)},
                {'$set': {
                    'status': 'pending',
                    'heartfelt_member_id': None,
                    'heartfelt_member_telehandle': None,
                    'claimed_at': None,
                    'waiting_since': now,
                    'released_at': now,
                 },
                 '$inc': {'release_count': 1},
                 '$addToSet': {'released_by': int(member_id),
                               'declined_by': int(member_id)}},
                return_document=ReturnDocument.AFTER,
            )
        except Exception as e:
            logger.error(f"Error releasing session {session_id} by {member_id}: {e}")
            return None

    def get_active_sessions(self) -> List[Dict[str, Any]]:
        """Get all active (claimed, unended) sessions, oldest first.

        Sorted on created_at rather than claimed_at so it reuses the existing
        (status, created_at) index."""
        if not self.db_available:
            return []

        try:
            return list(db_manager.db.sessions.find(
                {'status': 'active'},
                sort=[('created_at', 1)]
            ))
        except Exception as e:
            logger.error(f"Error getting active sessions: {e}")
            return []

    def update_session_activity(self, session_id: str) -> bool:
        """Update last_activity_at timestamp for a session"""
        if not self.db_available:
            return False
            
        try:
            result = db_manager.db.sessions.update_one(
                {'session_id': session_id, 'status': 'active'},
                {'$currentDate': {'last_activity_at': True}}
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error updating session activity {session_id}: {e}")
            return False
    
    def get_sessions_by_activity(self, cutoff_time: datetime.datetime) -> List[Dict[str, Any]]:
        """Get active sessions with last activity before cutoff time"""
        if not self.db_available:
            return []
            
        try:
            return list(db_manager.db.sessions.find({
                'status': 'active',
                'last_activity_at': {'$lte': cutoff_time}
            }))
        except Exception as e:
            logger.error(f"Error getting sessions by activity: {e}")
            return []

    # AUTHORIZED HEARTFELT MEMBERS

    def ensure_authorized_members_seed(self, default_members: Iterable[int], collection: str = None) -> None:
        """Seed the authorized members collection with defaults if empty."""
        if not self.db_available:
            return

        try:
            coll = collection or self._authorized_collection
            coll_obj = db_manager.db[coll]
            if coll_obj.estimated_document_count() > 0:
                return

            now = utcnow()
            docs = []
            for member_id in default_members:
                try:
                    member_int = int(member_id)
                except (TypeError, ValueError):
                    continue
                docs.append({
                    'telegram_id': member_int,
                    'active': True,
                    'created_at': now,
                    'updated_at': now,
                })

            if docs:
                coll_obj.insert_many(docs, ordered=False)
                logger.info("Seeded authorized members into MongoDB collection '%s'", coll)
        except Exception as e:
            logger.error(f"Error seeding authorized members: {e}")

    def _fetch_authorized_member_docs(self, include_inactive: bool = False, collection: str = None) -> Optional[List[Dict[str, Any]]]:
        if not self.db_available:
            return None

        try:
            coll = collection or self._authorized_collection
            query = {}
            if not include_inactive:
                query['active'] = {'$ne': False}

            docs = list(db_manager.db[coll].find(query))
            return docs
        except Exception as e:
            logger.error(f"Error retrieving authorized member records: {e}")
            return None

    def get_authorized_members(self, include_inactive: bool = False, collection: str = None) -> Optional[List[int]]:
        """Fetch authorized member IDs from a service's MongoDB collection."""
        docs = self._fetch_authorized_member_docs(include_inactive=include_inactive, collection=collection)
        if docs is None:
            return None

        members: List[int] = []
        for doc in docs:
            member_id = doc.get('telegram_id')
            try:
                members.append(int(member_id))
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring authorized member with invalid telegram_id: %s", member_id
                )
        return members

    def get_authorized_member_records(self, include_inactive: bool = True, collection: str = None) -> Optional[List[Dict[str, Any]]]:
        """Return raw authorized member documents from a service's MongoDB collection."""
        return self._fetch_authorized_member_docs(include_inactive=include_inactive, collection=collection)

    def add_authorized_member(self, member_id: int, username: str = None, active: bool = True, collection: str = None) -> bool:
        """Upsert an authorized member record in a service's collection."""
        if not self.db_available:
            return False

        try:
            coll = collection or self._authorized_collection
            now = utcnow()
            update = {
                '$set': {
                    'active': active,
                    'updated_at': now,
                },
                '$setOnInsert': {
                    'created_at': now,
                }
            }
            if username:
                update['$set']['username'] = username

            result = db_manager.db[coll].update_one(
                {'telegram_id': int(member_id)},
                update,
                upsert=True
            )
            return result.acknowledged
        except Exception as e:
            logger.error(f"Error adding authorized member {member_id}: {e}")
            return False

    def deactivate_authorized_member(self, member_id: int, collection: str = None) -> bool:
        """Mark an authorized member as inactive in a service's collection."""
        if not self.db_available:
            return False

        try:
            coll = collection or self._authorized_collection
            result = db_manager.db[coll].update_one(
                {'telegram_id': int(member_id)},
                {
                    '$set': {
                        'active': False,
                        'updated_at': utcnow()
                    }
                }
            )
            return result.modified_count > 0
        except Exception as e:
            logger.error(f"Error deactivating authorized member {member_id}: {e}")
            return False

    # --- supporter profiles (Phase 4) ---------------------------------------
    #
    # NONE of these upsert. The only creator of a roster entry stays
    # add_authorized_member: a typo in --telegram-id here must fail loudly, not
    # quietly authorize a stranger's Telegram id to claim conversations.

    def set_member_profile(self, member_id: int, collection: str = None,
                           display_name: str = None, blurb: str = None) -> bool:
        """Set the picker label and/or the one free-text line, leaving the other alone."""
        if not self.db_available:
            return False

        updates = {}
        if display_name is not None:
            updates['display_name'] = str(display_name).strip()
        if blurb is not None:
            updates['blurb'] = str(blurb).strip()
        if not updates:
            logger.error("set_member_profile called with nothing to set for %s", member_id)
            return False

        try:
            coll = collection or self._authorized_collection
            updates['updated_at'] = utcnow()
            result = db_manager.db[coll].update_one(
                {'telegram_id': int(member_id)},
                {'$set': updates},
            )
            return result.matched_count > 0
        except Exception as e:
            logger.error(f"Error setting profile for member {member_id}: {e}")
            return False

    def set_member_availability(self, member_id: int, available: bool,
                                collection: str = None) -> bool:
        """Toggle a supporter's availability.

        Returns matched_count > 0, NOT modified_count: running /available when
        already available changes nothing in Mongo but is a complete success, and
        reporting it as a failure would send a supporter chasing a non-problem.
        """
        if not self.db_available:
            return False

        try:
            coll = collection or self._authorized_collection
            result = db_manager.db[coll].update_one(
                {'telegram_id': int(member_id)},
                {'$set': {'available': bool(available), 'updated_at': utcnow()}},
            )
            return result.matched_count > 0
        except Exception as e:
            logger.error(f"Error setting availability for member {member_id}: {e}")
            return False

    def mark_member_started(self, member_id: int, started: bool = True,
                            collection: str = None) -> bool:
        """Record whether this member has an open chat with the bot.

        Written when a member /starts or sends any private message, and CLEARED when
        a directed DM comes back "bot can't initiate conversation" or "blocked" --
        self-healing, so a supporter who blocks and later unblocks returns to the
        picker on their next message rather than staying broken forever.
        """
        if not self.db_available:
            return False

        try:
            coll = collection or self._authorized_collection
            updates = {'has_started_bot': bool(started), 'updated_at': utcnow()}
            if started:
                updates['started_bot_at'] = utcnow()
            result = db_manager.db[coll].update_one(
                {'telegram_id': int(member_id)},
                {'$set': updates},
            )
            return result.matched_count > 0
        except Exception as e:
            logger.error(f"Error marking member {member_id} started: {e}")
            return False

    def get_member_profile_doc(self, member_id: int,
                               collection: str = None) -> Optional[Dict[str, Any]]:
        """One raw member document, for the CLI's confirmation output."""
        if not self.db_available:
            return None

        try:
            coll = collection or self._authorized_collection
            return db_manager.db[coll].find_one({'telegram_id': int(member_id)})
        except Exception as e:
            logger.error(f"Error reading profile for member {member_id}: {e}")
            return None

    def remove_authorized_member(self, member_id: int, collection: str = None) -> bool:
        """Completely remove an authorized member record from a service's collection."""
        if not self.db_available:
            return False

        try:
            coll = collection or self._authorized_collection
            result = db_manager.db[coll].delete_one({'telegram_id': int(member_id)})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Error removing authorized member {member_id}: {e}")
            return False

    # MESSAGE MANAGEMENT
    
    def log_message(self, session_id: str, from_user_id: int, to_user_id: int, 
                   message_type: str, content: str = None, file_id: str = None, 
                   file_type: str = None) -> bool:
        """Log a message to the database"""
        if not self.db_available:
            return False
            
        try:
            message_doc = {
                'session_id': session_id,
                'from_user_id': from_user_id,
                'to_user_id': to_user_id,
                'message_type': message_type,
                'content': content,
                'file_id': file_id,
                'file_type': file_type,
                'timestamp': utcnow()
            }
            
            db_manager.db.messages.insert_one(message_doc)
            return True
            
        except Exception as e:
            logger.error(f"Error logging message for session {session_id}: {e}")
            return False
    
    def get_session_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all messages for a session ordered by timestamp"""
        if not self.db_available:
            return []
            
        try:
            return list(db_manager.db.messages.find(
                {'session_id': session_id},
                sort=[('timestamp', 1)]
            ))
        except Exception as e:
            logger.error(f"Error getting messages for session {session_id}: {e}")
            return []
    
    # ANALYTICS QUERIES
    
    def get_sessions_in_date_range(self, start_date: datetime.datetime, 
                                  end_date: datetime.datetime) -> List[Dict[str, Any]]:
        """Get sessions created within a date range"""
        if not self.db_available:
            return []
            
        try:
            return list(db_manager.db.sessions.find(
                {
                    'created_at': {
                        '$gte': start_date,
                        '$lte': end_date
                    }
                },
                sort=[('created_at', -1)]
            ))
        except Exception as e:
            logger.error(f"Error getting sessions in date range: {e}")
            return []
    
    def get_session_stats(self) -> Dict[str, int]:
        """Get basic session statistics"""
        if not self.db_available:
            return {}
            
        try:
            pipeline = [
                {
                    '$group': {
                        '_id': '$status',
                        'count': {'$sum': 1}
                    }
                }
            ]
            
            result = list(db_manager.db.sessions.aggregate(pipeline))
            stats = {item['_id']: item['count'] for item in result}
            
            # Add total count
            stats['total'] = sum(stats.values())
            
            return stats
            
        except Exception as e:
            logger.error(f"Error getting session stats: {e}")
            return {}

# Global db manager instance
db_mgr = DBManager()
