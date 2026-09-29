#!/usr/bin/env python3
"""
Boot-path tests. Two sections, one file, no network, no Mongo, no bot token.

Section 1 pins the python-telegram-bot API surface `main.py` actually depends on.
Everything probed here is constructed offline -- none of it performs I/O -- so a
PTB upgrade that moves one of these breaks a named test instead of the deployed
container at 3 a.m.

Section 2 drives the real `main.main()` against a fake Application and locks the
T3.5 invariant: NO UPDATE IS PROCESSED UNTIL REHYDRATION AND BOTH SWEEPS HAVE
COMPLETED. If polling starts first, a member can claim a queue entry while it is
still being rehydrated, and a stale entry can be claimed before it is expired.

`main.CommandHandler`, `main.MessageHandler`, `main.CallbackQueryHandler` and
`main.filters` are deliberately left REAL, so the boot test re-validates
`CommandHandler(["chat", "help"], ...)` against the installed PTB from inside the
real code path -- that alias is the single most version-sensitive line in main.py.

Note on `main()`'s error handling: it catches `Exception` and logs it (main.py's
`except Exception: logger.exception(...)`). A failure inside the `async with`
therefore shows up as a MISSING EVENT, not a raised error, so every ordering
assertion prints the observed `events` list, and `assert_no_boot_failure()`
fails loudly if main() swallowed anything.

Run directly: `python tests/test_boot.py`
"""

import asyncio
import inspect
import logging
import os
import re
import sys
from types import SimpleNamespace
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from config import ServiceType
import datetime

import telegram
from telegram import BotCommand
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          MessageHandler, TypeHandler, filters)

import main

# Obviously fake, but format-valid so Application.builder().token() accepts it.
# THIS IS NOT A REAL TOKEN AND MUST NEVER BE REPLACED WITH ONE -- this file runs
# in CI, where no secret is available to the test job by design.
FAKE_TOKEN = "123456789:AAEyTESTtokenTESTtokenTESTtokenTEST"

HF_CHANNEL = "-1009000000001"
PSS_CHANNEL = "-1009000000002"

# Every callback main() registers on the application.
HANDLER_CALLBACKS = (
    "start_command", "chat_command", "end_command", "status_command",
    "cancel_command", "available_command", "unavailable_command",
    "release_command", "register_command", "handle_message", "handle_sticker",
    "handle_photo",
    "handle_callback_query", "handle_error",
    "name_command", "note_private_contact",
)


# =========================================================== section 1: PTB contract

def test_command_handler_accepts_a_list_of_aliases():
    """main.py:117 -- one handler, two names. The T1.4 alias."""
    async def cb(update, context):
        return None

    handler = CommandHandler(["chat", "help"], cb)
    assert set(handler.commands) == {"chat", "help"}, (
        f"CommandHandler(['chat','help']) exposed {handler.commands!r}; /help would "
        "stop working for posters and existing users."
    )


def test_bot_commands_are_botcommand_objects():
    """Names themselves are asserted by test_copy.py::test_bot_command_menu; this
    only pins the type, which is what set_my_commands requires."""
    assert isinstance(main.BOT_COMMANDS, list) and main.BOT_COMMANDS
    for c in main.BOT_COMMANDS:
        assert isinstance(c, BotCommand), f"{c!r} is not a telegram.BotCommand"
    # Constructing one the same way main.py does must not raise.
    assert BotCommand("chat", "Request support (start here)").command == "chat"


def test_set_my_commands_signature():
    from telegram import Bot
    assert hasattr(Bot, "set_my_commands"), "telegram.Bot lost set_my_commands"
    params = [p for p in inspect.signature(Bot.set_my_commands).parameters if p != "self"]
    assert params and params[0] == "commands", (
        f"Bot.set_my_commands first parameter is {params[:1]}, expected ['commands']; "
        "main.register_bot_commands passes BOT_COMMANDS positionally."
    )


