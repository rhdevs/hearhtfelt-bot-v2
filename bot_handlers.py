from telegram import Update
from telegram.ext import ContextTypes, CallbackContext
from config import (
    UserState, user_states, HEARTFELT_MEMBERS, MESSAGES
)
from session_manager import SessionManager
from queue_manager import QueueManager

class BotHandlers:
    def __init__(self, session_manager: SessionManager, queue_manager: QueueManager):
        self.session_manager = session_manager
        self.queue_manager = queue_manager
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        user_states[user_id] = UserState.IDLE
        
        await update.message.reply_text(MESSAGES["welcome"])
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command to start help request"""
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
        
        # Set state to waiting for description
        user_states[user_id] = UserState.WAITING_FOR_DESCRIPTION
        await update.message.reply_text(MESSAGES["help_request"])
    
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
                message = MESSAGES["conversation_ended_heartfelt"] if user_id_session in HEARTFELT_MEMBERS else MESSAGES["conversation_ended"]
                await context.bot.send_message(
                    chat_id=user_id_session,
                    text=message
                )
            except:
                pass
        
        if heartfelt_id_session and heartfelt_id_session != user_id:
            try:
                # Send user message to user, heartfelt message to heartfelt member
                message = MESSAGES["conversation_ended_heartfelt"] if heartfelt_id_session in HEARTFELT_MEMBERS else MESSAGES["conversation_ended"]
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
            position = self.queue_manager.get_queue_position(user_id)
            if position:
                wait_time = self.queue_manager.get_estimated_wait_time(position)
                message = MESSAGES["queue_status"].format(
                    position=position, wait_time=wait_time
                )
                await update.message.reply_text(message)
            else:
                await update.message.reply_text(MESSAGES["idle_status"])
        else:
            await update.message.reply_text(MESSAGES["idle_status"])
    
    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /cancel command to leave queue"""
        user_id = update.effective_user.id
        current_state = user_states.get(user_id, UserState.IDLE)
        
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
        await update.message.reply_text(
            "I'm not sure what you mean. Use /help to start a conversation with a support member."
        )
    
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
            await update.message.reply_text("You can only send stickers during an active conversation. Use /help to start.")
    
    async def _handle_help_description(self, update: Update, context: ContextTypes.DEFAULT_TYPE, description: str):
        """Handle help description from user"""
        user_id = update.effective_user.id
        
        # Add to queue
        queue_id = self.queue_manager.add_to_queue(user_id, description)
        
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
        
        # Check if user is authorized heartfelt member
        if user_id not in HEARTFELT_MEMBERS:
            await query.answer("You are not authorized to perform this action.", show_alert=True)
            return
        
        # Parse callback data
        if not query.data.startswith("claim_"):
            await query.answer("Invalid action.", show_alert=True)
            return
        
        queue_id = query.data.replace("claim_", "")
        
        # Check if heartfelt member is already in a conversation
        existing_session = self.session_manager.get_session_by_user(user_id)
        if existing_session:
            await query.answer("You are already in a conversation. End it first before claiming a new one.", show_alert=True)
            return
        
        # Get heartfelt member name for display
        heartfelt_member_name = query.from_user.first_name or f"Member #{user_id}"
        if query.from_user.last_name:
            heartfelt_member_name += f" {query.from_user.last_name}"
        
        # Claim the queue
        claimed_user_id = await self.queue_manager.claim_queue(queue_id, user_id, heartfelt_member_name)
        
        if claimed_user_id is None:
            await query.answer("This request has already been claimed or expired.", show_alert=True)
            return
        
        # Create session using the queue_id as session_id to maintain database consistency
        session_id = self.session_manager.create_session(claimed_user_id, user_id, queue_id)
        
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
    
    async def handle_error(self, update: Update, context: CallbackContext):
        """Handle errors"""
        print(f"Update {update} caused error {context.error}")