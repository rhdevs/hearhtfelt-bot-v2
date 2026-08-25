import datetime
import html
import logging
import random
import uuid
from typing import List, Optional, Tuple
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from config import (
    MESSAGES,
    used_anonymous_ids,
    ServiceType,
    UserState,
    get_service,
    queue_entries,
    queue_order,
    user_states,
    user_to_queue_map,
)
from src.timeutil import ensure_aware_utc, format_hhmm, utcnow
from src.database.manager import db_mgr


class SelfClaimError(Exception):
    """Raised when the claimant tries to take their own queue entry"""
    pass


logger = logging.getLogger(__name__)


class QueueManager:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.channel_accessible = {}  # service_key -> bool (per-service channel reachability)
        # Shared with SessionManager via config so the two never collide.
        self.used_anonymous_ids = used_anonymous_ids

    def _channel_for(self, entry: dict):
        """Channel this entry's post actually lives in.

        The recorded value wins over current config, because a service's channel_id
        can change between restarts and the post does not move with it."""
        return entry.get('channel_id') or get_service(entry.get('service')).channel_id

    def get_queue_entry(self, queue_id: str) -> Optional[dict]:
        """Return the in-memory queue entry (or None) without mutating it."""
        return queue_entries.get(queue_id)

    def get_user_service(self, user_id: int) -> Optional[str]:
        """Return the service key of the user's active queue entry, if any."""
        queue_id = user_to_queue_map.get(user_id)
        entry = queue_entries.get(queue_id) if queue_id else None
        return entry.get('service') if entry else None

    def _generate_anonymous_id(self, prefix: str = "RHesident") -> str:
        """Generate a unique anonymous ID using the given prefix"""
        # Try up to 50 times to generate a unique ID
        for _ in range(50):
            number = random.randint(1000, 9999)
            anonymous_id = f"{prefix} #{number}"

            if anonymous_id not in self.used_anonymous_ids:
                self.used_anonymous_ids.add(anonymous_id)
                return anonymous_id

        # Fallback to UUID if we can't generate unique ID
        fallback_id = f"{prefix} #{str(uuid.uuid4())[:8]}"
        self.used_anonymous_ids.add(fallback_id)
        return fallback_id

    def add_to_queue(self, user_id: int, description: str, user_telehandle: str = None,
                     service_key: str = ServiceType.HF.value) -> str:
        """Add user to the help queue for the given service"""
        queue_id = str(uuid.uuid4())
        anonymous_id = self._generate_anonymous_id(get_service(service_key).anon_prefix)

        queue_entries[queue_id] = {
            'user_id': user_id,
            'description': description,
            'created_at': utcnow(),
            'anonymous_id': anonymous_id,
            'message_id': None,  # Will be set after posting to channel
            'channel_id': None,  # ditto -- the channel the post actually landed in
            'service': service_key,
        }

        # Maintain O(1) lookup indices
        user_to_queue_map[user_id] = queue_id
        queue_order.append(queue_id)

        # Create pending session in database using queue_id as session_id
        if db_mgr.db_available:
            db_mgr.create_session(user_id, description, anonymous_id, queue_id, user_telehandle, service=service_key)

        return queue_id
    
    async def post_queue_to_channel(self, queue_id: str) -> Tuple[bool, str]:
        """Post queue entry to admin channel with claim button"""
        try:
            queue_entry = queue_entries.get(queue_id)
            if not queue_entry:
                return False, "Queue entry not found"

            svc = get_service(queue_entry.get('service'))

            # Skip if this service's channel is known to be inaccessible
            if not self.channel_accessible.get(svc.key, True):
                return False, "channel_offline"

            # Create message text
            message_text = (
                f"{svc.request_title}\n\n"
                f"From: {queue_entry['anonymous_id']}\n"
                f"Time: {format_hhmm(queue_entry['created_at'])}\n\n"
                f"Description: {queue_entry['description'][:200]}{'...' if len(queue_entry['description']) > 200 else ''}"
            )

            # Create inline keyboard with claim button
            keyboard = [[InlineKeyboardButton("📞 Claim", callback_data=f"claim_{queue_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Send message to this service's queue channel
            message = await self.bot.send_message(
                chat_id=svc.channel_id,
                text=message_text,
                reply_markup=reply_markup
            )

            # Store message ID for later deletion
            queue_entries[queue_id]['message_id'] = message.message_id
            queue_entries[queue_id]['channel_id'] = svc.channel_id
            self.channel_accessible[svc.key] = True  # Mark as accessible on success

            # Persist it so a restarted bot can still edit or delete this post. A DB
            # hiccup here must NOT fail the post: the requester is already queued and
            # the in-memory entry is complete. The only cost is losing edit/delete
            # ability across a restart.
            if db_mgr.db_available:
                try:
                    db_mgr.set_queue_message(queue_id, svc.channel_id, message.message_id)
                except Exception as exc:
                    logger.warning("Could not persist queue message id for %s: %s", queue_id, exc)

            return True, "success"

        except Exception as e:
            error_msg = str(e).lower()
            print(f"Error posting queue to channel: {e}")
            svc_key = get_service(queue_entries.get(queue_id, {}).get('service')).key

            # Categorize the error
            if "chat not found" in error_msg:
                self.channel_accessible[svc_key] = False
                return False, "chat_not_found"
            elif "forbidden" in error_msg or "not enough rights" in error_msg:
                self.channel_accessible[svc_key] = False
                return False, "access_denied"
            elif "network" in error_msg or "timeout" in error_msg:
                return False, "network_error"
            else:
                return False, "unknown_error"
    
    async def claim_queue(self, queue_id: str, heartfelt_member_id: int, heartfelt_member_name: str = None, heartfelt_member_telehandle: str = None) -> Optional[int]:
        """Claim a queue entry and return the user ID"""
        queue_entry = queue_entries.get(queue_id)
        if not queue_entry:
            logger.warning("Claim attempted on missing or expired queue entry %s by member %s", queue_id, heartfelt_member_id)
            return None

        user_id = queue_entry['user_id']
        message_id = queue_entry.get('message_id')
        if not message_id and queue_entry.get('restored'):
            logger.warning(
                "Claimed restored entry %s with no recorded message_id; channel post "
                "left unedited", queue_id)

        if user_id == heartfelt_member_id:
            raise SelfClaimError("Claimant cannot take their own queue entry")
        
        # Claim the session in database (queue_id is used as session_id).
        # When DB is available this is the atomic winner-selection: only the first
        # claimer flips status pending->active, so a losing concurrent claim aborts here.
        if db_mgr.db_available:
            claimed = db_mgr.claim_session(queue_id, heartfelt_member_id, heartfelt_member_telehandle)
            if not claimed:
                logger.info("Queue %s already claimed (DB guard); ignoring duplicate claim by %s", queue_id, heartfelt_member_id)
                return None

        # Take the entry out of the queue NOW -- synchronously, before the first
        # await -- so the in-memory claim is as atomic as the DB one above.
        #
        # This used to happen after `await self._edit_claimed_message(...)`. That
        # await yields, and if the 5-minute queue_cleanup_loop resumes in the gap
        # while the entry is past its window, cleanup_expired_queues still sees it,
        # pops it, and sweep_expired_queues closes the row -- whose status the claim
        # just flipped to 'active', which end_session's {pending, active} filter
        # happily matches. The requester was DMed "your place in the queue expired"
        # and then connected to a companion a moment later, with the conversation's
        # Mongo row already ended as 'queue_expired': no transcript close, no
        # activity updates, and nothing for rehydration to restore.
        #
        # `queue_entry` is a local reference, so the edit below still works.
        queue_entries.pop(queue_id, None)
        if user_id in user_to_queue_map:
            del user_to_queue_map[user_id]
        if queue_id in queue_order:
            queue_order.remove(queue_id)

        # Edit the queue message to show it's been claimed instead of deleting it
        try:
            if message_id:
                await self._edit_claimed_message(queue_entry, heartfelt_member_name or f"Member #{heartfelt_member_id}")
        except Exception as e:
            print(f"Error editing queue message: {e}")
            # Fallback to deletion if edit fails
            try:
                await self.bot.delete_message(
                    chat_id=self._channel_for(queue_entry),
                    message_id=message_id
                )
            except Exception as delete_error:
                print(f"Error deleting queue message as fallback: {delete_error}")

        return user_id
    
    async def _edit_claimed_message(self, queue_entry: dict, heartfelt_member_name: str):
        """Edit the queue message to show it's been claimed"""
        claimed_time = utcnow()
        
        # Create the claimed message text with HTML formatting
        # Escape user-provided content to prevent HTML injection
        safe_member_name = html.escape(heartfelt_member_name)
        safe_description = html.escape(queue_entry['description'][:200])
        description_suffix = '...' if len(queue_entry['description']) > 200 else ''

        claimed_message_text = (
            f"✅ <b>CLAIMED</b> - Help Request\n\n"
            f"From: {queue_entry['anonymous_id']}\n"
            f"Requested: {format_hhmm(queue_entry['created_at'])}\n"
            f"Claimed by: {safe_member_name}\n"
            f"Claimed at: {format_hhmm(claimed_time)}\n\n"
            f"Description: {safe_description}{description_suffix}"
        )

        # Edit the message, removing the claim button and applying HTML formatting
        try:
            await self.bot.edit_message_text(
                chat_id=self._channel_for(queue_entry),
                message_id=queue_entry['message_id'],
                text=claimed_message_text,
                reply_markup=None,
                parse_mode='HTML'
            )
        except Exception as e:
            print(f"Error editing claimed message: {e}")
    
    def get_queue_position(self, user_id: int) -> Optional[int]:
        """Get user's position in queue - O(1) lookup"""
        # Check if user is in queue
        queue_id = user_to_queue_map.get(user_id)
        if not queue_id:
            return None
        
        # Find position in ordered queue (O(n) but only for valid queue IDs)
        try:
            return queue_order.index(queue_id) + 1
        except ValueError:
            # Queue ID not in order list (data inconsistency)
            return None
    
    def get_estimated_wait_time(self, position: int) -> int:
        """Estimate wait time based on queue position"""
        # Simple estimation: 5 minutes per person ahead
        return position * 5
    
    def cleanup_expired_queues(self) -> List[dict]:
        """Remove expired queue entries. Returns the popped entries (sync, no Telegram I/O).

        Each returned entry is the popped entry plus 'queue_id' and 'notify', where
        notify is True iff the user was still IN_QUEUE at cleanup time -- the same
        gate the old code used before messaging anyone.
        """
        now = utcnow()
        expired_queue_ids = []

        for queue_id, entry in list(queue_entries.items()):
            created = ensure_aware_utc(entry.get('created_at'))
            window_seconds = get_service(entry.get('service')).queue_expire_minutes * 60
            if created is None:
                # Unusable timestamp: we cannot tell how long this has waited, and
                # leaving it queued forever is worse than retiring it.
                logger.warning("Queue entry %s has no usable created_at; expiring it", queue_id)
                expired = True
            else:
                expired = (now - created).total_seconds() > window_seconds
            if expired:
                expired_queue_ids.append(queue_id)

        expired_entries = []
        for queue_id in expired_queue_ids:
            entry = queue_entries.get(queue_id)
            if not entry:
                continue

            notify = False
            user_id = entry.get('user_id')
            if user_id:
                if user_states.get(user_id) == UserState.IN_QUEUE:
                    user_states[user_id] = UserState.IDLE
                    notify = True
                if user_id in user_to_queue_map:
                    del user_to_queue_map[user_id]

            queue_entries.pop(queue_id, None)
            if queue_id in queue_order:
                queue_order.remove(queue_id)

            expired_entries.append({**entry, 'queue_id': queue_id, 'notify': notify})

        return expired_entries

    async def sweep_expired_queues(self) -> List[dict]:
        """Expire stale queue entries: close the DB row, retire the channel post,
        notify the requester. Returns the entries actually acted on."""
        acted = []
        for entry in self.cleanup_expired_queues():
            queue_id = entry['queue_id']
            svc = get_service(entry.get('service'))

            # 1. Close the Mongo row FIRST. end_session is an atomic
            #    find_one_and_update filtered on status in {pending, active}, so a False
            #    return means another actor already moved this row -- possibly a member
            #    claiming it a moment ago. Telling that requester it expired would be a
            #    lie, so we skip the notification entirely.
            won = True
            if db_mgr.db_available:
                try:
                    won = db_mgr.end_session(queue_id, None, system_end=True,
                                             end_reason='queue_expired')
                except Exception as exc:
                    logger.warning("Could not close expired queue row %s: %s", queue_id, exc)
                    won = False
            if not won:
                logger.info("Queue %s was already claimed or closed elsewhere; not notifying",
                            queue_id)
                continue

            # 2. Retire the channel post so its Claim button stops being live.
            await self._retire_channel_post(entry, svc)

            # 3. Notify the requester.
            if entry.get('notify') and entry.get('user_id'):
                try:
                    await self.bot.send_message(
                        chat_id=entry['user_id'],
                        text=MESSAGES["queue_expired"].format(member=svc.member_label),
                    )
                except Exception as exc:
                    logger.warning("Failed to notify user %s about queue expiry: %s",
                                   entry['user_id'], exc)

            acted.append(entry)

        return acted

    async def _retire_channel_post(self, entry: dict, svc) -> None:
        """Edit an expired request's channel post into an EXPIRED card and strip the
        Claim button, falling back to deleting it. NEVER raises."""
        message_id = entry.get('message_id')
        if not message_id or not self.channel_accessible.get(svc.key, True):
            return

        description = entry.get('description') or ''
        safe_description = html.escape(description[:200])
        suffix = '...' if len(description) > 200 else ''
        text = (
            f"⌛ <b>EXPIRED</b> - Help Request\n\n"
            f"From: {entry.get('anonymous_id', 'Unknown')}\n"
            f"Requested: {format_hhmm(entry.get('created_at'))}\n"
            f"Expired at: {format_hhmm(utcnow())}\n\n"
            f"Description: {safe_description}{suffix}"
        )

        try:
            await self.bot.edit_message_text(
                chat_id=self._channel_for(entry),
                message_id=message_id,
                text=text,
                reply_markup=None,
                parse_mode='HTML',
            )
            return
        except Exception as exc:
            logger.warning("Could not edit expired queue post %s: %s", entry.get('queue_id'), exc)

        try:
            await self.bot.delete_message(
                chat_id=self._channel_for(entry),
                message_id=message_id,
            )
        except Exception as exc:
            logger.warning("Could not delete expired queue post %s either: %s",
                           entry.get('queue_id'), exc)

    
    def is_user_in_queue(self, user_id: int) -> bool:
        """Check if user is already in queue - O(1) lookup"""
        return user_id in user_to_queue_map
    
    async def remove_from_queue(self, user_id: int) -> Tuple[bool, str]:
        """Remove user from queue and clean up admin channel message - O(1) lookup"""
        # Find the user's queue entry using O(1) lookup
        queue_id_to_remove = user_to_queue_map.get(user_id)
        
        if not queue_id_to_remove:
            return False, "not_in_queue"
        
        queue_entry = queue_entries.get(queue_id_to_remove)
        if not queue_entry:
            # Clean up inconsistent state
            if user_id in user_to_queue_map:
                del user_to_queue_map[user_id]
            return False, "not_in_queue"
        
        # Try to delete the service's queue channel message
        svc = get_service(queue_entry.get('service'))
        if queue_entry.get('message_id') and self.channel_accessible.get(svc.key, True):
            try:
                await self.bot.delete_message(
                    chat_id=self._channel_for(queue_entry),
                    message_id=queue_entry['message_id']
                )
            except Exception as e:
                print(f"Error deleting queue message during cancellation: {e}")
                # Continue with removal even if message deletion fails
        
        # Remove from queue and clean up indices
        del queue_entries[queue_id_to_remove]
        
        # Clean up O(1) lookup indices  
        if user_id in user_to_queue_map:
            del user_to_queue_map[user_id]
        if queue_id_to_remove in queue_order:
            queue_order.remove(queue_id_to_remove)

        # Close the Mongo row. Without this a cancelled request stays status='pending'
        # forever and would be resurrected by rehydration on the next restart.
        if db_mgr.db_available:
            try:
                db_mgr.end_session(queue_id_to_remove, user_id, system_end=False,
                                   end_reason='user_cancelled')
            except Exception as exc:
                logger.warning("Could not close cancelled queue row %s: %s",
                               queue_id_to_remove, exc)

        return True, "success"