def test_application_supports_async_context_manager():
    """T3.5 boots inside `async with application:`."""
    assert hasattr(Application, "__aenter__") and hasattr(Application, "__aexit__"), (
        "Application is no longer an async context manager; main.py's `async with "
        "application:` would fail at boot."
    )


def test_application_builder_and_updater_offline():
    """Building an Application performs no I/O -- no getMe, no network."""
    async def probe():
        app = Application.builder().token(FAKE_TOKEN).build()
        assert app.updater is not None, "Application has no updater; start_polling is unreachable"
        params = inspect.signature(app.updater.start_polling).parameters
        assert "allowed_updates" in params, (
            "Updater.start_polling no longer accepts allowed_updates; main.py:209 passes "
            "allowed_updates=['message', 'callback_query']"
        )

    asyncio.run(probe())


def test_message_filters_resolve():
    for name, value in (("TEXT", filters.TEXT), ("COMMAND", filters.COMMAND),
                        ("Sticker.ALL", filters.Sticker.ALL), ("PHOTO", filters.PHOTO)):
        assert value is not None, f"filters.{name} is missing"
    # main.py:123 composes them; the operators must still work.
    assert (filters.TEXT & ~filters.COMMAND) is not None


# =========================================================== section 2: boot sequence

class BootFailureDetected(AssertionError):
    pass


