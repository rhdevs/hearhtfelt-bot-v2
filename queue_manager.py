import datetime
import uuid
import random
from typing import Optional, Tuple
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from config import queue_entries, ADMIN_CHANNEL_ID, QUEUE_EXPIRE_MINUTES, user_to_queue_map, queue_order

class QueueManager:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.channel_accessible = True  # Track channel accessibility
        self.used_anonymous_ids = set()  # Track used IDs to avoid duplicates
    
    def _generate_anonymous_id(self) -> str:
        """Generate a unique random anonymous ID"""
        # List of adjectives and animals for friendly anonymous names
        adjectives = [
            "Brave", "Kind", "Gentle", "Strong", "Wise", "Caring", "Hopeful", "Bright",
            "Calm", "Patient", "Peaceful", "Thoughtful", "Creative", "Curious", "Friendly"
        ]
        animals = [
            "Owl", "Butterfly", "Dolphin", "Panda", "Fox", "Rabbit", "Deer", "Bird",
            "Cat", "Turtle", "Swan", "Bee", "Whale", "Eagle", "Bear"
        ]
        
        # Try up to 50 times to generate a unique ID
        for _ in range(50):
            adj = random.choice(adjectives)
            animal = random.choice(animals)
            number = random.randint(100, 999)
            anonymous_id = f"{adj} {animal} #{number}"
            
            if anonymous_id not in self.used_anonymous_ids:
                self.used_anonymous_ids.add(anonymous_id)
                return anonymous_id
        
        # Fallback to UUID if we can't generate unique friendly name
        fallback_id = f"Anonymous User #{str(uuid.uuid4())[:8]}"
        self.used_anonymous_ids.add(fallback_id)
        return fallback_id
    
    def add_to_queue(self, user_id: int, description: str) -> str:
        """Add user to the help queue"""
        queue_id = str(uuid.uuid4())
        
        queue_entries[queue_id] = {
            'user_id': user_id,
            'description': description,
            'created_at': datetime.datetime.now(),
            'anonymous_id': self._generate_anonymous_id(),
            'message_id': None  # Will be set after posting to channel
        }
        
        # Maintain O(1) lookup indices
        user_to_queue_map[user_id] = queue_id
        queue_order.append(queue_id)
        
        return queue_id
    
    async def post_queue_to_channel(self, queue_id: str) -> Tuple[bool, str]:
        """Post queue entry to admin channel with claim button"""
        try:
            queue_entry = queue_entries.get(queue_id)
            if not queue_entry:
                return False, "Queue entry not found"
            
            # Skip if channel is known to be inaccessible
            if not self.channel_accessible:
                return False, "channel_offline"
            
            # Create message text
            message_text = (
                f"🆘 New Help Request\n\n"
                f"From: {queue_entry['anonymous_id']}\n"
                f"Time: {queue_entry['created_at'].strftime('%H:%M')}\n\n"
                f"Description: {queue_entry['description'][:200]}{'...' if len(queue_entry['description']) > 200 else ''}"
            )
            
            # Create inline keyboard with claim button
            keyboard = [[InlineKeyboardButton("📞 Claim", callback_data=f"claim_{queue_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Send message to admin channel
            message = await self.bot.send_message(
                chat_id=ADMIN_CHANNEL_ID,
                text=message_text,
                reply_markup=reply_markup
            )
            
            # Store message ID for later deletion
            queue_entries[queue_id]['message_id'] = message.message_id
            self.channel_accessible = True  # Mark as accessible on success
            
            return True, "success"
            
        except Exception as e:
            error_msg = str(e).lower()
            print(f"Error posting queue to channel: {e}")
            
            # Categorize the error
            if "chat not found" in error_msg:
                self.channel_accessible = False
                return False, "chat_not_found"
            elif "forbidden" in error_msg or "not enough rights" in error_msg:
                self.channel_accessible = False
                return False, "access_denied"
            elif "network" in error_msg or "timeout" in error_msg:
                return False, "network_error"
            else:
                return False, "unknown_error"
    
    async def claim_queue(self, queue_id: str, heartfelt_member_id: int, heartfelt_member_name: str = None) -> Optional[int]:
        """Claim a queue entry and return the user ID"""
        queue_entry = queue_entries.get(queue_id)
        if not queue_entry:
            return None
        
        user_id = queue_entry['user_id']
        message_id = queue_entry['message_id']
        
        # Edit the queue message to show it's been claimed instead of deleting it
        try:
            if message_id:
                await self._edit_claimed_message(queue_entry, heartfelt_member_name or f"Member #{heartfelt_member_id}")
        except Exception as e:
            print(f"Error editing queue message: {e}")
            # Fallback to deletion if edit fails
            try:
                await self.bot.delete_message(
                    chat_id=ADMIN_CHANNEL_ID,
                    message_id=message_id
                )
            except Exception as delete_error:
                print(f"Error deleting queue message as fallback: {delete_error}")
        
        # Remove from queue and clean up indices
        del queue_entries[queue_id]
        
        # Clean up O(1) lookup indices
        if user_id in user_to_queue_map:
            del user_to_queue_map[user_id]
        if queue_id in queue_order:
            queue_order.remove(queue_id)
        
        return user_id
    
    async def _edit_claimed_message(self, queue_entry: dict, heartfelt_member_name: str):
        """Edit the queue message to show it's been claimed"""
        claimed_time = datetime.datetime.now()
        
        # Create the claimed message text
        claimed_message_text = (
            f"✅ **CLAIMED** - Help Request\n\n"
            f"From: {queue_entry['anonymous_id']}\n"
            f"Requested: {queue_entry['created_at'].strftime('%H:%M')}\n"
            f"Claimed by: {heartfelt_member_name}\n"
            f"Claimed at: {claimed_time.strftime('%H:%M')}\n\n"
            f"Description: {queue_entry['description'][:200]}{'...' if len(queue_entry['description']) > 200 else ''}"
        )
        
        # Edit the message with no inline keyboard (removes the claim button)
        await self.bot.edit_message_text(
            chat_id=ADMIN_CHANNEL_ID,
            message_id=queue_entry['message_id'],
            text=claimed_message_text,
            reply_markup=None
        )
    
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
    
    def cleanup_expired_queues(self):
        """Remove expired queue entries"""
        now = datetime.datetime.now()
        expired_queue_ids = []
        
        for queue_id, entry in queue_entries.items():
            time_diff = now - entry['created_at']
            if time_diff.total_seconds() > (QUEUE_EXPIRE_MINUTES * 60):
                expired_queue_ids.append(queue_id)
        
        for queue_id in expired_queue_ids:
            # Try to delete the message from admin channel
            try:
                entry = queue_entries[queue_id]
                if entry.get('message_id'):
                    # Note: This should be called with await in an async context
                    pass  # Will be handled by the main bot loop
            except:
                pass
            
            # Clean up all data structures
            entry = queue_entries.get(queue_id)
            if entry:
                user_id = entry.get('user_id')
                if user_id and user_id in user_to_queue_map:
                    del user_to_queue_map[user_id]
            
            del queue_entries[queue_id]
            
            # Remove from queue order
            if queue_id in queue_order:
                queue_order.remove(queue_id)
        
        return len(expired_queue_ids)
    
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
        
        # Try to delete the admin channel message
        if queue_entry.get('message_id') and self.channel_accessible:
            try:
                await self.bot.delete_message(
                    chat_id=ADMIN_CHANNEL_ID,
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
        
        return True, "success"