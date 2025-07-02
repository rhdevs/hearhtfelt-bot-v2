import datetime
import uuid
import logging
from typing import Optional, List, Dict, Any
from src.database.connection import db_manager

logger = logging.getLogger(__name__)

class DBManager:
    def __init__(self):
        self.db_available = False
        
    def initialize(self):
        """Initialize database connection"""
        self.db_available = db_manager.connect()
        return self.db_available
    
    # SESSION MANAGEMENT
    
    def create_session(self, user_id: int, description: str, anonymous_user_id: str, session_id: str = None) -> Optional[str]:
        """Create a new session in pending state"""
        if not self.db_available:
            return None
            
        try:
            if session_id is None:
                session_id = str(uuid.uuid4())
                
            now = datetime.datetime.utcnow()
            session_doc = {
                'session_id': session_id,
                'user_id': user_id,
                'heartfelt_member_id': None,  # null until claimed
                'anonymous_user_id': anonymous_user_id,
                'status': 'pending',
                'description': description,
                'created_at': now,
                'last_activity_at': now,  # Track activity for auto-expiry
                'claimed_at': None,
                'ended_at': None,
                'ended_by_user_id': None
            }
            
            db_manager.db.sessions.insert_one(session_doc)
            logger.info(f"Created session {session_id} for user {user_id}")
            return session_id
            
        except Exception as e:
            logger.error(f"Error creating session: {e}")
            return None
    
    def claim_session(self, session_id: str, heartfelt_member_id: int) -> bool:
        """Claim a pending session and activate it"""
        if not self.db_available:
            return False
            
        try:
            result = db_manager.db.sessions.update_one(
                {'session_id': session_id, 'status': 'pending'},
                {
                    '$set': {
                        'heartfelt_member_id': heartfelt_member_id,
                        'status': 'active',
                        'claimed_at': datetime.datetime.utcnow()
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
    
    def end_session(self, session_id: str, ended_by_user_id: int, system_end: bool = False) -> bool:
        """End an active session and calculate duration"""
        if not self.db_available:
            return False
            
        try:
            ended_at = datetime.datetime.utcnow()
            
            # Use atomic operation to prevent double-termination
            result = db_manager.db.sessions.find_one_and_update(
                {'session_id': session_id, 'status': {'$in': ['pending', 'active']}},
                {
                    '$set': {
                        'status': 'ended',
                        'ended_at': ended_at,
                        'ended_by_user_id': ended_by_user_id if not system_end else None,
                        'ended_by_system': system_end
                    }
                },
                return_document=True
            )
            
            if not result:
                return False  # Session already ended or doesn't exist
            
            # Calculate duration from claimed_at if available, otherwise from created_at
            start_time = result.get('claimed_at', result['created_at'])
            duration_minutes = int((ended_at - start_time).total_seconds() / 60)
            
            # Update with calculated duration
            db_manager.db.sessions.update_one(
                {'session_id': session_id},
                {'$set': {'duration_minutes': duration_minutes}}
            )

            end_reason = "system auto-expiry" if system_end else f"user {ended_by_user_id}"
            logger.info(f"Session {session_id} ended by {end_reason}, duration: {duration_minutes}m")
            return True
            
        except Exception as e:
            logger.error(f"Error ending session {session_id}: {e}")
            return False
    
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
                'timestamp': datetime.datetime.utcnow()
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