class _ErrorCatcher(logging.Handler):
    """main() swallows Exception and logs it, so a broken boot looks like a missing
    event. Catch the log record instead of guessing."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _make_fakes(events):
    """Build the fake object graph main() will be driven against.

    `events` is the shared ordering record. `snapshot` captures the event list at
    the exact moment polling starts -- that, not a post-hoc scan, is what proves
    the boot work finished before the doors opened.
    """
    state = {"snapshot": None, "set_my_commands": [], "get_chat": [], "poll_kwargs": None}

    class FakeBot:
        async def get_chat(self, chat_id):
            state["get_chat"].append(chat_id)
            return SimpleNamespace(title=f"fake-channel-{chat_id}")

        async def set_my_commands(self, commands, *args, **kwargs):
            state["set_my_commands"].append(commands)
            events.append("set_my_commands")
            return True

    class FakeUpdater:
        async def start_polling(self, **kw):
            # Snapshot BEFORE recording the poll itself.
            state["snapshot"] = list(events)
            state["poll_kwargs"] = kw
            events.append("poll")
            return None

    class FakeApplication:
        def __init__(self):
            self.bot = FakeBot()
            self.updater = FakeUpdater()
            self.handlers = []
            self.groups = []        # the group each handler was added in, in step
            self.error_handlers = []

        # --- builder chain: Application.builder().token(t).build()
        @classmethod
        def builder(cls):
            inst = cls()

            class _Builder:
                def token(self, _t):
                    return self

                def build(self):
                    return inst

            return _Builder()

        def add_handler(self, handler, group=0):
            self.handlers.append(handler)
            self.groups.append(group)

        def add_error_handler(self, handler):
            self.error_handlers.append(handler)

        async def __aenter__(self):
            events.append("aenter")
            return self

        async def __aexit__(self, *exc):
            events.append("aexit")
            return False

        async def start(self):
            events.append("app_start")

    class FakeSessionManager:
        def __init__(self, bot):
            self.bot = bot

    class FakeQueueManager:
        def __init__(self, bot):
            self.bot = bot

        async def sweep_directed_requests(self):
            events.append("directed_sweep")
            return []

        async def sweep_expired_queues(self):
            events.append("queue_sweep")
            return []

    class FakeExpiryManager:
        def __init__(self, bot, session_manager):
            self.bot = bot
            self.session_manager = session_manager
            self.running = False

        async def run_once(self):
            events.append("expiry_sweep")

        async def start(self):
            self.running = True
            # Block forever; cancellation is how the test shuts main() down.
            await asyncio.Event().wait()

        def stop(self):
            # main()'s finally calls this unconditionally, even if start() was
            # never awaited -- it must tolerate that.
            events.append("stop")
            self.running = False

    class FakeHandlers:
        def __init__(self, session_manager, queue_manager):
            self.session_manager = session_manager
            self.queue_manager = queue_manager
            for name in HANDLER_CALLBACKS:
                setattr(self, name, self._make(name))

        @staticmethod
        def _make(name):
            async def cb(*args, **kwargs):
                return None
            cb.__name__ = name
            return cb

    class FakeDb:
        db_available = False

        def initialize(self):
            return False

    def fake_restore_state(queue_manager, session_manager):
        events.append("restore")
        return {"db_available": False, "enabled": True, "dry_run": False, "aborted": False,
                "pending_restored": 0, "pending_skipped": 0, "pending_stale_closed": 0,
                "active_restored": 0, "active_skipped": 0}

    return SimpleNamespace(
        state=state,
        Application=FakeApplication,
        SessionManager=FakeSessionManager,
        QueueManager=FakeQueueManager,
        SessionExpiryManager=FakeExpiryManager,
        BotHandlers=FakeHandlers,
        db_mgr=FakeDb(),
        restore_state=fake_restore_state,
    )


PATCHED = ("Application", "SessionManager", "QueueManager", "SessionExpiryManager",
           "BotHandlers", "db_mgr", "restore_state", "BOT_TOKEN")


def _install(fakes, token: Optional[str] = FAKE_TOKEN):
    """`token=None` is deliberate: it drives test_no_token_returns_immediately.
    Annotated Optional because Pyright otherwise infers `str` from the default and
    flags the very case this helper exists to support."""
    saved = {name: getattr(main, name) for name in PATCHED}
    for name in PATCHED:
        if name == "BOT_TOKEN":
            main.BOT_TOKEN = token
        else:
            setattr(main, name, getattr(fakes, name))
    return saved


def _restore(saved):
    for name, value in saved.items():
        setattr(main, name, value)


def _enable_services(hf_channel: Optional[str] = HF_CHANNEL,
                     pss_channel: Optional[str] = PSS_CHANNEL):
    """`channel=None` is deliberate: a service with no channel is not `runnable`,
    which is what drives test_no_runnable_service_returns_immediately. Same Pyright
    reason as _install for the Optional."""
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]
    hf.channel_id, hf.enabled = hf_channel, hf_channel is not None
    pss.channel_id, pss.enabled = pss_channel, pss_channel is not None


def assert_no_boot_failure(catcher, events):
    for rec in catcher.records:
        if "An error occurred" in rec.getMessage():
            raise BootFailureDetected(
                "main() swallowed an exception during boot (main.py catches Exception and "
                f"only logs it). Events so far: {events}\n"
                f"Logged: {rec.getMessage()}\n"
                + (logging.Formatter().formatException(rec.exc_info) if rec.exc_info else "")
            )


async def _drive_boot(events, fakes, catcher, timeout_ticks=200):
    """Run main() until polling starts, then cancel it and let its finally run."""
    task = asyncio.create_task(main.main())
    for _ in range(timeout_ticks):
        if "poll" in events:
            break
        if task.done():
            break
        await asyncio.sleep(0.01)
    else:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise AssertionError(
            f"start_polling was never reached within {timeout_ticks} ticks. "
            f"Observed events: {events}"
        )

    assert_no_boot_failure(catcher, events)
    assert "poll" in events, f"main() returned before polling. Events: {events}"

    task.cancel()
    # Bounded: main()'s finally awaits asyncio.wait(pending, timeout=5).
    await asyncio.wait_for(
        asyncio.gather(task, return_exceptions=True), timeout=15
    )
    return task


def test_boot_order_rehydrate_then_sweep_then_poll():
    events = []
    fakes = _make_fakes(events)
    catcher = _ErrorCatcher()
    logging.getLogger().addHandler(catcher)
    saved = _install(fakes)
    svc_saved = _save_services()
    try:
        _enable_services()
        asyncio.run(_drive_boot(events, fakes, catcher))
    finally:
        _restore(saved)
        _restore_services(svc_saved)
        logging.getLogger().removeHandler(catcher)

    snapshot = fakes.state["snapshot"]
    assert snapshot is not None, f"start_polling never recorded a snapshot. Events: {events}"

    why = (
        "\n\nT3.5 INVARIANT VIOLATED: no update may be processed until rehydration and "
        "both sweeps have completed. If polling opens first, a member can claim a queue "
        "entry while it is still being rehydrated, and a stale entry can be claimed "
        "before the sweep expires it -- on a live mental-health helpline that means "
        "someone is connected to a request that no longer exists.\n"
        f"Events at the moment polling started: {snapshot}\n"
        f"Full event list: {events}\n"
    )
    for name in ("restore", "directed_sweep", "queue_sweep", "expiry_sweep"):
        assert name in snapshot, f"{name!r} had not happened when polling started." + why

    # directed_sweep BEFORE queue_sweep, and that order is not cosmetic: handing a
    # lapsed directed request back resets its waiting_since, so sweeping expiry first
    # would expire a request the directed sweep was about to revive -- the requester
    # gets "they're not free, pick again" AND "your request expired", for the same
    # request, seconds apart.
    assert (snapshot.index("restore")
            < snapshot.index("directed_sweep")
            < snapshot.index("queue_sweep")
            < snapshot.index("expiry_sweep")), why

    # The boot-time sweeps run exactly once before the doors open; a second
    # "queue_sweep" in the snapshot would mean the periodic loop had already
    # started racing the boot sequence.
    assert snapshot.count("restore") == 1, why
    assert snapshot.count("directed_sweep") == 1, why
    assert snapshot.count("queue_sweep") == 1, why
    assert snapshot.count("expiry_sweep") == 1, why

    # The application must be up before any of that work runs.
    assert snapshot.index("aenter") < snapshot.index("restore"), why
    assert snapshot.index("app_start") < snapshot.index("restore"), why


def test_command_menu_is_published_before_rehydration():
    events, fakes, catcher = _run_boot()
    snapshot = fakes.state["snapshot"]
    assert "set_my_commands" in snapshot, f"command menu never published. Events: {events}"
    assert snapshot.index("set_my_commands") < snapshot.index("restore"), (
        f"the command menu must be published before rehydration. Events: {snapshot}"
    )
    assert fakes.state["set_my_commands"] == [main.BOT_COMMANDS], (
        f"set_my_commands got {fakes.state['set_my_commands']!r}, expected main.BOT_COMMANDS"
    )


def test_every_enabled_service_channel_is_validated():
    events, fakes, catcher = _run_boot()
    assert fakes.state["get_chat"] == [HF_CHANNEL, PSS_CHANNEL], (
        f"get_chat was called with {fakes.state['get_chat']!r}; expected one call per "
        f"enabled service, in order: {[HF_CHANNEL, PSS_CHANNEL]}"
    )


def test_start_polling_restricts_allowed_updates():
    events, fakes, catcher = _run_boot()
    kw = fakes.state["poll_kwargs"]
    assert kw is not None, f"start_polling was never called. Events: {events}"
    assert kw.get("allowed_updates") == ["message", "callback_query"], (
        f"start_polling got allowed_updates={kw.get('allowed_updates')!r}; widening this "
        "makes the bot process update types nothing handles."
    )


def test_shutdown_runs_the_finally_block():
    events, fakes, catcher = _run_boot()
    assert "aexit" in events, (
        f"the application context manager never exited; main() would leak the "
        f"Application on shutdown. Events: {events}"
    )
    assert "stop" in events, (
        f"SessionExpiryManager.stop() was never called. Events: {events}"
    )
    assert events.index("poll") < events.index("aexit") <= events.index("stop"), (
        f"shutdown ran out of order. Events: {events}"
    )


def _boot_and_capture_app():
    """Drive main() to polling against the fakes and return the Application it
    built. CommandHandler / MessageHandler / CallbackQueryHandler / TypeHandler /
    filters stay the REAL PTB classes."""
    events = []
    fakes = _make_fakes(events)
    catcher = _ErrorCatcher()
    logging.getLogger().addHandler(catcher)
    saved = _install(fakes)
    svc_saved = _save_services()
    app_holder = {}

    real_builder = fakes.Application.builder

    @classmethod
    def capturing_builder(cls):
        b = real_builder.__func__(cls)
        real_build = b.build

        def build():
            app = real_build()
            app_holder["app"] = app
            return app

        b.build = build
        return b

    fakes.Application.builder = capturing_builder
    try:
        _enable_services()
        asyncio.run(_drive_boot(events, fakes, catcher))
    finally:
        fakes.Application.builder = real_builder
        _restore(saved)
        _restore_services(svc_saved)
        logging.getLogger().removeHandler(catcher)
    return app_holder.get("app")


def test_all_handler_callbacks_are_registered_against_real_ptb():
    """The fake Application records handlers, but CommandHandler / MessageHandler /
    CallbackQueryHandler / filters are the REAL PTB classes, so this is a live check
    that main()'s registration block still constructs under the installed version."""
    app = _boot_and_capture_app()
    assert app is not None, "the fake Application was never built"
    # 1 TypeHandler + 10 CommandHandlers + 3 MessageHandlers + 1 CallbackQueryHandler
    assert len(app.handlers) == 15, (
        f"main() registered {len(app.handlers)} handlers, expected 15 "
        f"(1 type, 10 command, 3 message, 1 callback): {app.handlers!r}"
    )
    assert len(app.error_handlers) == 1, f"expected one error handler, got {app.error_handlers!r}"

    # WHICH callback is wired to WHICH handler, not just how many there are.
    # Counting handlers and collecting command names leaves every callback
    # identity unchecked: swapping handlers.handle_photo for handlers.handle_sticker
    # in main.py keeps the count unchanged and the command set identical, so the suite
    # stayed green while every photo a requester sends went through the sticker
    # path. The same held for handle_message <-> handle_callback_query and for
    # handle_error <-> handle_message.
    registered = [(type(h).__name__, h.callback.__name__) for h in app.handlers]
    assert registered == [
        # FIRST, in group -1: the capture point for PSS supporters' has_started_bot
        # and Telegram first name. It must see every update before anything else.
        ("TypeHandler", "note_private_contact"),
        ("CommandHandler", "start_command"),
        ("CommandHandler", "chat_command"),
        ("CommandHandler", "end_command"),
        ("CommandHandler", "status_command"),
        ("CommandHandler", "cancel_command"),
        ("CommandHandler", "available_command"),
        ("CommandHandler", "unavailable_command"),
        ("CommandHandler", "release_command"),
        # Registered unconditionally, so this list stays env-independent even
        # though the feature itself is inert without REGISTRATION_ADMIN_IDS. D41.
        ("CommandHandler", "register_command"),
        # Not in BOT_COMMANDS, and silent to anyone who is not an active PSS
        # supporter in a private chat.
        ("CommandHandler", "name_command"),
        ("MessageHandler", "handle_message"),
        ("MessageHandler", "handle_sticker"),
        ("MessageHandler", "handle_photo"),
        ("CallbackQueryHandler", "handle_callback_query"),
    ], f"main() wired its handlers differently: {registered!r}"

    # Group -1 for the capture hook, the default group 0 for everything else. A
    # capture hook in group 0 would never run for any update another group-0
    # handler takes -- i.e. for nearly all of them.
    assert app.groups == [-1] + [0] * 14, f"handler groups: {app.groups!r}"
    assert app.handlers[0].type is telegram.Update, (
        f"the capture hook must match every Update, got {app.handlers[0].type!r}")

    assert app.error_handlers[0].__name__ == "handle_error", (
        f"the error handler must be handlers.handle_error, got "
        f"{app.error_handlers[0].__name__!r}"
    )

    # The two singleton filters, by identity: a swap of the FILTERS rather than the
    # callbacks would leave the list above unchanged.
    # HARD-CODED INDICES (the TypeHandler at 0 and /name at 10 pushed them to 12
    # and 13). They move every time a handler is added ahead of them,
    # and a stale index still resolves to SOME handler, so the two asserts below
    # would keep passing while testing the wrong objects. The ordered-pair list
    # above is what pins them: keep the two in step.
    sticker_h, photo_h = app.handlers[12], app.handlers[13]
    assert sticker_h.filters is filters.Sticker.ALL, (
        f"handle_sticker must be filtered on filters.Sticker.ALL, got {sticker_h.filters!r}"
    )
    assert photo_h.filters is filters.PHOTO, (
        f"handle_photo must be filtered on filters.PHOTO, got {photo_h.filters!r}"
    )

    commands = set()
    for h in app.handlers:
        commands |= set(getattr(h, "commands", ()) or ())
    assert commands == {"start", "chat", "help", "end", "status", "cancel",
                        "available", "unavailable", "release", "register",
                        "name"}, (
        f"registered commands are {sorted(commands)}; /chat and its /help alias must "
        "both survive (main.py:117)"
    )


