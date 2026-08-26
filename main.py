import asyncio
import logging
import time
from telegram import BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from config import (
    BOT_TOKEN,
    AUTHORIZED_MEMBER_REFRESH_SECONDS,
    validate_channel_access,
    SERVICES,
    enabled_services,
)
from src.bot.managers.session import SessionManager
from src.bot.managers.queue import QueueManager
from src.bot.managers.expiry import SessionExpiryManager
from src.bot.handlers import BotHandlers
from src.bot.restore import restore_state
from src.database.manager import db_mgr

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# Quiet httpx: its INFO logs print full Telegram API URLs, which contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# The Telegram command menu. /help is a live handler but deliberately NOT listed:
# showing both /chat and /help invites "what's the difference?" and undercuts /chat
# as the primary entry point. /start is offered by Telegram itself in new chats.
BOT_COMMANDS = [
    BotCommand("chat", "Request support (start here)"),
    BotCommand("status", "Check your queue or conversation status"),
    BotCommand("cancel", "Leave the queue if you're waiting"),
    BotCommand("end", "End your current conversation"),
]


async def register_bot_commands(bot) -> bool:
    """Publish the Telegram command menu. Never fatal -- a failure here must not
    abort boot, and re-running it every boot is idempotent and self-healing."""
    try:
        await bot.set_my_commands(BOT_COMMANDS)
        logger.info("✅ Command menu registered: %s", ", ".join("/" + c.command for c in BOT_COMMANDS))
        return True
    except Exception as exc:
        logger.warning("Could not register command menu (continuing): %s", exc)
        return False

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
                # include_inactive=False is MANDATORY and is the highest-risk line in
                # Phase 4. get_authorized_member_records defaults to True, while the
                # get_authorized_members call it replaces hard-filtered active != False.
                # Omitting it silently re-authorizes every deactivated member on the
                # next refresh -- a security regression with no user-visible symptom.
                records = db_mgr.get_authorized_member_records(
                    include_inactive=False,
                    collection=svc.members_collection,
                )
                if records is None:
                    # DB error for this collection -> keep the current roster (no-op).
                    # None and [] mean different things: [] is a genuinely empty roster.
                    continue
                if svc.roster.replace_records(records):
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
    # One handler, two names: /help stays alive for posters and existing users.
    application.add_handler(CommandHandler(["chat", "help"], handlers.chat_command))
    application.add_handler(CommandHandler("end", handlers.end_command))
    application.add_handler(CommandHandler("status", handlers.status_command))
    application.add_handler(CommandHandler("cancel", handlers.cancel_command))
    # Member-only commands. Deliberately absent from BOT_COMMANDS: set_my_commands
    # publishes ONE menu to every chat, and that menu is the requester's surface.
    application.add_handler(CommandHandler("available", handlers.available_command))
    application.add_handler(CommandHandler("unavailable", handlers.unavailable_command))
    
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
    logger.info("Starting Care Network Bot...")

    # Start periodic cleanup tasks
    async def queue_cleanup_loop():
        """Periodic task to clean up expired queue entries.

        All the work -- closing the DB row, retiring the channel post, notifying the
        requester -- lives in QueueManager.sweep_expired_queues, so boot-time and
        periodic expiry go through exactly one code path.
        """
        while True:
            try:
                expired = await queue_manager.sweep_expired_queues()
                if expired:
                    logger.info("Cleaned up %d expired queue entries", len(expired))
            except Exception as e:
                logger.error(f"Error during queue cleanup: {e}")

            # Wait 5 minutes before next cleanup
            await asyncio.sleep(300)

    # Pre-bound so the finally block can cancel them even if we never get that far:
    # they are created inside the `async with`, so an early failure would otherwise
    # leave these names unbound.
    queue_cleanup_task = None
    session_expiry_task = None
    authorized_members_task = None

    try:
        # Everything below runs inside the initialised application. Channel validation
        # used to run before initialize(); it happened to work, but the restore and
        # sweep steps message real users, so all of it belongs in here.
        async with application:
            await application.start()   # update processor up; polling NOT started yet

            # 1. Channel reachability
            logger.info("Validating channel access for enabled services...")
            for svc in enabled_services():
                logger.info("Service '%s' -> channel %s, members: %d",
                            svc.key, svc.channel_id, len(svc.roster))
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

            # 2. Publish the command menu (never fatal)
            await register_bot_commands(bot)

            # 3. Rehydrate from Mongo. Synchronous, no Telegram I/O, never raises.
            restore_state(queue_manager, session_manager)

            # 4. Retire anything already past its window BEFORE any update can be
            #    processed, so a member cannot claim an entry mid-rehydration and a
            #    stale entry cannot be claimed before it is expired.
            await queue_manager.sweep_expired_queues()
            await expiry_manager.run_once()

            # 5. Only now start the background loops.
            queue_cleanup_task = asyncio.create_task(queue_cleanup_loop())
            session_expiry_task = asyncio.create_task(expiry_manager.start())
            if db_available:
                authorized_members_task = asyncio.create_task(refresh_authorized_members_periodically())

            # 6. Open the doors.
            logger.info("Bot is running. Press Ctrl+C to stop.")
            await application.updater.start_polling(allowed_updates=["message", "callback_query"])

            # Keep running until interrupted
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass

    except KeyboardInterrupt:
        logger.info("Received interrupt signal. Shutting down...")
    except Exception:
        # logger.exception, not logger.error: deploy.sh only ever shows
        # `docker logs --tail 50`, and a one-line message with no traceback is not
        # enough to diagnose a boot failure on a live helpline.
        logger.exception("An error occurred; shutting down")
    finally:
        # Clean shutdown
        expiry_manager.stop()
        pending = [t for t in (queue_cleanup_task, session_expiry_task,
                               authorized_members_task) if t]
        for task in pending:
            task.cancel()
        # Actually wait for the cancellations to land. Without this the loop closes
        # with the tasks still pending, so a sweep interrupted between closing a
        # Mongo row and notifying its user never finishes either half.
        if pending:
            try:
                await asyncio.wait(pending, timeout=5)
            except Exception:
                logger.warning("Background tasks did not shut down cleanly", exc_info=True)
        logger.info("Bot stopped.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
    except Exception as e:
        print(f"Failed to start bot: {e}")
