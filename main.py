import asyncio
import logging
import time
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from config import (
    BOT_TOKEN,
    AUTHORIZED_MEMBER_REFRESH_SECONDS,
    MESSAGES,
    validate_channel_access,
    SERVICES,
    enabled_services,
    get_service,
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
# Quiet httpx: its INFO logs print full Telegram API URLs, which contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

async def main():
    """Main function to start the bot"""
    
    # Validate configuration
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not found in environment variables")
        return

    runnable = enabled_services()
    if not runnable:
        logger.error("No runnable services configured (each needs an enabled flag and a channel). Aborting.")
        return
    for svc in runnable:
        if not svc.roster:
            logger.warning("No members configured for service '%s'", svc.key)

    # Initialize database
    logger.info("Initializing database connection...")
    db_available = db_mgr.initialize()
    if db_available:
        logger.info("✅ Database connected successfully")
        for svc in SERVICES.values():
            if svc.default_members:
                db_mgr.ensure_authorized_members_seed(svc.default_members, collection=svc.members_collection)
    else:
        logger.warning("🟡 Database unavailable - running in memory-only mode")

    async def refresh_all_rosters() -> None:
        if not db_mgr.db_available:
            return
        for svc in SERVICES.values():
            try:
                members = db_mgr.get_authorized_members(collection=svc.members_collection)
                if members is None:
                    # DB error for this collection -> keep the current roster (no-op)
                    continue
                if svc.roster.replace(members):
                    logger.info("Roster '%s' updated from database (%d entries)", svc.key, len(svc.roster))
                svc.roster.update_last_synced(time.time())
            except Exception as exc:
                logger.error("Error refreshing roster '%s': %s", svc.key, exc)

    async def refresh_authorized_members_periodically() -> None:
        while True:
            try:
                await refresh_all_rosters()
            except Exception as exc:
                logger.error("Error refreshing authorized members: %s", exc)
            await asyncio.sleep(AUTHORIZED_MEMBER_REFRESH_SECONDS)

    if db_available:
        await refresh_all_rosters()

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

    # Validate channel access for every enabled service
    logger.info("Validating channel access for enabled services...")
    for svc in enabled_services():
        logger.info("Service '%s' -> channel %s, members: %d", svc.key, svc.channel_id, len(svc.roster))
        channel_ok, channel_msg = await validate_channel_access(bot, svc.channel_id)
        if channel_ok:
            logger.info("✅ Channel access verified for '%s': %s", svc.key, channel_msg)
        else:
            logger.error("❌ Channel access failed for '%s': %s", svc.key, channel_msg)
            logger.error(
                "⚠️  Bot will continue but the '%s' queue may not work. Add the bot to the "
                "channel as admin (Send + Delete messages) and verify the channel id.",
                svc.key,
            )
    
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
                            member_label = get_service(expired.get("service")).member_label
                            await bot.send_message(
                                chat_id=user_id,
                                text=MESSAGES["queue_expired"].format(member=member_label)
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