def test_an_unrecognised_command_reaches_no_handler():
    """Pins the premise of /name's SILENT refusal.

    name_command answers a non-supporter with nothing at all, because that is what
    an unrecognised command gets: no registered handler accepts '/foo'. If someone
    ever adds a catch-all (a CommandHandler fallback, a MessageHandler without
    ~filters.COMMAND, ...), an unknown command starts getting a reply and /name's
    silence becomes distinguishable from a command that does not exist -- so /name
    must then be routed through that same catch-all. This test is what notices.
    """
    app = _boot_and_capture_app()
    assert app is not None, "the fake Application was never built"

    cmd = "/definitelynotacommand"
    msg = telegram.Message(
        message_id=1,
        date=datetime.datetime.now(datetime.timezone.utc),
        chat=telegram.Chat(id=1, type="private"),
        from_user=telegram.User(id=1, first_name="X", is_bot=False),
        text=cmd,
        entities=(telegram.MessageEntity(type=telegram.MessageEntity.BOT_COMMAND,
                                         offset=0, length=len(cmd)),),
    )
    upd = telegram.Update(update_id=1, message=msg)

    for h in app.handlers:
        if isinstance(h, TypeHandler):
            # Matches everything by design; note_private_contact never replies
            # (proven in test_supporter_names).
            continue
        if isinstance(h, CommandHandler):
            assert "definitelynotacommand" not in h.commands, h.commands
            continue
        assert isinstance(h, (MessageHandler, CallbackQueryHandler)), h
        assert not h.check_update(upd), (
            f"{type(h).__name__}({h.callback.__name__}) accepts an unknown command, "
            "so unknown commands are no longer answered with silence -- route "
            "/name's refusal through the same path")


