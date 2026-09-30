import datetime
import html
import logging
import random
import uuid
from typing import List, Optional, Tuple
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
import config
from config import (
    CB_DIRECT_ACCEPT,
    CB_DIRECT_DECLINE,
    CB_PICK_CANCEL,
    CB_PICK_LIST,
    CB_PICK_OPEN,
    MESSAGES,
    used_anonymous_ids,
    ServiceType,
    UserState,
    directed_by_member,
    get_service,
    is_supporter_available,
    picker_views,
    queue_entries,
    supporter_label,
    queue_order,
    user_states,
    user_to_queue_map,
)
from src.timeutil import ensure_aware_utc, format_duration_minutes, format_hhmm, utcnow
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
                     service_key: str = ServiceType.HF.value, routing: str = "open") -> str:
        """Add user to the help queue for the given service.

        routing='open' is today's behaviour exactly: the request goes into
        queue_order and gets a channel post with a Claim button. routing='choosing'
        means the requester is being asked whether they want a specific supporter,
        so the request exists and is durable but is NOT in the channel and NOT in
        queue_order -- a queue POSITION is meaningless for a request nobody outside
        this conversation can see.
        """
        queue_id = str(uuid.uuid4())
        anonymous_id = self._generate_anonymous_id(get_service(service_key).anon_prefix)

        # ONE `now` for both clocks, so a freshly created request cannot look like it
        # has already been waiting.
        now = utcnow()

        queue_entries[queue_id] = {
            'user_id': user_id,
            'description': description,
            'created_at': now,
            # The wait budget. Reset on every hand-back to the requester; NEVER reset
            # by the open lane, which is what keeps open-queue timing unchanged.
            'waiting_since': now,
            'anonymous_id': anonymous_id,
            'message_id': None,  # Will be set after posting to channel
            'channel_id': None,  # ditto -- the channel the post actually landed in
            'service': service_key,
            'routing': routing,
            'target_member_id': None,
            'directed_at': None,
            'directed_message_id': None,
            'notice_channel_id': None,
            'notice_message_id': None,
            'declined_by': [],
        }

        # Maintain O(1) lookup indices
        user_to_queue_map[user_id] = queue_id
        if routing == 'open':
            queue_order.append(queue_id)

        # Create pending session in database using queue_id as session_id
        if db_mgr.db_available:
            db_mgr.create_session(user_id, description, anonymous_id, queue_id, user_telehandle, service=service_key, routing=routing)

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
            # A directed request is owned ENTIRELY by sweep_directed_requests(): it has
            # its own, much longer clock (directed_at + directed_response_minutes) and
            # its own stale horizon. Expiring it here would close it out from under the
            # supporter who is still looking at the DM.
            if (entry.get('routing') or 'open') == 'directed':
                continue
            # waiting_since, not created_at: a request handed back to the requester
            # gets a fresh window, or a decline at 23h59m produces "choose again" and
            # "your request expired" one sweep apart. Legacy entries have no
            # waiting_since and fall back to created_at, so the open lane is unchanged.
            created = ensure_aware_utc(entry.get('waiting_since') or entry.get('created_at'))
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
                # CHOOSING_SUPPORTER counts too: someone who never finished picking is
                # still owed the news that their request is gone.
                if user_states.get(user_id) in (UserState.IN_QUEUE,
                                                UserState.CHOOSING_SUPPORTER):
                    user_states[user_id] = UserState.IDLE
                    notify = True
                if user_id in user_to_queue_map:
                    del user_to_queue_map[user_id]
                picker_views.pop(user_id, None)

            queue_entries.pop(queue_id, None)
            if queue_id in queue_order:
                queue_order.remove(queue_id)

            expired_entries.append({**entry, 'queue_id': queue_id, 'notify': notify,
                                    'routing': entry.get('routing') or 'open'})

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

            # 3. Notify the requester. queue_expired says "no {member} was available
            #    in time", which is simply false for a request that never reached
            #    anybody, so a 'choosing' request gets its own wording.
            if entry.get('notify') and entry.get('user_id'):
                try:
                    text = (MESSAGES["choosing_expired"]
                            if entry.get('routing') == 'choosing'
                            else MESSAGES["queue_expired"].format(member=svc.member_label))
                    await self.bot.send_message(
                        chat_id=entry['user_id'],
                        text=text,
                    )
                except Exception as exc:
                    logger.warning("Failed to notify user %s about queue expiry: %s",
                                   entry['user_id'], exc)

            acted.append(entry)

        return acted

    async def _retire_channel_post(self, entry: dict, svc) -> None:
        """Edit an expired request's channel post into an EXPIRED card and strip the
        Claim button, falling back to deleting it. NEVER raises."""
        # Stated, not inferred: only the open lane ever HAS a channel post with a Claim
        # button. A 'choosing' request was never posted, and a 'directed' one has only
        # the no-detail note, which _update_directed_notice owns.
        if (entry.get('routing') or 'open') != 'open':
            return
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

    
    # ==================================================================
    # Directed support (Phase 5)
    # ==================================================================

    @staticmethod
    def _directed_notice_text(member_name: Optional[str], tail: str = "") -> str:
        """The channel note, which carries NO description, NO claim button and NO
        anonymous id.

        Omitting the anonymous id is deliberate and is the whole reason this note is
        safe: with it, the channel could correlate this note against a later
        open-queue post of the SAME request and read off "X was asked and now it's
        open" -- i.e. that X said no. That is exactly what must never be surfaced.
        """
        who = html.escape(member_name) if member_name else "a supporter"
        return f"A request was sent directly to {who}{tail}"

    async def _post_directed_notice(self, queue_id: str, entry: dict, svc,
                                    member_name: str) -> None:
        """Tell the channel that SOMETHING was routed, and nothing more. NEVER raises."""
        try:
            if not self.channel_accessible.get(svc.key, True) or not svc.channel_id:
                return
            text = self._directed_notice_text(member_name,
                                              f" - {format_hhmm(utcnow())}")
            message = await self.bot.send_message(
                chat_id=svc.channel_id,
                text=text,
                parse_mode='HTML',
            )
            entry['notice_channel_id'] = svc.channel_id
            entry['notice_message_id'] = getattr(message, 'message_id', None)
            # Kept in memory only. A restarted bot can still EDIT the note (it has the
            # coordinates from Mongo) but renders it without the name rather than
            # persisting a curated display name onto the session document.
            entry['notice_member_name'] = member_name
            if db_mgr.db_available and entry['notice_message_id'] is not None:
                try:
                    db_mgr.set_directed_notice(queue_id, svc.channel_id,
                                               entry['notice_message_id'])
                except Exception as exc:
                    logger.warning("Could not persist directed notice: %s", exc)
        except Exception as exc:
            logger.warning("Could not post the directed notice for service %s: %s",
                           getattr(svc, 'key', '?'), exc)

    async def _update_directed_notice(self, entry: dict, outcome: str) -> None:
        """Edit the channel note in place. NEVER raises.

        ONE wording covers every non-accept outcome -- decline, lapse, cancel,
        reroute, release. "Declined" is never written to the channel: the channel is
        the supporter's own peers, and naming a decline there invites exactly the
        social pressure this feature exists to avoid.
        """
        try:
            message_id = entry.get('notice_message_id')
            channel_id = entry.get('notice_channel_id')
            if not message_id or not channel_id:
                return
            tail = (f" - accepted {format_hhmm(utcnow())}" if outcome == 'accepted'
                    else " - no longer waiting")
            await self.bot.edit_message_text(
                chat_id=channel_id,
                message_id=message_id,
                text=self._directed_notice_text(entry.get('notice_member_name'), tail),
                reply_markup=None,
                parse_mode='HTML',
            )
        except Exception as exc:
            logger.warning("Could not update the directed notice: %s", exc)

    async def _strip_directed_buttons(self, entry: dict, member_id: Optional[int]) -> None:
        """Take Accept / Not right now off the supporter's DM. NEVER raises."""
        try:
            message_id = entry.get('directed_message_id')
            target = member_id if member_id is not None else entry.get('target_member_id')
            if not message_id or target is None:
                return
            await self.bot.edit_message_reply_markup(
                chat_id=target, message_id=message_id, reply_markup=None)
        except Exception as exc:
            logger.warning("Could not strip the directed request buttons: %s", exc)

    @staticmethod
    def _still_directed_at(queue_id: str, entry: dict, member_int: int) -> bool:
        """True iff `entry` is still THE live entry for queue_id AND still waits on
        member_int.

        send_directed_request asks this after every await. Each await is a window in
        which the requester's own buttons keep working -- the list message still
        carries 'Send to anyone instead' and 'Cancel' -- and, once the DM has landed,
        in which the target can Accept or decline. Whatever won that window already
        moved the request on and owns it; code after the await must not write over it.

        MEMORY is the witness, not a Mongo return value. Every transition out of
        'directed' (undirect, accept_directed, remove_from_queue, the sweep) changes
        memory with no await between its Mongo write and its memory write, so between
        awaits memory cannot disagree with Mongo. undirect_session's None cannot serve:
        it also means "Mongo raised", and then rolling memory back is still right.
        """
        return (queue_entries.get(queue_id) is entry
                and entry.get('routing') == 'directed'
                and entry.get('target_member_id') == member_int)

    async def send_directed_request(self, queue_id: str, member_id: int) -> str:
        """Route ONE request to ONE supporter. Returns 'ok' | 'gone' | 'busy' | 'unreachable'.

        The order below IS the exactly-once gate. Everything that can refuse happens
        first, then the atomic Mongo transition, then the in-memory transition, and
        only THEN the first await. Two taps on the same name both reach
        direct_session; exactly one gets a document back; exactly one DM is sent.

        After EVERY await it re-checks _still_directed_at; losing that check returns
        'gone' and writes nothing over the winner.
        """
        entry = queue_entries.get(queue_id)
        if entry is None:
            return 'gone'
        if (entry.get('routing') or 'open') != 'choosing':
            return 'gone'

        svc = get_service(entry.get('service'))
        # KILL SWITCH: a picker button rendered before PSS_DIRECTED_ENABLED was
        # turned off must not DM anyone.
        if not svc.directed_enabled:
            return 'busy'
        try:
            member_int = int(member_id)
        except (TypeError, ValueError):
            return 'busy'

        declined = set(entry.get('declined_by') or [])
        if member_int in declined or not is_supporter_available(svc.key, member_int):
            return 'busy'
        if member_int == entry.get('user_id'):
            return 'busy'

        # --- atomic, synchronous, BEFORE any await -------------------------------
        if db_mgr.db_available:
            doc = db_mgr.direct_session(queue_id, member_int)
            if not doc:
                logger.info("Directed request %s was already routed elsewhere; "
                            "sending nothing", queue_id)
                return 'gone'

        entry['routing'] = 'directed'
        entry['target_member_id'] = member_int
        entry['directed_at'] = utcnow()
        entry['directed_message_id'] = None
        directed_by_member[member_int] = queue_id
        # IN_QUEUE, not CHOOSING_SUPPORTER: the choosing is over and they ARE now
        # waiting, on one named person. This MUST match what restore._restore_pending
        # sets for a 'directed' row, or the requester's state silently changes across
        # a restart -- and while it is CHOOSING_SUPPORTER, anything they type is read
        # as a picker number against a view that no longer exists.
        requester_id = entry.get('user_id')
        if requester_id:
            user_states[requester_id] = UserState.IN_QUEUE
        # ------------------------------------------------------------------------

        member_name = supporter_label(svc.key, member_int)

        description = entry.get('description') or ''
        text = MESSAGES["directed_request"].format(
            description=html.escape(description[:500]),
            window=format_duration_minutes(svc.directed_response_minutes),
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(MESSAGES["directed_accept_button"],
                                 callback_data=f"{CB_DIRECT_ACCEPT}:{queue_id}"),
            InlineKeyboardButton(MESSAGES["directed_decline_button"],
                                 callback_data=f"{CB_DIRECT_DECLINE}:{queue_id}"),
        ]])

        try:
            message = await self.bot.send_message(
                chat_id=member_int, text=text, reply_markup=keyboard, parse_mode='HTML')
        except Exception as exc:
            # ROLL EVERYTHING BACK. A row left 'directed' at a supporter who was never
            # messaged burns 24 hours of silence on a person in distress.
            logger.warning("Could not DM directed request %s to member %s: %s",
                           queue_id, member_int, exc)
            if self._still_directed_at(queue_id, entry, member_int):
                if db_mgr.db_available:
                    try:
                        db_mgr.undirect_session(queue_id, member_int, reason='unreachable')
                    except Exception as inner:
                        logger.warning("Rollback of directed request %s also failed: %s",
                                       queue_id, inner)
                entry['routing'] = 'choosing'
                entry['target_member_id'] = None
                entry['directed_at'] = None
                entry['directed_message_id'] = None
                # undirect_session also reset waiting_since; mirror it or memory expires
                # this request on a clock Mongo no longer agrees with.
                entry['waiting_since'] = utcnow()
                directed_by_member.pop(member_int, None)
                # Keep memory in step with what undirect_session just wrote, so the
                # re-rendered picker does not offer the same unreachable person again.
                if member_int not in declined:
                    entry['declined_by'] = list(entry.get('declined_by') or []) + [member_int]
                # AND PUT THE REQUESTER BACK. The state was moved to IN_QUEUE before the
                # await; rolling the row back to 'choosing' without rolling this back
                # leaves them staring at a re-rendered picker that says "reply with its
                # number" while handle_message answers every number they type with the
                # generic unknown-command string. The buttons keep working, so nothing
                # looks broken -- it just silently stops listening. Every other
                # directed -> choosing path (undirect) restores this; so must the rollback.
                if requester_id:
                    user_states[requester_id] = UserState.CHOOSING_SUPPORTER
                outcome = 'unreachable'
            else:
                # Something the requester did while this DM was in flight -- 'Send to
                # anyone instead' or Cancel -- already moved the request on, and it owns
                # the row and the requester's state now. Rolling back to 'choosing' over
                # it strands a live channel post that every Claim refuses ("sent to a
                # specific supporter") while Mongo says 'open', or puts somebody who
                # just cancelled back on a picker for a request that no longer exists.
                # Touch nothing but our own index entry.
                logger.info("Directed request %s moved on while its DM to member %s "
                            "was in flight; leaving it where it is", queue_id, member_int)
                if directed_by_member.get(member_int) == queue_id:
                    directed_by_member.pop(member_int, None)
                outcome = 'gone'

            # Unconditional: this is a fact about the member, not about the request.
            lowered = str(exc).lower()
            if ("bot can't initiate" in lowered
                    or "can't initiate conversation" in lowered
                    or "blocked" in lowered
                    or "forbidden" in lowered):
                # Self-healing: they drop out of the picker now and come back the
                # moment they message the bot again.
                logger.warning("Member %s cannot be messaged by the bot; clearing "
                               "has_started_bot", member_int)
                if db_mgr.db_available:
                    try:
                        db_mgr.mark_member_started(member_int, False,
                                                   collection=svc.members_collection)
                    except Exception as inner:
                        logger.warning("Could not clear has_started_bot for %s: %s",
                                       member_int, inner)
                svc.roster.mark_started(member_int, False)
            return outcome

        message_id = getattr(message, 'message_id', None)
        if not self._still_directed_at(queue_id, entry, member_int):
            # The DM landed, but the requester moved the request on while it was in
            # flight. undirect / remove_from_queue ran while directed_message_id was
            # still None, so they had no buttons to strip: retire THIS DM's buttons
            # here -- the same tidy-up a cancelled request gives its supporter -- and
            # post NO channel note. The request is already in the channel or gone;
            # a note would tell the channel it was sent to this person.
            logger.info("Directed request %s moved on while its DM to member %s was "
                        "in flight; retiring that DM's buttons", queue_id, member_int)
            await self._strip_directed_buttons({'directed_message_id': message_id},
                                               member_int)
            return 'gone'

        entry['directed_message_id'] = message_id
        if db_mgr.db_available and entry['directed_message_id'] is not None:
            try:
                db_mgr.set_directed_message(queue_id, entry['directed_message_id'])
            except Exception as exc:
                logger.warning("Could not persist directed message id for %s: %s",
                               queue_id, exc)

        # Best-effort, never fatal, and always AFTER the DM.
        await self._post_directed_notice(queue_id, entry, svc, member_name)
        if not self._still_directed_at(queue_id, entry, member_int):
            # An Accept, a 'Not right now', a 'Send to anyone' or a Cancel ran while the
            # note was being posted. Each of them edits the note -- but found none to
            # edit yet. Close it now with the wording that outcome would have used
            # (never "declined"), and do not tell the requester 'directed_sent': the
            # winner has already told them where they stand.
            await self._update_directed_notice(
                entry, 'accepted' if entry.get('accepted') else 'closed')
            return 'gone'
        return 'ok'

    async def accept_directed(self, queue_id: str, member_id: int, member_name: str = None,
                              member_telehandle: str = None) -> Tuple[str, Optional[int]]:
        """The targeted supporter, and ONLY the targeted supporter, takes the request.

        Separate from claim_queue because the AUTHORIZATION RULE is different:
        claim_queue authorizes against is_member_of_service -- the entire roster --
        which is exactly wrong here. It REUSES db_mgr.claim_session because the atomic
        win is the same one, and a second claim path is a second place for
        exactly-once to be wrong.
        """
        entry = queue_entries.get(queue_id)
        if entry is None:
            return ('gone', None)

        try:
            member_int = int(member_id)
        except (TypeError, ValueError):
            return ('not_yours', None)

        if ((entry.get('routing') or 'open') != 'directed'
                or entry.get('target_member_id') != member_int):
            return ('not_yours', None)

        user_id = entry.get('user_id')
        if user_id == member_int:
            raise SelfClaimError("Claimant cannot take their own queue entry")

        if db_mgr.db_available:
            if not db_mgr.claim_session(queue_id, member_int, member_telehandle):
                logger.info("Directed request %s was already claimed or closed; "
                            "ignoring accept by %s", queue_id, member_int)
                return ('gone', None)

        # Synchronously, before the first await -- the same reason claim_queue does it.
        queue_entries.pop(queue_id, None)
        # Marks the now-orphaned entry, so a channel note that send_directed_request
        # is still posting closes as 'accepted' rather than 'no longer waiting'.
        entry['accepted'] = True
        if user_id in user_to_queue_map:
            del user_to_queue_map[user_id]
        directed_by_member.pop(member_int, None)
        picker_views.pop(user_id, None)
        if queue_id in queue_order:
            queue_order.remove(queue_id)

        await self._strip_directed_buttons(entry, member_int)
        await self._update_directed_notice(entry, 'accepted')
        return ('ok', user_id)

    async def undirect(self, queue_id: str, member_id: int, reason: str,
                       notify_member: bool = True) -> bool:
        """directed -> choosing. True IFF THIS CALLER WON the transition.

        A decline and a 24-hour lapse are the same transition, so they share one
        implementation. Only a caller that gets True back may message the requester;
        losing this race must be completely silent, or two actors both tell the same
        person their supporter isn't free.
        """
        entry = queue_entries.get(queue_id)
        if entry is None:
            return False

        try:
            member_int = int(member_id)
        except (TypeError, ValueError):
            return False

        if ((entry.get('routing') or 'open') != 'directed'
                or entry.get('target_member_id') != member_int):
            return False

        # Atomic, synchronous, pre-await. When Mongo is unavailable, the memory check
        # above is the gate.
        if db_mgr.db_available:
            if not db_mgr.undirect_session(queue_id, member_int, reason=reason):
                logger.info("Directed request %s had already moved on; staying quiet",
                            queue_id)
                return False

        message_id = entry.get('directed_message_id')
        entry['routing'] = 'choosing'
        entry['target_member_id'] = None
        entry['directed_at'] = None
        # A FRESH window. Without this a request lapsing at 23h59m would produce
        # "they're not free, pick again" and "your request expired" one sweep apart.
        entry['waiting_since'] = utcnow()
        declined = list(entry.get('declined_by') or [])
        if member_int not in declined:
            declined.append(member_int)
        entry['declined_by'] = declined
        directed_by_member.pop(member_int, None)
        requester = entry.get('user_id')
        if requester:
            user_states[requester] = UserState.CHOOSING_SUPPORTER

        await self._strip_directed_buttons({'directed_message_id': message_id},
                                           member_int)
        entry['directed_message_id'] = None
        await self._update_directed_notice(entry, 'closed')

        if notify_member and reason == 'timeout':
            # Otherwise their DM sits looking live forever.
            try:
                await self.bot.send_message(chat_id=member_int,
                                            text=MESSAGES["directed_lapsed_member"])
            except Exception as exc:
                logger.warning("Could not tell member %s their request lapsed: %s",
                               member_int, exc)
        return True

    async def _offer_next_step(self, entry: dict, note_key: str, name: str = "") -> None:
        """DM the requester the "what next?" prompt. NEVER raises."""
        user_id = entry.get('user_id')
        if not user_id:
            return
        note = MESSAGES[note_key]
        if "{name}" in note:
            note = note.format(name=name or "They")
        text = note + "\n\n" + MESSAGES["next_step_question"]
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(MESSAGES["picker_choose_else_button"],
                                  callback_data=f"{CB_PICK_LIST}:0")],
            [InlineKeyboardButton(MESSAGES["picker_anyone_button"],
                                  callback_data=CB_PICK_OPEN)],
            [InlineKeyboardButton(MESSAGES["picker_cancel_button"],
                                  callback_data=CB_PICK_CANCEL)],
        ])
        try:
            await self.bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard)
        except Exception as exc:
            logger.warning("Could not offer the next step to %s: %s", user_id, exc)

    async def sweep_directed_requests(self) -> List[dict]:
        """Hand back requests one supporter has sat on for too long.

        THE STALE HORIZON IS THE POINT. Without it, the first boot after a multi-day
        outage walks every mid-request row and DMs every one of those people, months
        after the fact. This is the twin of restore._restore_pending's horizon and of
        expiry._expire_session's, and it is the single most dangerous send path added
        by this feature.
        """
        now = utcnow()
        acted: List[dict] = []

        for queue_id, entry in list(queue_entries.items()):
            if (entry.get('routing') or 'open') != 'directed':
                continue

            svc = get_service(entry.get('service'))
            target = entry.get('target_member_id')

            directed = ensure_aware_utc(entry.get('directed_at'))
            if directed is None or directed > now:
                # Unusable clock. REPAIR IT AND WAIT -- exactly what
                # restore._restore_pending does with the same bad value, and for the
                # same reason.
                #
                # The two obvious alternatives are both wrong. Treating it as
                # infinitely old lands in the silent branch below and DESTROYS a live
                # request: the requester is told nothing, ever, and one bad write
                # across many rows quietly deletes all of them. Treating it as
                # infinitely old but notifying is worse still -- that is messaging on
                # a timestamp we know we cannot trust, which is the exact failure this
                # sweep exists to prevent.
                #
                # Failing toward waiting sends nobody anything now, keeps the request
                # alive, and lets it resolve normally one window from here. It cannot
                # loop: the repaired value is a real timestamp that ages.
                logger.warning("Directed request %s has an unusable directed_at (%r); "
                               "treating it as just sent rather than messaging on a "
                               "clock we cannot trust", queue_id,
                               entry.get('directed_at'))
                entry['directed_at'] = now
                continue

            idle = (now - directed).total_seconds() / 60.0

            if idle < svc.directed_response_minutes:
                continue

            stale = idle > (svc.directed_response_minutes
                            + config.STALE_NOTIFY_GRACE_MINUTES)

            if stale:
                won = True
                if db_mgr.db_available:
                    try:
                        won = db_mgr.end_session(queue_id, None, system_end=True,
                                                 end_reason='stale_startup_sweep')
                    except Exception as exc:
                        logger.warning("Could not close stale directed row %s: %s",
                                       queue_id, exc)
                        won = False
                if not won:
                    continue

                requester = entry.get('user_id')
                queue_entries.pop(queue_id, None)
                if requester in user_to_queue_map:
                    del user_to_queue_map[requester]
                if queue_id in queue_order:
                    queue_order.remove(queue_id)
                if target is not None:
                    directed_by_member.pop(target, None)
                picker_views.pop(requester, None)
                if requester:
                    user_states[requester] = UserState.IDLE

                await self._strip_directed_buttons(entry, target)
                await self._update_directed_notice(entry, 'closed')
                # AND MESSAGE NOBODY.
                logger.info("Directed request %s was %.0f min old (> %d min horizon); "
                            "closed silently", queue_id, idle,
                            svc.directed_response_minutes
                            + config.STALE_NOTIFY_GRACE_MINUTES)
                acted.append({**entry, 'queue_id': queue_id, 'outcome': 'stale'})
                continue

            if target is None:
                continue

            name = supporter_label(svc.key, target)

            if await self.undirect(queue_id, target, reason='timeout'):
                # Only the winner speaks to the requester.
                await self._offer_next_step(entry, "directed_unavailable", name)
                acted.append({**entry, 'queue_id': queue_id, 'outcome': 'lapsed'})

        return acted

    def rebuild_released_entry(self, doc: dict, routing: str) -> str:
        """Rebuild the in-memory queue entry for a request handed back by its member.

        The SAME session_id and the SAME anonymous_user_id: the channel must see the
        RHesident #NNNN it already knows, or a released request reads as a brand new
        person asking for help.
        """
        session_id = doc['session_id']
        svc = get_service(doc.get('service'))
        anon = (doc.get('anonymous_user_id')
                or self._generate_anonymous_id(svc.anon_prefix))
        used_anonymous_ids.add(anon)
        user_id = doc.get('user_id')

        queue_entries[session_id] = {
            'user_id': user_id,
            # The description exists ONLY on the document -- active_sessions has never
            # carried one -- which is why /release requires Mongo at all.
            'description': doc.get('description') or '',
            'created_at': ensure_aware_utc(doc.get('created_at')) or utcnow(),
            'waiting_since': ensure_aware_utc(doc.get('waiting_since')) or utcnow(),
            'anonymous_id': anon,
            'message_id': None,
            'channel_id': None,
            'service': svc.key,
            'routing': routing,
            'target_member_id': None,
            'directed_at': None,
            'directed_message_id': None,
            'notice_channel_id': None,
            'notice_message_id': None,
            'declined_by': list(doc.get('declined_by') or []),
            'released': True,
        }
        if user_id is not None:
            user_to_queue_map[user_id] = session_id
        if routing == 'open' and session_id not in queue_order:
            queue_order.append(session_id)
        return session_id

    async def route_to_open_queue(self, queue_id: str) -> str:
        """The requester chose to ask anyone who's free.

        Allowed from 'directed' as well as 'choosing', and deliberately so: the
        alternative locks somebody in distress behind one person's 24-hour silence
        with no exit but /cancel and retyping everything. It is their own choice, so
        it is not the automatic reroute that must never happen.

        Returns 'ok' | 'already_open' | 'gone' | 'post_failed'. ONLY 'post_failed'
        is a fault. 'already_open' is a double tap, or a tap on an older
        fork/list/next-step message whose request is already in the channel, and
        must never be reported to a student as an outage. NOT a bool: every result
        is a non-empty string, so never test it for truthiness.
        """
        entry = queue_entries.get(queue_id)
        if entry is None:
            return 'gone'

        routing = entry.get('routing') or 'open'
        if routing == 'open':
            # A repeat tap: the request is already posted and working.
            return 'already_open'
        if routing not in ('choosing', 'directed'):
            return 'gone'

        if routing == 'directed':
            target = entry.get('target_member_id')
            if target is None:
                return 'gone'
            if not await self.undirect(queue_id, target, reason='rerouted',
                                       notify_member=False):
                # We lost the race to an accept or a lapse. Its winner has already
                # messaged the requester, so do not post; the caller shows status.
                return 'gone'

        # Before the first await: a double tap finds routing already 'open' and bails.
        entry['routing'] = 'open'

        # POST FIRST, FLIP MONGO SECOND. The reverse can leave a live 'open' row with
        # no channel post -- invisible to every member until it expires, i.e. a person
        # waiting for a message that never comes.
        posted, _error_type = await self.post_queue_to_channel(queue_id)
        if not posted:
            entry['routing'] = 'choosing'
            return 'post_failed'

        if db_mgr.db_available:
            try:
                if not db_mgr.open_session(queue_id):
                    # Not fatal: claim_session filters on status, not routing, so the
                    # request still works. Only rehydration would get it wrong.
                    logger.warning("Could not flip session %s to routing 'open'", queue_id)
            except Exception as exc:
                logger.warning("Error flipping session %s to routing 'open': %s",
                               queue_id, exc)

        entry['waiting_since'] = utcnow()
        if queue_id not in queue_order:
            queue_order.append(queue_id)

        requester = entry.get('user_id')
        if requester:
            user_states[requester] = UserState.IN_QUEUE
            picker_views.pop(requester, None)
            try:
                await self.bot.send_message(chat_id=requester,
                                            text=MESSAGES["queue_added"])
            except Exception as exc:
                logger.warning("Could not confirm the open queue to %s: %s",
                               requester, exc)
        return 'ok'

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
        #
        # The open lane deliberately IGNORES this return value and keeps its ordering
        # exactly as it was: the only person messaged is the requester, and they are
        # the one who just pressed cancel.
        closed = True
        if db_mgr.db_available:
            closed = False
            try:
                closed = db_mgr.end_session(queue_id_to_remove, user_id, system_end=False,
                                            end_reason='user_cancelled')
            except Exception as exc:
                logger.warning("Could not close cancelled queue row %s: %s",
                               queue_id_to_remove, exc)

        # Directed tail. This one IS gated on winning the close, because it touches a
        # THIRD PARTY's chat: if a supporter accepted in the same instant, end_session
        # returns False and their live conversation must not have its DM defaced.
        if closed and (queue_entry.get('routing') or 'open') == 'directed':
            target = queue_entry.get('target_member_id')
            if target is not None:
                directed_by_member.pop(target, None)
            await self._strip_directed_buttons(queue_entry, target)
            await self._update_directed_notice(queue_entry, 'closed')

        picker_views.pop(user_id, None)

        return True, "success"
