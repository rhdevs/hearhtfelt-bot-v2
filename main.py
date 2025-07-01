import asyncio
import logging
from telegram import Bot
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from config import BOT_TOKEN, ADMIN_CHANNEL_ID, HEARTFELT_MEMBERS, validate_channel_access
from session_manager import SessionManager
from queue_manager import QueueManager
from bot_handlers import BotHandlers

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
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Create managers
    bot = application.bot
    session_manager = SessionManager(bot)
    queue_manager = QueueManager(bot)
    handlers = BotHandlers(session_manager, queue_manager)
    
    # Register handlers
    application.add_handler(CommandHandler("start", handlers.start_command))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("end", handlers.end_command))
    application.add_handler(CommandHandler("status", handlers.status_command))
    application.add_handler(CommandHandler("cancel", handlers.cancel_command))
    
    # Message handler for regular messages
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.handle_message))
    
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
    
    # Start periodic cleanup task
    async def cleanup_expired_queues():
        """Periodic task to clean up expired queue entries"""
        while True:
            try:
                expired_count = queue_manager.cleanup_expired_queues()
                if expired_count > 0:
                    logger.info(f"Cleaned up {expired_count} expired queue entries")
            except Exception as e:
                logger.error(f"Error during queue cleanup: {e}")
            
            # Wait 5 minutes before next cleanup
            await asyncio.sleep(300)
    
    # Start cleanup task
    cleanup_task = asyncio.create_task(cleanup_expired_queues())
    
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
        cleanup_task.cancel()
        logger.info("Bot stopped.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
    except Exception as e:
        print(f"Failed to start bot: {e}")