# --------------------------------------------------------------- negative cases

# Both negative cases assert that main() RETURNS. A bare asyncio.run(main.main())
# cannot express that: if the guard under test regresses, main() does not return,
# it falls through to `await asyncio.Event().wait()` in the `async with` block and
# runs forever. Measured with main.py's BOT_TOKEN guard neutralised: the suite hung
# until killed at 60s, exit 124, with no assertion message. In CI that consumes the
# test job's whole `timeout-minutes: 10` budget and the run reports "cancelled"
# rather than naming the broken invariant -- ten minutes of latency on every deploy
# to a live helpline, and a failure nobody can read.
#
# 10s is ~1000x what these take with fakes (they finish in milliseconds) and is far
# below the job timeout, so a slow runner cannot make this flaky. On a passing run
# the timeout never engages and the suite still finishes in ~2s.
#
# How the failure actually surfaces: wait_for cancels main() on expiry, and main.py
# CATCHES asyncio.CancelledError around `asyncio.Event().wait()`, so it unwinds
# through its own `finally` and returns normally -- which means wait_for returns
# rather than raising TimeoutError. So the timeout is what BOUNDS the hang; the
# caller's existing `assert events == []` is what REPORTS it, and it reports well,
# naming every boot step that should never have happened. The TimeoutError branch
# below is defence for a future main() that does not swallow cancellation.
_RETURN_TIMEOUT_SECONDS = 10


