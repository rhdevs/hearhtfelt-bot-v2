import asyncio
import datetime
import logging
from typing import Dict, Any
from telegram import Bot
from config import (
    active_sessions,
    session_warnings,
    user_states,
    UserState,
    SESSION_TIMEOUT_MINUTES,
    SESSION_WARNING_MINUTES,
    SESSION_SWEEP_SECONDS,
    MESSAGES,
    is_heartfelt_member,
)
from src.database.manager import db_mgr

logger = logging.getLogger(__name__)

class SessionExpiryManager:
    """Manages session expiry, warnings, and cleanup"""
    
    def __init__(self, bot: Bot, session_manager):
        self.bot = bot
        self.session_manager = session_manager
        self.running = False
    
    async def start(self):
        """Start the background cleanup task"""
        self.running = True
        logger.info("Starting session expiry cleanup task")
        
        while self.running:
            try:
                await self._cleanup_expired_sessions()
            except Exception as e:
                logger.error(f"Error during session cleanup: {e}")
            
            # Wait before next cleanup cycle
            await asyncio.sleep(SESSION_SWEEP_SECONDS)
    
    def stop(self):
        """Stop the background cleanup task"""
        self.running = False
        logger.info("Stopping session expiry cleanup task")
    
    async def _cleanup_expired_sessions(self):
        """Check for expired sessions and handle warnings/cleanup"""
        now = datetime.datetime.now()
        
        # Calculate cutoff times
        expiry_cutoff = now - datetime.timedelta(minutes=SESSION_TIMEOUT_MINUTES)
        warning_cutoff = now - datetime.timedelta(minutes=SESSION_WARNING_MINUTES)
        
        sessions_to_expire = []
        sessions_to_warn = []
        
        # Check in-memory sessions first (primary data source)
        for session_id, session_data in active_sessions.items():
            last_activity = session_data.get('last_activity_at', session_data.get('created_at'))
            
            if not last_activity:
                continue
                
            # Check if session should be expired
            if last_activity <= expiry_cutoff:
                sessions_to_expire.append((session_id, session_data))
            # Check if session needs warning (and hasn't been warned yet)
            elif (last_activity <= warning_cutoff and 
                  not session_warnings.get(session_id, False)):
                sessions_to_warn.append((session_id, session_data))
        
        # If database is available, also check for any sessions that might be missing from memory
        if db_mgr.db_available:
            try:
                db_expired = db_mgr.get_sessions_by_activity(expiry_cutoff)
                for db_session in db_expired:
                    session_id = db_session['session_id']
                    # Only add if not already in memory (edge case for bot restarts)
                    if session_id not in active_sessions:
                        sessions_to_expire.append((session_id, db_session))
            except Exception as e:
                logger.error(f"Error checking database for expired sessions: {e}")
        
        # Process warnings
        for session_id, session_data in sessions_to_warn:
            await self._send_session_warning(session_id, session_data)
        
        # Process expiries
        for session_id, session_data in sessions_to_expire:
            await self._expire_session(session_id, session_data)
        
        # Log summary if any actions taken
        if sessions_to_warn or sessions_to_expire:
            logger.info(f"Session cleanup: {len(sessions_to_warn)} warnings sent, "
                       f"{len(sessions_to_expire)} sessions expired")
    
    async def _send_session_warning(self, session_id: str, session_data: Dict[str, Any]):
        """Send warning message to both parties in a session"""
        try:
            user_id = session_data['user_id']
            heartfelt_member_id = session_data['heartfelt_member_id']
            
            # Mark as warned to prevent spam
            session_warnings[session_id] = True
            
            # Send warning to user
            try:
                await self.bot.send_message(
                    chat_id=user_id,
                    text=MESSAGES["session_warning"]
                )
            except Exception as e:
                logger.warning(f"Could not send warning to user {user_id}: {e}")
            
            # Send warning to heartfelt member
            try:
                await self.bot.send_message(
                    chat_id=heartfelt_member_id,
                    text=MESSAGES["session_warning"]
                )
            except Exception as e:
                logger.warning(f"Could not send warning to heartfelt member {heartfelt_member_id}: {e}")
            
            logger.info(f"Sent inactivity warning for session {session_id}")
            
        except Exception as e:
            logger.error(f"Error sending warning for session {session_id}: {e}")
    
    async def _expire_session(self, session_id: str, session_data: Dict[str, Any]):
        """Expire a session due to inactivity"""
        try:
            user_id = session_data['user_id']
            heartfelt_member_id = session_data['heartfelt_member_id']
            
            # Log system message to transcript if database available
            if db_mgr.db_available:
                try:
                    db_mgr.log_message(
                        session_id=session_id,
                        from_user_id=0,  # System message
                        to_user_id=0,
                        message_type="system",
                        content="Session automatically closed due to inactivity"
                    )
                except Exception as e:
                    logger.warning(f"Could not log system message for session {session_id}: {e}")
            
            # End the session (this handles database updates and cleanup)
            await self.session_manager.end_session(session_id, user_id, system_end=True)
            
            # Update user states
            if user_id:
                user_states[user_id] = UserState.IDLE
            if heartfelt_member_id:
                user_states[heartfelt_member_id] = UserState.IDLE
            
            # Send expiry notifications
            if user_id:
                try:
                    message = (
                        MESSAGES["session_expired_heartfelt"]
                        if is_heartfelt_member(user_id)
                        else MESSAGES["session_expired"]
                    )
                    await self.bot.send_message(
                        chat_id=user_id,
                        text=message
                    )
                except Exception as e:
                    logger.warning(f"Could not notify user {user_id} of expiry: {e}")
            
            if heartfelt_member_id and heartfelt_member_id != user_id:
                try:
                    message = (
                        MESSAGES["session_expired_heartfelt"]
                        if is_heartfelt_member(heartfelt_member_id)
                        else MESSAGES["session_expired"]
                    )
                    await self.bot.send_message(
                        chat_id=heartfelt_member_id,
                        text=message
                    )
                except Exception as e:
                    logger.warning(f"Could not notify heartfelt member {heartfelt_member_id} of expiry: {e}")
            
            logger.info(f"Session {session_id} expired due to inactivity")
            
        except Exception as e:
            logger.error(f"Error expiring session {session_id}: {e}")
