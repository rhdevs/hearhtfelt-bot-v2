import asyncio
import logging
import time
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from config import (
    BOT_TOKEN,
    ADMIN_CHANNEL_ID,
    HEARTFELT_MEMBERS,
    DEFAULT_HEARTFELT_MEMBERS,
    AUTHORIZED_MEMBER_REFRESH_SECONDS,
    MESSAGES,
    validate_channel_access,
)
from src.bot.managers.session import SessionManager
from src.bot.managers.queue import QueueManager
from src.bot.managers.expiry import SessionExpiryManager
from src.bot.handlers import BotHandlers
from src.database.manager import db_mgr

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

async def main():
    """Main function to start the bot"""
    
    # Validate configuration
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not found in environment variables")
        return
    
    if not ADMIN_CHANNEL_ID:
        logger.error("ADMIN_CHANNEL_ID not found in environment variables")
        return
    
    if not HEARTFELT_MEMBERS:
        logger.warning("No HEARTFELT_MEMBERS configured. Please add authorized user IDs to config.py")
    
    # Initialize database
    logger.info("Initializing database connection...")
    db_available = db_mgr.initialize()
    if db_available:
        logger.info("✅ Database connected successfully")
        db_mgr.ensure_authorized_members_seed(DEFAULT_HEARTFELT_MEMBERS)
    else:
        logger.warning("🟡 Database unavailable - running in memory-only mode")

    async def refresh_authorized_members() -> None:
        if not db_mgr.db_available:
            return

        members = db_mgr.get_authorized_members()
        if members is None:
            return

        if HEARTFELT_MEMBERS.replace(members):
            logger.info(
                "Authorized Heartfelt member list updated from database (%d entries)",
                len(HEARTFELT_MEMBERS),
            )
        HEARTFELT_MEMBERS.update_last_synced(time.time())

    async def refresh_authorized_members_periodically() -> None:
        while True:
            try:
                await refresh_authorized_members()
            except Exception as exc:
                logger.error("Error refreshing authorized members: %s", exc)
            await asyncio.sleep(AUTHORIZED_MEMBER_REFRESH_SECONDS)

    if db_available:
        await refresh_authorized_members()

    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Create managers
    bot = application.bot
    session_manager = SessionManager(bot)
    queue_manager = QueueManager(bot)
    expiry_manager = SessionExpiryManager(bot, session_manager)
    handlers = BotHandlers(session_manager, queue_manager)
    
    # Register handlers
    application.add_handler(CommandHandler("start", handlers.start_command))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("end", handlers.end_command))
    application.add_handler(CommandHandler("status", handlers.status_command))
    application.add_handler(CommandHandler("cancel", handlers.cancel_command))
    
    # Message handler for regular messages
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message))
    
    # Sticker handler for stickers during conversations
    application.add_handler(MessageHandler(filters.Sticker.ALL, handlers.handle_sticker))
    
    # Photo handler for photos during conversations
    application.add_handler(MessageHandler(filters.PHOTO, handlers.handle_photo))
    
    # Callback query handler for inline keyboards
    application.add_handler(CallbackQueryHandler(handlers.handle_callback_query))
    
    # Error handler
    application.add_error_handler(handlers.handle_error)
    
    # Start the bot
    logger.info("Starting Heartfelt Anonymous Helpline Bot...")
    logger.info(f"Admin Channel ID: {ADMIN_CHANNEL_ID}")
    logger.info(f"Authorized Heartfelt Members: {len(HEARTFELT_MEMBERS)}")
    
    # Validate channel access
    logger.info("Validating admin channel access...")
    channel_ok, channel_msg = await validate_channel_access(bot, ADMIN_CHANNEL_ID)
    if channel_ok:
        logger.info(f"✅ Channel access verified: {channel_msg}")
    else:
        logger.error(f"❌ Channel access failed: {channel_msg}")
        logger.error("⚠️  Bot will continue but queue system may not work properly")
        logger.error("📋 Setup instructions:")
        logger.error("   1. Add bot to admin channel as administrator")
        logger.error("   2. Grant permissions: Send Messages, Delete Messages")
        logger.error("   3. Verify ADMIN_CHANNEL_ID is correct")
    
    # Start periodic cleanup tasks
    async def cleanup_expired_queues():
        """Periodic task to clean up expired queue entries"""
        while True:
            try:
                expired_entries = queue_manager.cleanup_expired_queues()
                if expired_entries:
                    logger.info("Cleaned up %d expired queue entries", len(expired_entries))
                    for expired in expired_entries:
                        user_id = expired.get("user_id")
                        if not user_id:
                            continue
                        try:
                            await bot.send_message(
                                chat_id=user_id,
                                text=MESSAGES["queue_expired"]
                            )
                        except Exception as send_error:
                            logger.warning(
                                "Failed to notify user %s about queue expiry: %s",
                                user_id,
                                send_error
                            )
            except Exception as e:
                logger.error(f"Error during queue cleanup: {e}")
            
            # Wait 5 minutes before next cleanup
            await asyncio.sleep(300)
    
    # Start cleanup tasks
    queue_cleanup_task = asyncio.create_task(cleanup_expired_queues())
    session_expiry_task = asyncio.create_task(expiry_manager.start())
    authorized_members_task = None
    if db_available:
        authorized_members_task = asyncio.create_task(refresh_authorized_members_periodically())
    
    try:
        # Run the bot
        logger.info("Bot is running. Press Ctrl+C to stop.")
        
        # Start polling and run until stopped
        async with application:
            await application.start()
            await application.updater.start_polling(allowed_updates=["message", "callback_query"])
            
            # Keep running until interrupted
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
        
    except KeyboardInterrupt:
        logger.info("Received interrupt signal. Shutting down...")
    except Exception as e:
        logger.error(f"An error occurred: {e}")
    finally:
        # Clean shutdown
        queue_cleanup_task.cancel()
        expiry_manager.stop()
        session_expiry_task.cancel()
        if authorized_members_task:
            authorized_members_task.cancel()
        logger.info("Bot stopped.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
    except Exception as e:
        print(f"Failed to start bot: {e}")