def _run_main_expecting_return(why):
    """Run main() under a time bound so a regressed guard fails instead of hanging."""
    async def _drive():
        try:
            await asyncio.wait_for(main.main(), timeout=_RETURN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise AssertionError(
                f"main() did not return within {_RETURN_TIMEOUT_SECONDS}s. {why} "
                "Instead it reached `await asyncio.Event().wait()` and would run "
                "forever, so the guard that should have returned early is gone."
            ) from None

    asyncio.run(_drive())


def test_no_token_returns_immediately():
    events = []
    fakes = _make_fakes(events)
    saved = _install(fakes, token=None)
    svc_saved = _save_services()
    try:
        _enable_services()
        _run_main_expecting_return(
            "With no BOT_TOKEN it must return at main.py's `if not BOT_TOKEN` guard."
        )
    finally:
        _restore(saved)
        _restore_services(svc_saved)
    assert events == [], (
        f"with no BOT_TOKEN main() must return before doing anything. Events: {events}"
    )


def test_no_runnable_service_returns_immediately():
    events = []
    fakes = _make_fakes(events)
    saved = _install(fakes)
    svc_saved = _save_services()
    try:
        # HF has no channel, PSS disabled -> enabled_services() is empty.
        _enable_services(hf_channel=None, pss_channel=None)
        assert config.enabled_services() == [], "precondition: no service may be runnable"
        _run_main_expecting_return(
            "With no runnable service it must return at main.py's "
            "'No runnable services configured' guard."
        )
    finally:
        _restore(saved)
        _restore_services(svc_saved)
    assert events == [], (
        f"with no runnable service main() must return at the 'No runnable services' "
        f"branch without building an Application. Events: {events}"
    )


# --------------------------------------------------------------------- helpers

def _save_services():
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]
    saved = (hf.channel_id, hf.enabled, list(hf.roster),
             pss.channel_id, pss.enabled, list(pss.roster),
             config.REGISTRATION_ADMINS)
    # This file has no reset_state(), so the registration allowlist is blanked
    # here. Every boot assertion must run with /register OFF, or a maintainer's
    # local .env (config.py calls load_dotenv() at import) changes what main()
    # logs. A REBIND, not a .clear(): REGISTRATION_ADMINS is a frozenset, and a
    # by-value importer would never see it -- which is why the production code
    # goes through config.is_registration_admin(). The handler itself is
    # registered UNCONDITIONALLY (D41), so the ordered-pair assertion below is
    # deliberately unaffected by this.
    config.REGISTRATION_ADMINS = frozenset()
    return saved


