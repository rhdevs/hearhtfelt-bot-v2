import logging
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackContext
from config import (
    SERVICES,
    UserState,
    user_states,
    user_to_service_map,
    MESSAGES,
    PHOTO_SHARING_ENABLED,
    is_heartfelt_member,
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
        """Handle /end command to end conversation"""
        user_id = update.effective_user.id
        current_state = user_states.get(user_id, UserState.IDLE)
        
        # Check if user has an active session
        session_id = self.session_manager.get_session_by_user(user_id)
        if not session_id:
            await update.message.reply_text(MESSAGES["no_active_conversation"])
            return
        
        # End the session
        user_id_session, heartfelt_id_session = await self.session_manager.end_session(session_id, user_id)
        
        # Update states
        if user_id_session:
            user_states[user_id_session] = UserState.IDLE
        if heartfelt_id_session:
            user_states[heartfelt_id_session] = UserState.IDLE
        
        # Notify both parties with appropriate messages
        if user_id_session:
            try:
                # Send user message to user, heartfelt message to heartfelt member
                message = (
                    MESSAGES["conversation_ended_heartfelt"]
                    if is_heartfelt_member(user_id_session)
                    else MESSAGES["conversation_ended"]
                )
                await context.bot.send_message(
                    chat_id=user_id_session,
                    text=message
                )
            except:
                pass
        
        if heartfelt_id_session and heartfelt_id_session != user_id:
            try:
                # Send user message to user, heartfelt message to heartfelt member
                message = (
                    MESSAGES["conversation_ended_heartfelt"]
                    if is_heartfelt_member(heartfelt_id_session)
                    else MESSAGES["conversation_ended"]
                )
                await context.bot.send_message(
                    chat_id=heartfelt_id_session,
                    text=message
                )
            except:
                pass
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        user_id = update.effective_user.id
        current_state = user_states.get(user_id, UserState.IDLE)
        
        if current_state == UserState.IN_CONVERSATION:
            await update.message.reply_text(MESSAGES["conversation_status"])
        elif current_state == UserState.IN_QUEUE:
            if self.queue_manager.is_user_in_queue(user_id):
                svc = get_service(self.queue_manager.get_user_service(user_id))
                await update.message.reply_text(MESSAGES["queue_status"].format(member=svc.member_label))
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

        # Check if user is actually in queue
        if current_state != UserState.IN_QUEUE:
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

        # Add to queue
        user_telehandle = f"@{update.effective_user.username}" if update.effective_user.username else None
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

        # Membership gate first, matching the original ordering: any non-svc action by a
        # non-member gets "not authorized" before we distinguish claim vs other data.
        if not is_any_member(user_id):
            await query.answer("You are not authorized to perform this action.", show_alert=True)
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

        # Create session using the queue_id as session_id to maintain database consistency
        session_id = self.session_manager.create_session(claimed_user_id, user_id, queue_id, service=service_key)
        
        # Update states
        user_states[claimed_user_id] = UserState.IN_CONVERSATION
        user_states[user_id] = UserState.IN_CONVERSATION
        
        # Notify both parties
        try:
            await context.bot.send_message(
                chat_id=claimed_user_id,
                text=MESSAGES["conversation_started"]
            )
        except:
            pass
        
        try:
            # Get the session to retrieve the anonymous ID
            session_info = self.session_manager.get_session_info(session_id)
            anonymous_id = session_info.get('anonymous_user_id', 'Unknown User') if session_info else 'Unknown User'
            
            await context.bot.send_message(
                chat_id=user_id,
                text=f"You have claimed a conversation with {anonymous_id}. You can now start chatting."
            )
        except:
            pass
        
        await query.answer("Conversation claimed successfully!")

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
