import html
import logging
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackContext
from config import (
    CB_DIRECT_ACCEPT,
    closing_text,
    CB_DIRECT_DECLINE,
    CB_PICK_CANCEL,
    CB_PICK_LIST,
    CB_PICK_OPEN,
    CB_PICK_SELECT,
    SERVICES,
    UserState,
    available_supporters,
    picker_views,
    user_states,
    user_to_queue_map,
    user_to_service_map,
    MESSAGES,
    PHOTO_SHARING_ENABLED,
    is_any_member,
    is_member_of_service,
    enabled_services,
    default_service_key,
    get_service,
    help_request_text,
)
from src.bot.managers.session import SessionManager
from src.bot.managers.queue import QueueManager, SelfClaimError
from src.database.manager import db_mgr
from src.timeutil import utcnow

logger = logging.getLogger(__name__)

class BotHandlers:
    def __init__(self, session_manager: SessionManager, queue_manager: QueueManager):
        self.session_manager = session_manager
        self.queue_manager = queue_manager
    
    @staticmethod
    def _is_private_chat(update) -> bool:
        """True only for a genuine one-to-one chat with the bot.

        A callback tapped on a CHANNEL post does not prove a private chat exists, and
        has_started_bot is precisely the claim "we are allowed to DM this person".
        getattr, not attribute access: the existing test fakes have no effective_chat
        at all, and a boot-time AttributeError here would take the bot down.
        """
        chat = getattr(update, "effective_chat", None)
        return chat is not None and getattr(chat, "type", None) == "private"

    def _record_bot_started(self, user_id: int) -> None:
        """Record that this member has an open chat with the bot.

        Mongo FIRST, memory second: the five-minute roster refresh replaces the
        in-memory profile map wholesale from Mongo, so a memory-only write is
        silently reverted within minutes and the supporter never becomes pickable.

        The in-memory short-circuit keeps this a dict lookup rather than a Mongo
        write on every single message a supporter sends.
        """
        for svc in SERVICES.values():
            if user_id not in svc.roster:
                continue
            profile = svc.roster.profile(user_id)
            if profile is not None and profile.has_started_bot:
                continue
            try:
                db_mgr.mark_member_started(user_id, True,
                                           collection=svc.members_collection)
            except Exception as exc:
                logger.warning("Could not record bot-started for member %s (%s): %s",
                               user_id, svc.key, exc)
            svc.roster.mark_started(user_id, True)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        user_states[user_id] = UserState.IDLE

        text = MESSAGES["welcome"]
        # /available, /unavailable and /release are member-only, so they are
        # deliberately NOT in set_my_commands -- that menu is the requester's
        # surface. This addendum is how a supporter discovers them instead.
        if self._is_private_chat(update) and is_any_member(user_id):
            self._record_bot_started(user_id)
            text = text + "\n\n" + MESSAGES["member_addendum"]

        await update.message.reply_text(text)

    async def available_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Member-only: appear on the list requesters choose from."""
        await self._set_availability(update, True)

    async def unavailable_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Member-only: stay off that list. Does NOT end an existing conversation and
        does NOT hand back a directed request already sitting with them."""
        await self._set_availability(update, False)

    async def _set_availability(self, update: Update, available: bool) -> None:
        user_id = update.effective_user.id

        svc = next((s for s in SERVICES.values() if user_id in s.roster), None)
        if svc is None:
            # The SAME neutral string handle_message sends for gibberish. Confirming
            # the command exists would tell a stranger who is on the roster.
            await update.message.reply_text(MESSAGES["unknown_command"])
            return

        suffix = ""
        if db_mgr.db_available:
            written = False
            try:
                written = db_mgr.set_member_availability(
                    user_id, available, collection=svc.members_collection)
            except Exception as exc:
                logger.warning("Could not persist availability for member %s: %s",
                               user_id, exc)
            if not written:
                # Memory is deliberately NOT changed: a toggle the next roster
                # refresh silently reverts is worse than a visible failure, because
                # the supporter believes they are off the list and is not.
                await update.message.reply_text(MESSAGES["availability_failed"])
                return
            svc.roster.set_available(user_id, available)
        else:
            svc.roster.set_available(user_id, available)
            suffix = "\n\n" + MESSAGES["availability_memory_only"]

        profile = svc.roster.profile(user_id)
        if profile is None or not profile.display_name:
            # The toggle was honoured; they just have nothing to render yet.
            await update.message.reply_text(MESSAGES["availability_needs_profile"] + suffix)
            return

        text = MESSAGES["now_available"] if available else MESSAGES["now_unavailable"]
        await update.message.reply_text(text + suffix)
    
    async def chat_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /chat (and its /help alias) to start a support request."""
        user_id = update.effective_user.id
        current_state = user_states.get(user_id, UserState.IDLE)
        
        # Check if user is already in queue or conversation
        if current_state == UserState.IN_QUEUE:
            await update.message.reply_text(MESSAGES["already_in_queue"])
            return
        
        if current_state == UserState.IN_CONVERSATION:
            await update.message.reply_text(MESSAGES["already_in_conversation"])
            return
        
        # Check if user already has an active session
        existing_session = self.session_manager.get_session_by_user(user_id)
        if existing_session:
            await update.message.reply_text(MESSAGES["already_in_conversation"])
            return

        # An INDEX-based check, immune to user_states drift. Without it a requester
        # sitting at the picker (CHOOSING_SUPPORTER, which the IN_QUEUE test above
        # does not catch) can start a SECOND request -- two pending rows for one user,
        # which rehydration then treats as duplicate_pending and silently closes one.
        if self.queue_manager.is_user_in_queue(user_id):
            await update.message.reply_text(self._open_request_status_text(user_id))
            return
        
        # Landing page: if more than one support track is available, let the user
        # choose. With a single runnable service (Phase 1 = HF only) we skip the
        # chooser entirely so behaviour is identical to before.
        svcs = enabled_services()
        if len(svcs) <= 1:
            # Resolve the key ONCE: the prompt must describe the same track we just
            # recorded against this user.
            key = default_service_key()
            user_to_service_map[user_id] = key
            user_states[user_id] = UserState.WAITING_FOR_DESCRIPTION
            await update.message.reply_text(help_request_text(key))
            return

        user_states[user_id] = UserState.WAITING_FOR_SERVICE
        keyboard = [
            [InlineKeyboardButton(s.display_name, callback_data=f"svc_{s.key}")]
            for s in svcs
        ]
        await update.message.reply_text(
            MESSAGES["choose_service"],
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Back-compat alias: /help is kept alive for posters and existing users."""
        await self.chat_command(update, context)
    
    async def end_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /end. REQUESTER-ONLY: a supporter hands back with /release instead.

        Ending is the requester's decision. A supporter who has to stop should not be
        able to close the conversation out from under someone who is still talking --
        /release keeps their request alive and finds them somebody else.
        """
        user_id = update.effective_user.id

        session_id = self.session_manager.get_session_by_user(user_id)
        if not session_id:
            await update.message.reply_text(MESSAGES["no_active_conversation"])
            return

        # Capture the roles NOW: end_session deletes the in-memory session, and after
        # that there is nothing left to ask who was who. `info` can legitimately be
        # None -- an orphan whose rehydration was skipped but whose index survived --
        # in which case there is no conversation to end.
        info = self.session_manager.get_session_info(session_id)
        if info is None:
            await update.message.reply_text(MESSAGES["no_active_conversation"])
            return
        service_key = info.get('service')
        requester_id = info.get('user_id')
        member_id = info.get('heartfelt_member_id')

        if user_id != requester_id:
            # Refused WITHOUT ending: the session survives untouched.
            await update.message.reply_text(MESSAGES["end_is_requester_only"])
            return

        await self.session_manager.end_session(session_id, user_id)

        if requester_id:
            user_states[requester_id] = UserState.IDLE
        if member_id:
            user_states[member_id] = UserState.IDLE

        # Copy chosen by SESSION ROLE, not by roster lookup. A roster lookup gets this
        # wrong for a supporter who is themselves the requester in this conversation,
        # and it is one dict read away from being right.
        #
        # DELIBERATELY SCOPED TO /end. SessionExpiryManager._expire_session keeps its
        # is_heartfelt_member() check: expiry copy is a different message with a
        # different brief, and changing it here would churn test_session_expiry.py for
        # no behavioural gain. The two differ on purpose.
        if requester_id:
            try:
                await context.bot.send_message(
                    chat_id=requester_id,
                    text=closing_text(service_key, for_member=False),
                )
            except:
                pass

        if member_id and member_id != requester_id:
            try:
                await context.bot.send_message(
                    chat_id=member_id,
                    text=closing_text(service_key, for_member=True),
                )
            except:
                pass

    async def release_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /release: the SUPPORTER hands a live conversation back.

        Not end_session. The request is not over, so the row goes active -> pending
        and keeps its session_id and its anonymous id.
        """
        user_id = update.effective_user.id

        session_id = self.session_manager.get_session_by_user(user_id)
        if not session_id:
            await update.message.reply_text(MESSAGES["no_active_conversation"])
            return

        info = self.session_manager.get_session_info(session_id)
        if info is None:
            await update.message.reply_text(MESSAGES["no_active_conversation"])
            return
        requester_id = info.get('user_id')
        member_id = info.get('heartfelt_member_id')

        if user_id != member_id:
            await update.message.reply_text(MESSAGES["release_is_member_only"])
            return

        if not db_mgr.db_available:
            # The DESCRIPTION lives only on the document; active_sessions has never
            # carried one. Without Mongo the request could be handed back to nobody,
            # because there would be nothing to show them. Runbook R12.
            await update.message.reply_text(MESSAGES["release_unavailable"])
            return

        # Atomic active -> pending, synchronously, before the first await. None means
        # somebody already ended or moved this conversation, and the requester -- who
        # did not ask for any of this -- must hear nothing at all.
        doc = db_mgr.release_session(session_id, user_id)
        if doc is None:
            await update.message.reply_text(MESSAGES["no_active_conversation"])
            return

        self.session_manager.release_session(session_id)
        user_states[member_id] = UserState.IDLE

        # PROVENANCE, not routing. `routing` is never cleared on claim precisely so
        # this question can be asked: a requester who deliberately kept their message
        # OFF the channel does not consent to it going there because the supporter
        # they chose had to step away. See D28.
        provenance = doc.get('routing') or 'open'
        new_routing = 'open' if provenance == 'open' else 'choosing'

        queue_id = self.queue_manager.rebuild_released_entry(doc, new_routing)
        picker_views.pop(requester_id, None)

        if new_routing == 'open':
            posted, _error = await self.queue_manager.post_queue_to_channel(queue_id)
            user_states[requester_id] = UserState.IN_QUEUE
            try:
                await context.bot.send_message(chat_id=requester_id,
                                               text=MESSAGES["released_to_queue"])
            except Exception as exc:
                logger.warning("Could not tell %s their request went back to the "
                               "queue: %s", requester_id, exc)
            if not posted:
                # The row is 'pending', so the next boot rehydrates and re-posts it.
                # The requester is still correctly told they are waiting; the person
                # who needs to know the channel is broken is the member.
                logger.error("Released request %s could not be posted to the channel",
                             queue_id)
                await update.message.reply_text(MESSAGES["channel_error"])
                return
        else:
            # Persist the lane change too, or a restart rehydrates this as still
            # sitting with the supporter who just walked away.
            try:
                if db_mgr.undirect_session(queue_id, user_id, reason='released') is None:
                    logger.warning("Released request %s could not be moved back to "
                                   "'choosing' in Mongo", queue_id)
            except Exception as exc:
                logger.warning("Error moving released request %s back to 'choosing': "
                               "%s", queue_id, exc)
            user_states[requester_id] = UserState.CHOOSING_SUPPORTER
            try:
                await context.bot.send_message(
                    chat_id=requester_id,
                    text=(MESSAGES["released_choose_again"] + "\n\n"
                          + MESSAGES["next_step_question"]),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(MESSAGES["picker_choose_else_button"],
                                              callback_data=f"{CB_PICK_LIST}:0")],
                        [InlineKeyboardButton(MESSAGES["picker_anyone_button"],
                                              callback_data=CB_PICK_OPEN)],
                        [InlineKeyboardButton(MESSAGES["picker_cancel_button"],
                                              callback_data=CB_PICK_CANCEL)],
                    ]),
                )
            except Exception as exc:
                logger.warning("Could not offer %s the choice again: %s",
                               requester_id, exc)

        await update.message.reply_text(MESSAGES["released_member"])
    
    def _open_request_status_text(self, user_id: int) -> str:
        """One description of an existing open request, shared by /status and /chat.

        Two callers, one answer: telling somebody at the picker "you're already in
        the queue, please wait for a support member" would have them waiting for a
        message that only arrives once they tap a name.
        """
        queue_id = user_to_queue_map.get(user_id)
        entry = self.queue_manager.get_queue_entry(queue_id) if queue_id else None
        if entry is None:
            return MESSAGES["idle_status"]

        routing = entry.get('routing') or 'open'
        svc = get_service(entry.get('service'))
        if routing == 'choosing':
            return MESSAGES["choosing_status"]
        if routing == 'directed':
            # Discloses only the supporter this requester chose themselves.
            profile = svc.roster.profile(entry.get('target_member_id'))
            name = profile.display_name if profile is not None else svc.member_label
            return MESSAGES["directed_status"].format(name=name)
        return MESSAGES["queue_status"].format(member=svc.member_label)

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        user_id = update.effective_user.id
        current_state = user_states.get(user_id, UserState.IDLE)

        if current_state == UserState.IN_CONVERSATION:
            await update.message.reply_text(MESSAGES["conversation_status"])
        elif current_state in (UserState.IN_QUEUE, UserState.CHOOSING_SUPPORTER):
            if self.queue_manager.is_user_in_queue(user_id):
                await update.message.reply_text(self._open_request_status_text(user_id))
            else:
                # State drift: reset local state to avoid confusing responses
                user_states[user_id] = UserState.IDLE
                await update.message.reply_text(MESSAGES["idle_status"])
        else:
            await update.message.reply_text(MESSAGES["idle_status"])
    
    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /cancel command to leave queue"""
        user_id = update.effective_user.id
        current_state = user_states.get(user_id, UserState.IDLE)
        
        # Allow users to cancel while providing their request description
        if current_state == UserState.WAITING_FOR_DESCRIPTION:
            user_states[user_id] = UserState.IDLE
            await update.message.reply_text(MESSAGES["help_request_cancelled"])
            return

        # Check if user is actually in queue. CHOOSING_SUPPORTER counts: they have a
        # live request, it is just not in the channel yet.
        if current_state not in (UserState.IN_QUEUE, UserState.CHOOSING_SUPPORTER):
            await update.message.reply_text(MESSAGES["not_in_queue"])
            return
        
        # Verify user is in queue (double-check)
        if not self.queue_manager.is_user_in_queue(user_id):
            # State mismatch - reset user state
            user_states[user_id] = UserState.IDLE
            await update.message.reply_text(MESSAGES["not_in_queue"])
            return
        
        # Remove from queue
        success, result = await self.queue_manager.remove_from_queue(user_id)
        
        if success:
            # Update user state
            user_states[user_id] = UserState.IDLE
            await update.message.reply_text(MESSAGES["queue_cancelled"])
        else:
            # This shouldn't happen given our checks above, but handle gracefully
            await update.message.reply_text(MESSAGES["cancel_error"])
    
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle regular messages"""
        user_id = update.effective_user.id
        message_text = update.message.text
        current_state = user_states.get(user_id, UserState.IDLE)

        # Backfill: ANY private message from a member proves the bot may DM them.
        # Without this every supporter already on the roster is invisible in the
        # picker until they happen to type /start again, which most never will.
        if self._is_private_chat(update) and is_any_member(user_id):
            self._record_bot_started(user_id)

        # A number typed at the picker. Checked BEFORE the description branch, and
        # routed into the SAME coroutine the pk_s: buttons use -- two entry points
        # into a send path is how one of them ends up missing a guard.
        if current_state == UserState.CHOOSING_SUPPORTER:
            await self._handle_picker_number(update, context, message_text)
            return

        # Check if user is waiting for description
        if current_state == UserState.WAITING_FOR_DESCRIPTION:
            await self._handle_help_description(update, context, message_text)
            return
        
        # Check if user is in an active conversation
        session_id = self.session_manager.get_session_by_user(user_id)
        if session_id:
            success = await self.session_manager.forward_message(session_id, user_id, message_text)
            if not success:
                await update.message.reply_text("Sorry, there was an error sending your message.")
            return
        
        # Default response for messages when not in conversation or waiting for input
        await update.message.reply_text(MESSAGES["unknown_command"])
    
    async def handle_sticker(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle and relay stickers during an active conversation"""
        user_id = update.effective_user.id
        sticker_file_id = update.message.sticker.file_id
        
        # Check if the user is in an active session
        session_id = self.session_manager.get_session_by_user(user_id)
        
        if session_id:
            # If a session exists, forward the sticker
            success = await self.session_manager.forward_sticker(session_id, user_id, sticker_file_id)
            if not success:
                await update.message.reply_text("Sorry, there was an error sending your sticker. Please try again.")
        else:
            # If no session, inform the user
            await update.message.reply_text("You can only send stickers during an active conversation. Use /chat to start.")
    
    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle and relay photos during an active conversation with size validation"""
        user_id = update.effective_user.id
        
        # Check if the user is in an active session
        session_id = self.session_manager.get_session_by_user(user_id)
        
        if session_id:
            # Get the largest photo size for best quality
            photo = update.message.photo[-1]
            
            # Check file size (Telegram limit is 10MB for photos)
            if photo.file_size and photo.file_size > 10 * 1024 * 1024:  # 10MB limit
                await update.message.reply_text(MESSAGES["photo_size_limit"])
                return
            
            success = await self.session_manager.forward_photo(
                session_id, user_id, photo.file_id, photo.file_size
            )
            if not success:
                await update.message.reply_text(MESSAGES["photo_error"])
        else:
            # If no session, inform the user
            await update.message.reply_text(
                "You can only send photos during an active conversation. Use /chat to start."
            )
    
    async def _handle_help_description(self, update: Update, context: ContextTypes.DEFAULT_TYPE, description: str):
        """Handle help description from user"""
        user_id = update.effective_user.id

        # Which service did the user pick? (read-and-clear; defaults to hf in Phase 1)
        service_key = user_to_service_map.pop(user_id, default_service_key())

        user_telehandle = f"@{update.effective_user.username}" if update.effective_user.username else None

        # THE FORK. `if options:` is load-bearing in three separate ways: no pickable
        # supporters means no dead-end UI, means HF (directed_enabled False) never
        # reaches it, and means the pre-Phase-5 suites -- whose rosters are bare ints
        # with no display names -- keep driving the original path below untouched.
        svc = get_service(service_key)
        options = available_supporters(svc.key) if svc.directed_enabled else []
        if options:
            # The Mongo row is created BEFORE the question, so a restart mid-question
            # is recoverable rather than a request that quietly never existed.
            queue_id = self.queue_manager.add_to_queue(
                user_id, description, user_telehandle, service_key, routing='choosing')
            user_states[user_id] = UserState.CHOOSING_SUPPORTER
            await update.message.reply_text(
                MESSAGES["comfort_question"],
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(MESSAGES["comfort_specific_button"],
                                          callback_data=f"{CB_PICK_LIST}:0")],
                    [InlineKeyboardButton(MESSAGES["comfort_anyone_button"],
                                          callback_data=CB_PICK_OPEN)],
                ]),
            )
            return

        # Add to queue
        queue_id = self.queue_manager.add_to_queue(user_id, description, user_telehandle, service_key)
        
        # Post to admin channel
        success, error_type = await self.queue_manager.post_queue_to_channel(queue_id)
        
        if success:
            user_states[user_id] = UserState.IN_QUEUE
            await update.message.reply_text(MESSAGES["queue_added"])
        else:
            # Provide specific error messages based on error type
            if error_type == "chat_not_found":
                await update.message.reply_text(MESSAGES["channel_error"])
            elif error_type == "access_denied":
                await update.message.reply_text(MESSAGES["channel_error"])
            elif error_type == "channel_offline":
                await update.message.reply_text(MESSAGES["queue_system_offline"])
            else:
                await update.message.reply_text("Sorry, there was an error processing your request. Please try again.")
    
    async def handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard button presses"""
        query = update.callback_query
        user_id = query.from_user.id
        data = query.data or ""

        # Requester picking a service on the landing page. Requesters are NOT members,
        # so this MUST be dispatched before the membership gate below.
        if data.startswith("svc_"):
            await self._handle_service_choice(update, context)
            return

        # Picker callbacks, for the SAME reason: requesters are not members. Every
        # pk_* branch resolves the request through user_to_queue_map[user_id] and then
        # checks the entry belongs to them, so a member who is themselves a requester
        # still works and nobody can act on somebody else's request.
        if (data in (CB_PICK_OPEN, CB_PICK_CANCEL)
                or data.startswith(CB_PICK_LIST + ":")
                or data.startswith(CB_PICK_SELECT + ":")):
            await self._handle_picker_callback(update, context)
            return

        # Membership gate first, matching the original ordering: any non-svc action by a
        # non-member gets "not authorized" before we distinguish claim vs other data.
        if not is_any_member(user_id):
            await query.answer("You are not authorized to perform this action.", show_alert=True)
            return

        # Accept / Not right now on a directed request. After the gate: only members
        # get here, and accept_directed then narrows that to the ONE targeted member.
        if (data.startswith(CB_DIRECT_ACCEPT + ":")
                or data.startswith(CB_DIRECT_DECLINE + ":")):
            await self._handle_directed_response(update, context)
            return

        # Only claim actions remain valid for members
        if not data.startswith("claim_"):
            await query.answer("Invalid action.", show_alert=True)
            return

        queue_id = data[len("claim_"):]

        # Block claims if the member still has their own pending request
        if self.queue_manager.is_user_in_queue(user_id):
            await query.answer(MESSAGES["member_cancel_before_claim"], show_alert=True)
            return

        # Check if the member is already in a conversation
        existing_session = self.session_manager.get_session_by_user(user_id)
        if existing_session:
            await query.answer("You are already in a conversation. End it first before claiming a new one.", show_alert=True)
            return

        # Resolve the queue entry for service-scoped authorization + threading
        entry = self.queue_manager.get_queue_entry(queue_id)
        if entry is None:
            await query.answer("This request has already been claimed or expired.", show_alert=True)
            return

        # Defence in depth. Should be unreachable -- a choosing/directed request has no
        # channel post -- but a supporter can be holding a stale Claim button from an
        # EARLIER lane: a request that was declined and went open reuses the same
        # session_id, and could be sent back to a specific supporter later. One dict
        # lookup closes the window in which claim_queue would authorize the whole
        # roster against a request routed to one person.
        if (entry.get('routing') or 'open') in ('choosing', 'directed'):
            await query.answer("This request was sent to a specific supporter.",
                               show_alert=True)
            return

        # A member may only claim requests for their own service's roster.
        service_key = entry.get('service', 'hf')
        if not is_member_of_service(user_id, service_key):
            await query.answer("You are not authorized to claim this request.", show_alert=True)
            return

        # Get member name for display
        heartfelt_member_name = query.from_user.first_name or f"Member #{user_id}"
        if query.from_user.last_name:
            heartfelt_member_name += f" {query.from_user.last_name}"

        # Claim the queue
        heartfelt_member_telehandle = f"@{query.from_user.username}" if query.from_user.username else None
        try:
            claimed_user_id = await self.queue_manager.claim_queue(
                queue_id,
                user_id,
                heartfelt_member_name,
                heartfelt_member_telehandle
            )
        except SelfClaimError:
            await query.answer("You can't claim your own request.", show_alert=True)
            return

        if claimed_user_id is None:
            await query.answer("This request has already been claimed or expired.", show_alert=True)
            return

        await self._start_claimed_conversation(context, queue_id, claimed_user_id,
                                               user_id, service_key)
        await query.answer("Conversation claimed successfully!")

    async def _start_claimed_conversation(self, context, queue_id: str, requester_id: int,
                                          member_id: int, service_key: str) -> str:
        """Create the session and greet both parties.

        Shared by the channel Claim path and the directed Accept path. Two copies of a
        session-creation path is how the two drift, and the drift is invisible until
        somebody in one lane never gets told their conversation started.
        """
        # Create session using the queue_id as session_id to maintain database consistency
        session_id = self.session_manager.create_session(requester_id, member_id, queue_id, service=service_key)

        # Update states
        user_states[requester_id] = UserState.IN_CONVERSATION
        user_states[member_id] = UserState.IN_CONVERSATION

        # Notify both parties
        try:
            await context.bot.send_message(
                chat_id=requester_id,
                text=MESSAGES["conversation_started"]
            )
        except:
            pass

        try:
            # Get the session to retrieve the anonymous ID
            session_info = self.session_manager.get_session_info(session_id)
            anonymous_id = session_info.get('anonymous_user_id', 'Unknown User') if session_info else 'Unknown User'

            await context.bot.send_message(
                chat_id=member_id,
                text=f"You have claimed a conversation with {anonymous_id}. You can now start chatting."
            )
        except:
            pass

        return session_id

    # ==================================================================
    # The supporter picker (Phase 5)
    # ==================================================================

    async def _send_or_edit(self, query, context, chat_id: int, text: str,
                            reply_markup=None, parse_mode=None) -> None:
        """Edit the message the button lives on, falling back to a fresh DM.

        The same pattern _handle_service_choice already uses: an edit can fail for
        reasons that have nothing to do with us (message too old, identical content),
        and the requester must still see the new list."""
        if query is not None:
            try:
                await query.edit_message_text(text, reply_markup=reply_markup,
                                              parse_mode=parse_mode)
                return
            except Exception:
                pass
        try:
            await context.bot.send_message(chat_id=chat_id, text=text,
                                           reply_markup=reply_markup,
                                           parse_mode=parse_mode)
        except Exception as exc:
            logger.warning("Could not show the picker to %s: %s", chat_id, exc)

    async def _render_picker(self, query, context, user_id: int, queue_id: str,
                             page: int, note: str = "") -> bool:
        """Render one page of choosable supporters. False when there is nobody to show.

        Numbers are 1-based and PAGE-LOCAL, and the view is recorded BEFORE the
        message is sent so a very fast reply cannot race the record and be read
        against the previous page.
        """
        entry = self.queue_manager.get_queue_entry(queue_id)
        if entry is None:
            return False

        svc = get_service(entry.get('service'))
        declined = set(entry.get('declined_by') or [])
        options = [p for p in available_supporters(svc.key)
                   if p.telegram_id not in declined]

        if not options:
            # NEVER render an empty list: a picker with no names is a dead end with no
            # way out but /cancel.
            picker_views.pop(user_id, None)
            await self._send_or_edit(
                query, context, user_id, MESSAGES["picker_nobody_free"],
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(MESSAGES["picker_anyone_button"],
                                          callback_data=CB_PICK_OPEN)],
                    [InlineKeyboardButton(MESSAGES["picker_cancel_button"],
                                          callback_data=CB_PICK_CANCEL)],
                ]))
            return False

        page_size = max(1, svc.picker_page_size)
        pages = max(1, (len(options) + page_size - 1) // page_size)
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = 0
        page = max(0, min(page, pages - 1))
        chunk = options[page * page_size:(page + 1) * page_size]

        lines = []
        for number, profile in enumerate(chunk, start=1):
            lines.append(f"{number}. {html.escape(profile.display_name)}")
            if profile.blurb:
                lines.append(f"   {html.escape(profile.blurb)}")

        text = ((note + "\n\n") if note else "") + MESSAGES["picker_header"] + "\n\n"
        text += "\n".join(lines) + "\n\n" + MESSAGES["picker_hint"]
        if pages > 1:
            text += "\n" + MESSAGES["picker_page"].format(page=page + 1, pages=pages)

        rows = [[InlineKeyboardButton(f"{number}. {profile.display_name}"[:60],
                                      callback_data=f"{CB_PICK_SELECT}:{profile.telegram_id}")]
                for number, profile in enumerate(chunk, start=1)]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(MESSAGES["picker_back_button"],
                                            callback_data=f"{CB_PICK_LIST}:{page - 1}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(MESSAGES["picker_next_button"],
                                            callback_data=f"{CB_PICK_LIST}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([InlineKeyboardButton(MESSAGES["picker_anyone_button"],
                                          callback_data=CB_PICK_OPEN)])
        rows.append([InlineKeyboardButton(MESSAGES["picker_cancel_button"],
                                          callback_data=CB_PICK_CANCEL)])

        # RECORD BEFORE SENDING.
        picker_views[user_id] = {
            'queue_id': queue_id,
            'page': page,
            'ids': [p.telegram_id for p in chunk],
            'rendered_at': utcnow(),
        }

        await self._send_or_edit(query, context, user_id, text,
                                 InlineKeyboardMarkup(rows), parse_mode='HTML')
        return True

    async def _handle_picker_number(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                    message_text: str) -> None:
        """A typed number. Converges on the same _choose_supporter the buttons use."""
        user_id = update.effective_user.id
        queue_id = user_to_queue_map.get(user_id)
        if not queue_id:
            # State drift: they are CHOOSING_SUPPORTER with no request.
            user_states[user_id] = UserState.IDLE
            await update.message.reply_text(MESSAGES["not_in_queue"])
            return

        view = picker_views.get(user_id)
        # NO GUESSING when there is no view. This is the post-restart path -- picker_views
        # is deliberately not persisted -- and a number read against a list we never
        # rendered could select somebody the requester has never seen.
        if view is None or view.get('queue_id') != queue_id:
            await self._render_picker(None, context, user_id, queue_id, 0,
                                      MESSAGES["picker_lost_view"])
            return

        ids = view.get('ids') or []
        text = (message_text or "").strip()
        # isascii(): "\u00b2".isdigit() is True but int() raises on it.
        if not (text.isascii() and text.isdigit()):
            await self._render_picker(None, context, user_id, queue_id,
                                      view.get('page', 0), MESSAGES["picker_not_a_number"])
            return

        number = int(text)
        if not (1 <= number <= len(ids)):
            await self._render_picker(None, context, user_id, queue_id,
                                      view.get('page', 0), MESSAGES["picker_not_a_number"])
            return

        await self._choose_supporter(None, context, user_id, queue_id, ids[number - 1])

    async def _handle_picker_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """pk_o / pk_l:<page> / pk_s:<member_id> / pk_x, all from the requester."""
        query = update.callback_query
        user_id = query.from_user.id
        data = query.data or ""

        queue_id = user_to_queue_map.get(user_id)
        entry = self.queue_manager.get_queue_entry(queue_id) if queue_id else None
        # Ownership, not just existence: user_to_queue_map is keyed by the requester,
        # so this can only ever be their own request -- assert it anyway.
        if entry is None or entry.get('user_id') != user_id:
            await query.answer(MESSAGES["directed_gone"], show_alert=True)
            return

        if data == CB_PICK_CANCEL:
            await query.answer()
            success, _reason = await self.queue_manager.remove_from_queue(user_id)
            user_states[user_id] = UserState.IDLE
            picker_views.pop(user_id, None)
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=MESSAGES["queue_cancelled"] if success else MESSAGES["cancel_error"])
            except Exception as exc:
                logger.warning("Could not confirm cancellation to %s: %s", user_id, exc)
            return

        if data == CB_PICK_OPEN:
            await query.answer()
            if not await self.queue_manager.route_to_open_queue(queue_id):
                try:
                    await context.bot.send_message(chat_id=user_id,
                                                   text=MESSAGES["channel_error"])
                except Exception as exc:
                    logger.warning("Could not report the channel error to %s: %s",
                                   user_id, exc)
            return

        if data.startswith(CB_PICK_LIST + ":"):
            await query.answer()
            try:
                page = int(data.split(":", 1)[1])
            except (IndexError, TypeError, ValueError):
                page = 0
            await self._render_picker(query, context, user_id, queue_id, page)
            return

        # CB_PICK_SELECT
        await query.answer()
        try:
            member_id = int(data.split(":", 1)[1])
        except (IndexError, TypeError, ValueError):
            await self._render_picker(query, context, user_id, queue_id, 0,
                                      MESSAGES["picker_not_a_number"])
            return
        await self._choose_supporter(query, context, user_id, queue_id, member_id)

    async def _choose_supporter(self, query, context, user_id: int, queue_id: str,
                                member_id: int) -> None:
        """THE ONE send path, reached identically by a tapped button and a typed number."""
        outcome = await self.queue_manager.send_directed_request(queue_id, member_id)

        if outcome == 'ok':
            entry = self.queue_manager.get_queue_entry(queue_id)
            svc = get_service(entry.get('service')) if entry else None
            profile = svc.roster.profile(member_id) if svc is not None else None
            name = profile.display_name if profile is not None else ""
            picker_views.pop(user_id, None)
            await self._send_or_edit(
                query, context, user_id,
                MESSAGES["directed_sent"].format(name=html.escape(name) or "them"),
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(MESSAGES["picker_anyone_button"],
                                          callback_data=CB_PICK_OPEN)],
                    [InlineKeyboardButton(MESSAGES["picker_cancel_button"],
                                          callback_data=CB_PICK_CANCEL)],
                ]), parse_mode='HTML')
            return

        if outcome == 'busy':
            await self._render_picker(query, context, user_id, queue_id, 0,
                                      MESSAGES["picker_busy"])
            return

        if outcome == 'unreachable':
            await self._render_picker(query, context, user_id, queue_id, 0,
                                      MESSAGES["picker_unreachable"])
            return

        # 'gone'. Two very different situations reach here, and directed_gone --
        # "This request is no longer waiting." -- is a LIE in the second one: a stale
        # picker button tapped while the request is already sitting with somebody.
        # Say which it is, and never name a supporter the requester did not choose.
        picker_views.pop(user_id, None)
        if self.queue_manager.get_queue_entry(queue_id) is not None:
            await self._send_or_edit(query, context, user_id,
                                     self._open_request_status_text(user_id))
            return
        await self._send_or_edit(query, context, user_id, MESSAGES["directed_gone"])

    async def _handle_directed_response(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """dr_a:<sid> / dr_d:<sid>, from the targeted supporter."""
        query = update.callback_query
        user_id = query.from_user.id
        data = query.data or ""
        accepting = data.startswith(CB_DIRECT_ACCEPT + ":")
        queue_id = data.split(":", 1)[1] if ":" in data else ""

        if accepting:
            # The SAME guards the channel claim path applies, re-applied here rather
            # than assumed: a targeted supporter is still a member with their own life.
            if self.queue_manager.is_user_in_queue(user_id):
                await query.answer(MESSAGES["member_cancel_before_claim"], show_alert=True)
                return
            if self.session_manager.get_session_by_user(user_id):
                await query.answer(
                    "You are already in a conversation. End it first before claiming a new one.",
                    show_alert=True)
                return

            entry = self.queue_manager.get_queue_entry(queue_id)
            service_key = entry.get('service', 'hf') if entry else 'hf'

            member_name = query.from_user.first_name or f"Member #{user_id}"
            if query.from_user.last_name:
                member_name += f" {query.from_user.last_name}"
            member_telehandle = f"@{query.from_user.username}" if query.from_user.username else None

            try:
                result, requester_id = await self.queue_manager.accept_directed(
                    queue_id, user_id, member_name, member_telehandle)
            except SelfClaimError:
                await query.answer("You can't claim your own request.", show_alert=True)
                return

            if result == 'not_yours':
                await query.answer("This request was sent to a different supporter.",
                                   show_alert=True)
                return
            if result != 'ok' or requester_id is None:
                await query.answer(MESSAGES["directed_gone"], show_alert=True)
                return

            await self._start_claimed_conversation(context, queue_id, requester_id,
                                                   user_id, service_key)
            await query.answer("Conversation claimed successfully!")
            return

        # --- declining -------------------------------------------------------
        entry = self.queue_manager.get_queue_entry(queue_id)
        name = ""
        if entry is not None:
            profile = get_service(entry.get('service')).roster.profile(user_id)
            name = profile.display_name if profile is not None else ""

        if not await self.queue_manager.undirect(queue_id, user_id, reason='declined',
                                                 notify_member=False):
            # We did NOT win the transition, so the requester hears nothing at all --
            # somebody else already moved this request.
            await query.answer(MESSAGES["directed_gone"], show_alert=True)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return

        await query.answer()
        try:
            await context.bot.send_message(chat_id=user_id, text=MESSAGES["decline_ack"])
        except Exception as exc:
            logger.warning("Could not acknowledge the decline to %s: %s", user_id, exc)

        entry = self.queue_manager.get_queue_entry(queue_id)
        if entry is not None:
            await self.queue_manager._offer_next_step(entry, "directed_unavailable", name)

    async def _handle_service_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle the requester's landing-page service selection (Phase 2 chooser)."""
        query = update.callback_query
        user_id = query.from_user.id

        if user_states.get(user_id) != UserState.WAITING_FOR_SERVICE:
            await query.answer("This choice is no longer valid. Use /chat to start.", show_alert=True)
            return

        key = (query.data or "")[len("svc_"):]
        svc = get_service(key)
        if not svc.runnable:
            await query.answer("That option isn't available right now.", show_alert=True)
            return

        user_to_service_map[user_id] = svc.key
        user_states[user_id] = UserState.WAITING_FOR_DESCRIPTION
        prompt = help_request_text(svc.key)
        try:
            await query.edit_message_text(prompt)
        except Exception:
            # If the message can't be edited, fall back to a fresh prompt
            await context.bot.send_message(chat_id=user_id, text=prompt)
        await query.answer()

    async def handle_error(self, update: Update, context: CallbackContext):
        """Handle errors, scrubbing the bot token from any error text before logging."""
        scrubbed = re.sub(r"/bot\d+:[\w-]+/", "/bot<redacted>/", str(context.error))
        logger.error("Update caused error %s: %s", type(context.error).__name__, scrubbed)