def _restore_services(saved):
    """A leaked hf.channel_id changes default_service_key() for every suite that
    runs later in the same process."""
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]
    (hf.channel_id, hf.enabled, hf_roster,
     pss.channel_id, pss.enabled, pss_roster,
     config.REGISTRATION_ADMINS) = saved
    hf.roster.replace(hf_roster)
    pss.roster.replace(pss_roster)


def _run_boot():
    """Boot once with both services enabled; return (events, fakes, catcher)."""
    events = []
    fakes = _make_fakes(events)
    catcher = _ErrorCatcher()
    logging.getLogger().addHandler(catcher)
    saved = _install(fakes)
    svc_saved = _save_services()
    try:
        _enable_services()
        asyncio.run(_drive_boot(events, fakes, catcher))
    finally:
        _restore(saved)
        _restore_services(svc_saved)
        logging.getLogger().removeHandler(catcher)
    return events, fakes, catcher


# --------------------------------------------------------------------------- CI gate
# The deploy job is `needs: test`, so this workflow file is the ONLY thing standing
# between a red suite and a live mental-health helpline. It names its suites one
# hard-coded step at a time, and nothing anywhere checked that the list was complete.
#
# A suite that is never invoked cannot go red. Add tests/test_foo.py, forget the
# workflow line, and every future regression it would have caught ships green --
# indistinguishable, from the outside, from a passing gate. The same happens on a
# rename, or when a merge drops a step. This branch alone added three suites and
# three hand-written steps; getting that right by hand is not a control.
#
# Deliberately excluded, and asserted to STAY excluded so the exclusion is a
# decision rather than an oversight:
CI_EXCLUDED_SUITES = {
    # Requires a live Mongo (MONGODB_URI) and refuses to run without one. It is an
    # operator tool, not a gate; running it in CI would either be a no-op or would
    # hand the test job a database credential, which that job must never have.
    "test_db_integration.py",
}


