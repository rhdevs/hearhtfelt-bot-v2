import uuid
import datetime
import random
import logging
from typing import Optional, Tuple
from telegram import Bot
from config import active_sessions, safety_logs, user_to_session_map
from src.database.manager import db_mgr

logger = logging.getLogger(__name__)

class SessionManager:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.used_anonymous_ids = set()  # Track used IDs to avoid duplicates
    
    def _generate_anonymous_id(self) -> str:
        """Generate a unique anonymous ID using RHesident format"""
        # Try up to 50 times to generate a unique ID
        for _ in range(50):
            number = random.randint(1000, 9999)
            anonymous_id = f"RHesident #{number}"
            
            if anonymous_id not in self.used_anonymous_ids:
                self.used_anonymous_ids.add(anonymous_id)
                return anonymous_id
        
        # Fallback to UUID if we can't generate unique ID
        fallback_id = f"RHesident #{str(uuid.uuid4())[:8]}"
        self.used_anonymous_ids.add(fallback_id)
        return fallback_id
    
    def create_session(self, user_id: int, heartfelt_member_id: int, session_id: str = None) -> str:
        """Create a new anonymous session between user and heartfelt member"""
        # Use provided session_id (from queue_id) or generate new one
        if session_id is None:
            session_id = str(uuid.uuid4())
        
        anonymous_user_id = self._generate_anonymous_id()
        
        active_sessions[session_id] = {
            'user_id': user_id,
            'heartfelt_member_id': heartfelt_member_id,
            'created_at': datetime.datetime.now(),
            'anonymous_user_id': anonymous_user_id
        }
        
        # Maintain O(1) lookup indices
        user_to_session_map[user_id] = session_id
        user_to_session_map[heartfelt_member_id] = session_id
        
        # Save to database - claim existing pending session or create new active one
        if db_mgr.db_available:
            db_mgr.claim_session(session_id, heartfelt_member_id)
        
        # Safety log for developers
        safety_logs.append({
            'session_id': session_id,
            'user_id': user_id,
            'heartfelt_member_id': heartfelt_member_id,
            'timestamp': datetime.datetime.now(),
            'action': 'session_created'
        })
        
        return session_id
    
    def get_session_by_user(self, user_id: int) -> Optional[str]:
        """Find active session for a user - O(1) lookup"""
        return user_to_session_map.get(user_id)
    
    def get_session_info(self, session_id: str) -> Optional[dict]:
        """Get session information"""
        return active_sessions.get(session_id)
    
    def get_other_party(self, session_id: str, current_user_id: int) -> Optional[int]:
        """Get the other party's user ID in a session"""
        session = active_sessions.get(session_id)
        if not session:
            return None
        
        if session['user_id'] == current_user_id:
            return session['heartfelt_member_id']
        elif session['heartfelt_member_id'] == current_user_id:
            return session['user_id']
        
        return None
    
    def is_heartfelt_member(self, session_id: str, user_id: int) -> bool:
        """Check if user is the heartfelt member in this session"""
        session = active_sessions.get(session_id)
        if not session:
            return False
        return session['heartfelt_member_id'] == user_id
    
    def get_anonymous_name(self, session_id: str, for_user_id: int) -> str:
        """Get the anonymous name to display for the other party"""
        session = active_sessions.get(session_id)
        if not session:
            return "Unknown"
        
        if session['user_id'] == for_user_id:
            return "Heartfelt Member"
        else:
            return session['anonymous_user_id']
    
    async def forward_message(self, session_id: str, from_user_id: int, message_text: str) -> bool:
        """Forward message to the other party in the session"""
        try:
            other_party_id = self.get_other_party(session_id, from_user_id)
            if not other_party_id:
                return False
            
            anonymous_name = self.get_anonymous_name(session_id, other_party_id)
            formatted_message = f"💬 {anonymous_name}: {message_text}"
            
            await self.bot.send_message(
                chat_id=other_party_id,
                text=formatted_message
            )
            
            # Log to database
            if db_mgr.db_available:
                db_mgr.log_message(
                    session_id=session_id,
                    from_user_id=from_user_id,
                    to_user_id=other_party_id,
                    message_type="text",
                    content=message_text
                )
            
            # Safety log
            safety_logs.append({
                'session_id': session_id,
                'from_user_id': from_user_id,
                'to_user_id': other_party_id,
                'timestamp': datetime.datetime.now(),
                'action': 'message_forwarded'
            })
            
            return True
        except Exception as e:
            logger.error(f"Error forwarding message in session {session_id}: {e}")
            return False
    
    async def forward_sticker(self, session_id: str, from_user_id: int, sticker_file_id: str) -> bool:
        """Forward a sticker to the other party in the session"""
        try:
            other_party_id = self.get_other_party(session_id, from_user_id)
            if not other_party_id:
                return False
            
            anonymous_name = self.get_anonymous_name(session_id, other_party_id)
            
            # Send the sticker
            await self.bot.send_sticker(
                chat_id=other_party_id,
                sticker=sticker_file_id
            )
            
            # Send a caption to identify the sender
            await self.bot.send_message(
                chat_id=other_party_id,
                text=f"🎭 {anonymous_name} sent a sticker"
            )
            
            # Log to database
            if db_mgr.db_available:
                db_mgr.log_message(
                    session_id=session_id,
                    from_user_id=from_user_id,
                    to_user_id=other_party_id,
                    message_type="file",
                    file_id=sticker_file_id,
                    file_type="sticker"
                )
            
            # Safety log
            safety_logs.append({
                'session_id': session_id,
                'from_user_id': from_user_id,
                'to_user_id': other_party_id,
                'timestamp': datetime.datetime.now(),
                'action': 'sticker_forwarded'
            })
            
            return True
        except Exception as e:
            logger.error(f"Error forwarding sticker in session {session_id}: {e}")
            return False
    
    async def end_session(self, session_id: str, ended_by_user_id: int) -> Tuple[Optional[int], Optional[int]]:
        """End a session and return both user IDs"""
        session = active_sessions.get(session_id)
        if not session:
            return None, None
        
        user_id = session['user_id']
        heartfelt_member_id = session['heartfelt_member_id']
        
        # End session in database
        if db_mgr.db_available:
            db_mgr.end_session(session_id, ended_by_user_id)
        
        # Remove session and clean up indices
        del active_sessions[session_id]
        
        # Clean up O(1) lookup indices
        if user_id in user_to_session_map:
            del user_to_session_map[user_id]
        if heartfelt_member_id in user_to_session_map:
            del user_to_session_map[heartfelt_member_id]
        
        # Safety log
        safety_logs.append({
            'session_id': session_id,
            'ended_by_user_id': ended_by_user_id,
            'timestamp': datetime.datetime.now(),
            'action': 'session_ended'
        })
        
        return user_id, heartfelt_member_id