def _workflow_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".github", "workflows", "deploy.yml")


def test_every_suite_is_wired_into_the_deploy_gate():
    """Every tests/test_*.py either runs in CI or is explicitly excluded here."""
    tests_dir = os.path.dirname(os.path.abspath(__file__))
    on_disk = {f for f in os.listdir(tests_dir)
               if f.startswith("test_") and f.endswith(".py")}
    assert on_disk, "no suites found on disk -- discovery itself is broken"

    workflow = _workflow_path()
    assert os.path.isfile(workflow), (
        "the deploy workflow is not where this guard looks (%s). If it moved, move "
        "this check with it -- do not delete it." % workflow)

    with open(workflow, encoding="utf-8") as fh:
        body = fh.read()

    invoked = set(re.findall(r"run:\s*python\s+tests/(test_\w+\.py)", body))

    missing = sorted(on_disk - invoked - CI_EXCLUDED_SUITES)
    assert not missing, (
        "these suites exist but NOTHING runs them in CI, so they can never fail a "
        "deploy: %s. Add a step to .github/workflows/deploy.yml, or add the file to "
        "CI_EXCLUDED_SUITES with the reason." % ", ".join(missing))

    phantom = sorted(invoked - on_disk)
    assert not phantom, (
        "the workflow runs suites that do not exist: %s. `python` on a missing file "
        "exits non-zero, so this is a permanently red gate, not a silent one -- but "
        "fix the name." % ", ".join(phantom))

    still_excluded = sorted(CI_EXCLUDED_SUITES & invoked)
    assert not still_excluded, (
        "%s is listed as deliberately excluded but the workflow runs it. Pick one."
        % ", ".join(still_excluded))

    stale = sorted(CI_EXCLUDED_SUITES - on_disk)
    assert not stale, (
        "CI_EXCLUDED_SUITES names files that no longer exist: %s. A stale exclusion "
        "will silently forgive a future suite that happens to reuse the name."
        % ", ".join(stale))



if __name__ == "__main__":
    # main.py's boot logging is deliberately chatty; keep the test output readable
    # without hiding the ERROR record that a swallowed boot failure produces.
    logging.getLogger().setLevel(logging.WARNING)

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    # A driver that discovers its own tests reports success when it discovers
    # NOTHING. Verified: renaming the `test_` prefix in this file made it print
    # "All 0 tests passed!" and exit 0 -- a fully green CI step, in front of a
    # deploy to a live helpline, having run zero assertions. A refactor into a
    # class, a rename, an import shadow or a bad merge all reach that state.
    # Coverage here may grow; it may not silently shrink.
    assert len(tests) >= 16, (
        "expected at least 16 tests, collected %d (%s). Test discovery has "
        "regressed -- fix the discovery, do not lower this number."
        % (len(tests), ", ".join(t.__name__ for t in tests) or "none")
    )
    _svc = _save_services()
    try:
        for t in tests:
            t()
            print(f"OK  {t.__name__}")
    finally:
        _restore_services(_svc)
    print(f"\nAll {len(tests)} boot tests passed!")
