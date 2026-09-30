import datetime
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
    CB_REG_APPROVE,
    CB_REG_REJECT,
    REGISTRATION_REAPPLY_HOURS,
    CB_PICK_LIST,
    CB_PICK_OPEN,
    CB_PICK_SELECT,
    SERVICES,
    UserState,
    picker_views,
    user_states,
    user_to_queue_map,
    user_to_service_map,
    MESSAGES,
    PHOTO_SHARING_ENABLED,
    is_any_member,
    is_member_of_service,
    # Imported as FUNCTIONS, never as the REGISTRATION_ADMINS frozenset itself:
    # this module imports from config BY VALUE, so a rebind of that frozenset
    # would be invisible here. A function defined inside config.py reads config's
    # own global at call time, which is what makes the allowlist testable and
    # what makes "evaluated at DECISION time" true. See config.py.
    is_registration_admin,
    registration_admin_ids,
    registration_is_enabled,
    REGISTER_DEEP_LINK_PAYLOAD,
    enabled_services,
    default_service_key,
    get_service,
    help_request_text,
    directed_fork_offered,
    listed_name,
    name_taken_by_other,
    omitted_supporters,
    picker_entries,
    supporter_label,
    supporter_name_problem,
)
from src.bot.managers.session import SessionManager
from src.bot.managers.queue import QueueManager, SelfClaimError
from src.database.manager import db_mgr
from src.supporter_names import (
    CAPTURED_NAME_MAX_LENGTH,
    NAME_MAX_LENGTH,
    RESET_KEYWORD,
    clean_name,
)
from src.timeutil import ensure_aware_utc, format_hhmm, utcnow

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
            self._record_started_on(svc, user_id)

    def _record_started_on(self, svc, user_id) -> None:
        """_record_bot_started for ONE track. Plain, not async, on purpose: nothing
        may await between the Mongo write and the memory write (see
        _record_first_name_on for why that matters)."""
        profile = svc.roster.profile(user_id)
        if profile is not None and profile.has_started_bot:
            return
        try:
            db_mgr.mark_member_started(user_id, True,
                                       collection=svc.members_collection)
        except Exception as exc:
            logger.warning("Could not record bot-started for member %s (%s): %s",
                           user_id, svc.key, exc)
        svc.roster.mark_started(user_id, True)

    def _record_first_name_on(self, svc, user_id, first_name) -> None:
        """Capture the Telegram first name this member's own private update carried.

        It is the automatic listed name (a /name override always wins), so it is
        refreshed on every contact and a rename on Telegram follows through.

        Why the 5-minute roster refresh cannot clobber this: main.refresh_all_rosters
        reads Mongo and replaces memory with no await in between, and this writes
        Mongo FIRST and memory SECOND, also with no await in between. On one event
        loop the two therefore run strictly one after the other, and either order
        ends with the new name in both places. A failed Mongo write still updates
        memory: the next refresh reverts it, and the next contact re-captures it, so
        it self-heals rather than sticking.
        """
        captured = clean_name(first_name)[:CAPTURED_NAME_MAX_LENGTH].strip()
        if not captured:
            # Telegram always sends a first name. An empty one here is a malformed
            # update, and it must never erase a good name we already have.
            return
        profile = svc.roster.profile(user_id)
        if profile is not None and profile.telegram_first_name == captured:
            # The hot path, taken on nearly every message: a dict lookup, no Mongo.
            return

        old_first = profile.telegram_first_name if profile else None
        old_listed = listed_name(profile)
        try:
            db_mgr.set_member_first_name(user_id, captured,
                                         collection=svc.members_collection)
        except Exception as exc:
            logger.warning("Could not record the Telegram first name of member %s "
                           "(%s): %s", user_id, svc.key, exc)
        base_doc = None
        if profile is None:
            # No profile in memory, only a bare roster id (e.g. an approval whose doc
            # read failed). Build it from Mongo, never from nothing: a blank profile
            # would list somebody under the real first name they chose /name to
            # hide, and as free when they said /unavailable.
            # Read AFTER the write above, so the doc already carries this first name.
            # Synchronous, like everything here -- see the refresh argument in the
            # docstring.
            try:
                base_doc = db_mgr.get_member_profile_doc(
                    user_id, collection=svc.members_collection)
            except Exception as exc:
                logger.warning("Could not read the '%s' record of member %s: %s",
                               svc.key, user_id, exc)
        # create_if_missing: a roster id with no profile would otherwise stay
        # unlisted until the next refresh. The profile is built from base_doc, so it
        # keeps the override and availability; blank only when Mongo is unreadable,
        # and never created at all for a doc that says active: False.
        svc.roster.set_first_name(user_id, captured, create_if_missing=True,
                                  base_doc=base_doc)
        new_listed = listed_name(svc.roster.profile(user_id))
        logger.info("%s supporter %s: Telegram first name %r -> %r (listed as %r -> %r)",
                    svc.key, user_id, old_first, captured, old_listed, new_listed)
        if new_listed == "" and clean_name(profile.display_name if profile else "") == "":
            # The id only: the name itself is already on the line above.
            logger.info("%s supporter %s cannot be listed under their Telegram first "
                        "name; /name sets one", svc.key, user_id)

    def _note_supporter_contact(self, user) -> None:
        """THE capture point for tracks that list supporters by name: records
        has_started_bot and the Telegram first name on every supporter_names track
        this user is a member of.

        HF (supporter_names False) is never touched here, which keeps HF
        byte-identical: its has_started_bot backfill stays in handle_message and
        start_command exactly as before.
        """
        for svc in SERVICES.values():
            if not svc.supporter_names or user.id not in svc.roster:
                continue
            self._record_started_on(svc, user.id)
            self._record_first_name_on(svc, user.id, getattr(user, "first_name", None))

    async def note_private_contact(self, update, context) -> None:
        """The group -1 TypeHandler callback. Group -1 runs before every other
        handler for every update, so every private message, command or button tap
        from a supporter refreshes what the bot knows about them -- whichever
        handler then takes the update, and even when none does.

        It replies to nobody, sends nothing and NEVER raises: an exception here
        would reach handle_error on an update that is otherwise fine.
        """
        try:
            if not self._is_private_chat(update):
                return
            user = getattr(update, "effective_user", None)
            if user is None:
                return
            self._note_supporter_contact(user)
        except Exception:
            logger.warning("Supporter contact capture failed; continuing", exc_info=True)

    # ------------------------------------------------------------------ /name
    async def name_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/name: a PSS supporter sees, sets or resets the name students see.

        Deliberately absent from set_my_commands, and SILENT to everybody else.
        """
        user = getattr(update, "effective_user", None)
        message = getattr(update, "message", None)
        if user is None or message is None:
            return

        svc = next((s for s in SERVICES.values()
                    if s.supporter_names and user.id in s.roster), None)
        if svc is None or not self._is_private_chat(update):
            # No reply, no send, no DB call. This is EXACTLY what an unrecognised
            # command gets: main.py registers no handler for those, and
            # handle_message is filtered on ~filters.COMMAND, so '/foo' is answered
            # with silence. Replying MESSAGES['unknown_command'] here would make
            # /name distinguishable from a command that does not exist, and so tell
            # a stranger that a supporter roster exists. Students, HF-only members,
            # deactivated members and every group/channel land here. test_boot's
            # unknown-command guard pins the premise: if a catch-all is ever
            # added, /name must be routed through it.
            logger.debug("/name ignored for user %s (not an active named-track "
                         "supporter in a private chat)", user.id)
            return

        # So the answer reflects their CURRENT Telegram first name, even though the
        # group -1 hook normally got there first.
        self._note_supporter_contact(user)

        # message.text, NOT context.args: args are whitespace-split, which would
        # silently turn "Sam\nLee" into "Sam Lee" and hide the newline the rules
        # reject. "/name@botname Sam" splits the same way.
        raw = message.text or ""
        parts = raw.split(None, 1)
        arg = parts[1] if len(parts) > 1 else ""

        # Every reply in this flow is PLAIN TEXT, no parse_mode: a name is
        # supporter-typed text and must never be interpreted as markup.
        if not arg.strip():
            await message.reply_text(self._name_status_text(svc, user.id))
            return
        if arg.strip().casefold() == RESET_KEYWORD:
            await self._reset_name(message, svc, user.id)
            return
        await self._set_name(message, svc, user.id, arg)

    def _name_status_text(self, svc, user_id) -> str:
        profile = svc.roster.profile(user_id)
        name = listed_name(profile)
        label = supporter_label(svc.key, user_id) or name
        if not name:
            text = MESSAGES["name_current_none"]
        elif clean_name(profile.display_name):
            text = MESSAGES["name_current_chosen"].format(name=label)
        else:
            text = MESSAGES["name_current_telegram"].format(name=label)
        if name and label != name:
            text += "\n\n" + MESSAGES["name_suffix_note"]
        if not (svc.runnable and svc.directed_enabled):
            text += "\n\n" + MESSAGES["name_list_off_note"]
        return text

    def _write_display_name(self, svc, user_id, new):
        """Mongo FIRST, memory SECOND, no await anywhere in here.

        Returns (reply_key_or_None, suffix, old_chosen_from_mongo_or_None).
        reply_key 'missing' means: answer with SILENCE (not an active member in
        Mongo, the same path as the unknown-command refusal). Memory is changed only
        when the write landed, so a refresh can never silently undo a name the
        supporter was told was saved.
        """
        if db_mgr.db_available:
            try:
                outcome, before = db_mgr.set_member_display_name(
                    user_id, new, collection=svc.members_collection)
            except Exception as exc:
                logger.warning("Could not save the chosen name of member %s (%s): %s",
                               user_id, svc.key, exc)
                outcome, before = "error", None
            if outcome == "missing":
                logger.info("%s /name from %s refused: not an active member in the "
                            "database", svc.key, user_id)
                return "missing", "", None
            if outcome != "ok":
                return "availability_failed", "", None
            svc.roster.set_display_name(user_id, new)
            return None, "", (before or {}).get("display_name")
        if not svc.roster.set_display_name(user_id, new):
            return "availability_failed", "", None
        return None, "\n\n" + MESSAGES["availability_memory_only"], None

    async def _set_name(self, message, svc, user_id, arg) -> None:
        problem = supporter_name_problem(arg)
        if problem:
            await message.reply_text(MESSAGES[problem])
            return
        new = clean_name(arg)
        profile = svc.roster.profile(user_id)
        if profile is not None and clean_name(profile.display_name) == new:
            await message.reply_text(MESSAGES["name_unchanged"])
            return
        if name_taken_by_other(svc.key, user_id, new):
            await message.reply_text(MESSAGES["name_taken"])
            return

        old_listed = listed_name(profile)
        old_chosen = clean_name(profile.display_name) if profile else ""
        failure, suffix, stored_before = self._write_display_name(svc, user_id, new)
        if failure == "missing":
            return
        if failure:
            await message.reply_text(MESSAGES[failure])
            return
        old_chosen = stored_before or old_chosen
        new_listed = listed_name(svc.roster.profile(user_id)) or new
        logger.info("%s supporter %s changed their listed name: %r -> %r "
                    "(chosen name %r -> %r)",
                    svc.key, user_id, old_listed, new_listed, old_chosen, new)
        await message.reply_text(
            MESSAGES["name_saved"].format(name=supporter_label(svc.key, user_id) or new)
            + suffix)

    async def _reset_name(self, message, svc, user_id) -> None:
        profile = svc.roster.profile(user_id)
        if profile is None or clean_name(profile.display_name) == "":
            await message.reply_text(MESSAGES["name_reset_nothing"])
            return

        old_listed = listed_name(profile)
        old_chosen = clean_name(profile.display_name)
        failure, suffix, stored_before = self._write_display_name(svc, user_id, "")
        if failure == "missing":
            return
        if failure:
            await message.reply_text(MESSAGES[failure])
            return
        old_chosen = stored_before or old_chosen
        new_listed = listed_name(svc.roster.profile(user_id))
        logger.info("%s supporter %s changed their listed name: %r -> %r "
                    "(chosen name %r -> %r)",
                    svc.key, user_id, old_listed, new_listed, old_chosen, "")
        if new_listed:
            label = supporter_label(svc.key, user_id) or new_listed
            await message.reply_text(MESSAGES["name_reset_done"].format(name=label) + suffix)
        else:
            await message.reply_text(MESSAGES["name_reset_done_no_name"] + suffix)

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        user_states[user_id] = UserState.IDLE

        # Deep link: t.me/<bot>?start=register lands here with args == ["register"].
        # It is how a prospective supporter finds registration at all, because
        # /register is deliberately NOT in set_my_commands -- that menu is what
        # somebody who opened the bot in distress sees, and a recruitment prompt
        # does not belong beside "Request support".
        #
        # Gated on registration_is_enabled() so the link cannot betray that the
        # feature exists while it is switched off: with an empty allowlist this
        # falls through and answers with the ordinary welcome, byte-identical to a
        # bare /start. register_command re-checks every gate for itself.
        # getattr, not context.args: PTB always sets it for a CommandHandler, but the
        # existing test fakes build SimpleNamespace contexts without it -- the same
        # reason _is_private_chat reaches for getattr on effective_chat.
        args = getattr(context, "args", None)
        if (args and args[0] == REGISTER_DEEP_LINK_PAYLOAD
                and registration_is_enabled()):
            await self.register_command(update, context)
            return

        text = MESSAGES["welcome"]
        # /available, /unavailable and /release are member-only, so they are
        # deliberately NOT in set_my_commands -- that menu is the requester's
        # surface. This addendum is how a supporter discovers them instead.
        if self._is_private_chat(update) and is_any_member(user_id):
            self._record_bot_started(user_id)
            # A member of a track that lists supporters by name also has /name.
            # An HF-only member's text is byte-identical to before. `enabled`, the
            # same gate is_any_member applies, so a switched-off PSS track does not
            # change what an HF member who is also on its roster is told.
            named = any(s.enabled and s.supporter_names and user_id in s.roster
                        for s in SERVICES.values())
            text = text + "\n\n" + MESSAGES[
                "member_addendum_named" if named else "member_addendum"]

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
        if svc.supporter_names:
            # A named track lists everyone with a name and marks the busy ones, and
            # the name defaults to the Telegram first name -- so "no name" means
            # "nothing usable", which the supporter fixes themselves with /name.
            if profile is None or not listed_name(profile):
                await update.message.reply_text(MESSAGES["availability_needs_name"] + suffix)
                return
            text = (MESSAGES["now_available_named"] if available
                    else MESSAGES["now_unavailable_named"])
            await update.message.reply_text(text + suffix)
            return

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
            # Refused WITHOUT ending: the session survives untouched. The copy sends
            # them to /release, which keeps the requester's request alive.
            #
            # UNLESS /release cannot run. It requires Mongo (the description lives
            # only on the document), so with Mongo down a supporter who says /end is
            # told to use /release and /release then refuses them. Before this
            # command became requester-only they could always leave; that combination
            # leaves a volunteer sealed inside a conversation with no exit but the
            # IDLE timeout -- which never fires while the other person keeps typing.
            #
            # So in that degraded mode only, /end is honoured. It is the same escape
            # hatch they had before, restricted to the one case where the intended
            # route is genuinely unavailable, and the requester still gets the normal
            # closing message rather than being told they were dropped.
            if db_mgr.db_available:
                await update.message.reply_text(MESSAGES["end_is_requester_only"])
                return
            logger.warning(
                "Member %s ended session %s directly: Mongo is unavailable, so "
                "/release could not have handed it back", user_id, session_id)

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

    # --- self-service supporter registration (/register) --------------------
    #
    # Approval here grants somebody the ability to read messages from students in
    # crisis. Every gate below is a deliberate refusal, not a convenience check,
    # and the ORDER of them is load-bearing. See D33-D45.

    # Telegram error text that means "this chat does not exist / we are not
    # allowed to write to it", as opposed to a transient failure. One tuple, two
    # readers: the admin fan-out (which escalates to ERROR and points at R13) and
    # the post-approval DM (which clears has_started_bot so the picker never
    # offers somebody the bot cannot reach). Identical rule to D16.
    _UNREACHABLE_MARKERS = ("bot can't initiate", "can't initiate conversation",
                            "blocked", "forbidden")

    @staticmethod
    def _looks_unreachable(exc) -> bool:
        lowered = str(exc).lower()
        return any(marker in lowered for marker in BotHandlers._UNREACHABLE_MARKERS)

    async def register_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /register: a prospective supporter asks to join the team."""
        user = update.effective_user
        user_id = user.id

        # 1. INERTNESS GATE #1. With an empty allowlist there is nobody who could
        #    ever approve, so the command must be indistinguishable from one that
        #    does not exist. This string is BYTE-IDENTICAL to what handle_message
        #    sends for gibberish and what _set_availability sends to a non-member.
        #    Confirming the command exists would advertise a recruitment surface
        #    to somebody who opened this bot in distress. D41/D42.
        if not registration_is_enabled():
            await update.message.reply_text(MESSAGES["unknown_command"])
            return

        # 2. Private chat only, and NOTHING is said in the group. A /register in a
        #    group would broadcast the applicant's intent to that group, and a
        #    group message is not proof the bot may DM them -- which is the entire
        #    point of capturing this. An update with no effective_chat at all
        #    lands here too: _is_private_chat fails CLOSED. Note the deliberate
        #    absence of any update.message.reply_text on this path.
        if not self._is_private_chat(update):
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=MESSAGES["registration_private_only"])
            except Exception as exc:
                # Usually expected: there is no private chat, which is the point.
                logger.info("Could not nudge %s to /register privately: %s",
                            user_id, exc)
            logger.info("Ignored a /register from %s sent outside a private chat",
                        user_id)
            return

        # 3. Degraded mode fails CLOSED. A memory-only registration would evaporate
        #    on restart while the applicant believed it pending, and there would be
        #    no atomic gate at all. Same precedent as /release. D36/R12.
        if not db_mgr.db_available:
            logger.warning("/register from %s refused: our records are offline",
                           user_id)
            await update.message.reply_text(MESSAGES["registration_unavailable"])
            return

        # 4. Already on a roster. The ROSTER-WIDE check, deliberately not
        #    is_any_member(): that one filters on svc.enabled, so a member of a
        #    track that is not switched on yet would be told to register again.
        if any(user_id in s.roster for s in SERVICES.values()):
            # Free and correct here: this IS a private message from them.
            self._record_bot_started(user_id)
            await update.message.reply_text(MESSAGES["registration_already_member"])
            return

        # 5. Duplicate pending. Mongo-backed, so it survives a restart.
        existing = db_mgr.get_pending_registration(user_id)
        if existing:
            if existing.get('admin_messages'):
                await update.message.reply_text(
                    MESSAGES["registration_already_pending"])
                return
            # Nobody was ever SHOWN this row: the previous attempt died between
            # creation and fan-out. A request nobody has seen is not a pending
            # request, and without this branch one crash blocks that person
            # forever. Supersede it and fall through to a fresh one.
            logger.warning("Superseding registration %s for %s: the row was created "
                           "but never delivered to any admin",
                           existing.get('registration_id'), user_id)
            try:
                db_mgr.close_registration(existing.get('registration_id'),
                                          'never_delivered')
            except Exception as exc:
                logger.warning("Could not close undelivered registration %s: %s",
                               existing.get('registration_id'), exc)

        # 6. Cooldown after a decision. If the timestamp is unusable we ALLOW:
        #    fail toward letting a person reach a human, never toward silently
        #    blocking them on a clock we cannot trust. D43.
        last = db_mgr.get_last_decided_registration(user_id)
        if last and last.get('status') == 'rejected':
            decided_at = ensure_aware_utc(last.get('decided_at'))
            if decided_at is None:
                logger.warning("Registration %s for %s carries an unusable "
                               "decided_at (%r); allowing the re-application rather "
                               "than blocking somebody on a clock we cannot trust",
                               last.get('registration_id'), user_id,
                               last.get('decided_at'))
            elif (utcnow() - decided_at
                    < datetime.timedelta(hours=REGISTRATION_REAPPLY_HOURS)):
                await update.message.reply_text(MESSAGES["registration_cooldown"])
                return

        # 7. Fan-out targets. EXCLUDING THE APPLICANT closes the only
        #    self-elevation path at source: an admin who runs /register on
        #    themselves never receives a card they could tap.
        targets = [a for a in registration_admin_ids() if a != user_id]
        if not targets:
            logger.error("/register from %s can be sent to nobody: the only "
                         "registration admin is the applicant themselves", user_id)
            await update.message.reply_text(MESSAGES["registration_unavailable"])
            return

        # 8. Create the row BEFORE the fan-out, so a crash mid-fan-out still leaves
        #    something a surviving admin's card can act on. Step 5's
        #    empty-admin_messages clause is the counterweight.
        attempt = db_mgr.count_registrations(user_id) + 1
        registration_id = db_mgr.create_registration(
            user_id, username=user.username, first_name=user.first_name,
            last_name=user.last_name, attempt=attempt)
        if not registration_id:
            logger.error("Could not create a registration row for applicant %s",
                         user_id)
            await update.message.reply_text(MESSAGES["registration_unavailable"])
            return

        # Render from the STORED document where we can, so the card an admin sees
        # now is built the same way as the settled card after a restart.
        reg = db_mgr.get_registration(registration_id) or {
            'registration_id': registration_id, 'telegram_id': user_id,
            'username': user.username, 'first_name': user.first_name,
            'last_name': user.last_name, 'created_at': utcnow(),
            'attempt': attempt,
        }

        # 9. Fan out, in the deterministic sorted order. NEVER raises out of the
        #    loop: one unreachable admin must not cost the other one their card.
        card = self._registration_card(reg)
        keyboard = self._registration_keyboard(registration_id)
        delivered = []
        for admin_id in targets:
            try:
                sent = await context.bot.send_message(
                    chat_id=admin_id, text=card, reply_markup=keyboard,
                    parse_mode='HTML')
            except Exception as exc:
                if self._looks_unreachable(exc):
                    logger.error(
                        "Registration admin %s cannot be DMed by the bot. They must "
                        "send the bot /start (or any private message) before they "
                        "can approve anybody. See runbook R13. (%s)", admin_id, exc)
                else:
                    logger.warning("Could not send the registration card to admin "
                                   "%s: %s", admin_id, exc)
                continue
            message_id = getattr(sent, "message_id", None)
            if message_id is None:
                # A card we cannot address is a card whose buttons we could never
                # strip later. Do not record it as delivered.
                logger.warning("The registration card sent to admin %s returned no "
                               "message_id; its buttons could not be settled later",
                               admin_id)
                continue
            delivered.append({'admin_id': admin_id, 'message_id': message_id})

        # 10. Outcome.
        if delivered:
            try:
                db_mgr.set_registration_admin_messages(registration_id, delivered)
            except Exception as exc:
                logger.warning("Could not record the admin cards for registration "
                               "%s: %s", registration_id, exc)
            logger.info("Registration %s from %s was sent to %d of %d admins",
                        registration_id, user_id, len(delivered), len(targets))
            await update.message.reply_text(MESSAGES["registration_submitted"])
            return

        # NOBODY was reached. The applicant must NEVER be told this is pending.
        # Somebody who has just volunteered, believing their offer is with the team
        # when in fact nobody will ever see it, is the worst available outcome --
        # so the row is closed and they are told honestly. See D34 and M28.
        try:
            db_mgr.close_registration(registration_id, 'undeliverable')
        except Exception as exc:
            logger.warning("Could not close undeliverable registration %s: %s",
                           registration_id, exc)
        logger.error("REGISTRATION UNDELIVERABLE: none of the %d registration admins "
                     "could be DMed for applicant %s; the request has NOT been "
                     "submitted. Every admin must send the bot a private message "
                     "before they can be reached. See runbook R13.",
                     len(targets), user_id)
        await update.message.reply_text(MESSAGES["registration_unavailable"])

    def _registration_card(self, reg: dict, footer: str = "") -> str:
        """The admin-facing card, built from the STORED document.

        From the document and never from memory, so the settled edit after a
        restart renders identically to the card that was originally sent -- after
        a restart the document is the only thing that still exists.

        parse_mode='HTML', so every Telegram-supplied string goes through
        html.escape: a first name of "<b>" would otherwise break the parse and the
        card would never send at all.
        """
        first = (reg.get('first_name') or "").strip()
        last = (reg.get('last_name') or "").strip()
        name = (first + " " + last).strip() or "not set"

        username = (reg.get('username') or "").strip()
        if username:
            # Add the '@' only if it is not already stored with one.
            username = username if username.startswith("@") else "@" + username
        else:
            username = "not set"

        try:
            attempt = int(reg.get('attempt') or 1)
        except (TypeError, ValueError):
            attempt = 1

        lines = [
            MESSAGES["registration_card_title"],
            "",
            "Name: " + html.escape(name),
            "Username: " + html.escape(username),
            "Telegram id: " + html.escape(str(reg.get('telegram_id'))),
            "Asked: " + format_hhmm(reg.get('created_at')),
        ]
        if attempt > 1:
            lines.append("Previous requests: %d" % (attempt - 1))
        lines += ["", MESSAGES["registration_card_note"]]
        if footer:
            lines += ["", footer]
        return "\n".join(lines)

    def _registration_keyboard(self, registration_id: str) -> InlineKeyboardMarkup:
        """One Approve row per service, plus one reject row.

        Iterates SERVICES rather than enabled_services(), deliberately:
        refresh_all_rosters syncs EVERY service regardless of `enabled`, so
        approving into a track that has not been switched on yet is meaningful and
        is how a roster gets built before launch. sorted() by key so the button
        order -- and therefore every callback_data in the tests -- is deterministic.
        """
        rows = []
        for svc in sorted(SERVICES.values(), key=lambda s: s.key):
            # A non-runnable track (switched off, or no channel) still accepts
            # approvals, but nothing is posted to its queue -- so an approved
            # supporter would be told they can claim and then find nothing to claim.
            # Say so on the button. Admin-facing only; the applicant's copy and the
            # callback_data are both unchanged, so approving is still possible and
            # every existing test's callback string still matches.
            label_key = ("registration_approve_button" if svc.runnable
                         else "registration_approve_button_offline")
            rows.append([InlineKeyboardButton(
                MESSAGES[label_key].format(member=svc.member_label),
                callback_data=f"{CB_REG_APPROVE}:{registration_id}:{svc.key}")])
        rows.append([InlineKeyboardButton(
            MESSAGES["registration_reject_button"],
            callback_data=f"{CB_REG_REJECT}:{registration_id}")])
        return InlineKeyboardMarkup(rows)

    async def _strip_registration_buttons(self, query) -> None:
        """Best-effort removal of the buttons on the card that was just tapped.

        Only the tapper's own card. The other admins' cards are rewritten by
        _settle_registration_cards, which the LOSER of a race deliberately does
        not run -- the winner settles every card, including the loser's.
        """
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as exc:
            logger.warning("Could not strip the buttons on a settled registration "
                           "card: %s", exc)

    async def _handle_registration_callback(self, update: Update,
                                            context: ContextTypes.DEFAULT_TYPE) -> None:
        """Approve / Not now on a registration card.

        THE AWAITS ARE THE DESIGN. Between the parse and the atomic gate there is
        not one await on the path that goes on to decide: every `await
        query.answer(...)` below sits on a branch that returns immediately. A
        yield point before the gate would let two taps both clear the cheap
        status pre-check before either reached Mongo.
        """
        query = update.callback_query
        admin_id = query.from_user.id
        data = query.data or ""

        # 1. AUTHORIZATION. THE FIRST STATEMENT OF THIS FUNCTION, and the sole
        #    entry point to decide_registration anywhere in the codebase.
        #
        #    Deliberately the same string the membership gate uses, so a refusal
        #    tells a rostered supporter and a total stranger exactly the same
        #    thing. With REGISTRATION_ADMINS empty this refuses EVERYONE:
        #    inertness gate #2, reading the same frozenset as gate #1.
        #
        #    Evaluated HERE, at DECISION time, not at fan-out time: an admin
        #    removed from the allowlist between receiving a card and tapping it is
        #    refused, and a card that leaks to anybody else is inert. D40.
        if not is_registration_admin(admin_id):
            await query.answer("You are not authorized to perform this action.",
                               show_alert=True)
            return

        # 2. Parse. Approve carries a service, reject does not.
        approving = data.startswith(CB_REG_APPROVE + ":")
        parts = data.split(":", 2)
        if approving:
            if len(parts) != 3 or not parts[1] or not parts[2]:
                await query.answer(MESSAGES["registration_gone"], show_alert=True)
                return
            registration_id, service_key = parts[1], parts[2]
        else:
            if len(parts) != 2 or not parts[1]:
                await query.answer(MESSAGES["registration_gone"], show_alert=True)
                return
            registration_id, service_key = parts[1], None

        # 3. Approve only: the service must EXIST. Deliberately NOT get_service(),
        #    which falls back to HF for an unknown key -- a mangled or truncated
        #    callback would then silently put somebody on the HF roster.
        svc = None
        if approving:
            if service_key not in SERVICES:
                logger.warning("Registration callback named an unknown service %r",
                               service_key)
                await query.answer(MESSAGES["registration_gone"], show_alert=True)
                return
            svc = SERVICES[service_key]

        # 4. Records offline: nothing is written, nothing is sent, and the buttons
        #    are deliberately LEFT LIVE so the very same tap works once Mongo is
        #    back. D35.
        if not db_mgr.db_available:
            await query.answer(MESSAGES["registration_db_offline"], show_alert=True)
            return

        # 5. Resolved from Mongo on every callback -- there is no in-memory
        #    registration index anywhere, which is why restore.py needs no new
        #    pass and a redeploy leaves live buttons fully functional. D35.
        reg = db_mgr.get_registration(registration_id)
        if reg is None:
            await query.answer(MESSAGES["registration_gone"], show_alert=True)
            await self._strip_registration_buttons(query)
            return

        try:
            applicant = int(reg['telegram_id'])
        except (KeyError, TypeError, ValueError):
            logger.error("Registration %s carries an unusable telegram_id %r",
                         registration_id, reg.get('telegram_id'))
            await query.answer(MESSAGES["registration_gone"], show_alert=True)
            return

        # 6. Self-decision guard. register_command already keeps an admin off their
        #    own fan-out, but a STALE card from an earlier attempt could still be
        #    tapped. Belt and braces on the one path that grants privilege to the
        #    person doing the tapping. M29.
        if applicant == admin_id:
            await query.answer(MESSAGES["registration_self_decision"],
                               show_alert=True)
            return

        # 7. Cheap pre-check. The atomic gate below is the real one; this exists so
        #    the ordinary "somebody already handled it" case reads correctly and
        #    the stale buttons go away.
        if reg.get('status') != 'pending':
            await query.answer(MESSAGES["registration_already_handled"],
                               show_alert=True)
            await self._strip_registration_buttons(query)
            return

        # 8. Cross-roster exclusivity, approve only. Membership is exclusive.
        #    Checked against BOTH memory and Mongo: the in-memory roster is up to
        #    300s stale, so somebody added by the CLI thirty seconds ago is
        #    invisible to the memory check alone. Synchronous, so it stays inside
        #    the pre-gate block. Nothing is decided and the buttons stay live --
        #    the fix is out-of-band, and the admin should be able to retry. D37.
        if approving:
            clash = None
            for other in SERVICES.values():
                if other.key == svc.key:
                    continue
                in_other = applicant in other.roster
                if not in_other:
                    try:
                        doc = db_mgr.get_member_profile_doc(
                            applicant, collection=other.members_collection)
                        in_other = bool(doc) and doc.get('active') is not False
                    except Exception as exc:
                        # Fall back to the memory answer and log; an exception here
                        # must neither refuse nor wave somebody through silently.
                        logger.warning("Could not check the '%s' roster for %s: %s",
                                       other.key, applicant, exc)
                if in_other:
                    clash = other
                    break
            if clash is not None:
                await query.answer(
                    MESSAGES["registration_cross_roster"].format(
                        member=clash.member_label),
                    show_alert=True)
                return

        # 9. THE ATOMIC GATE. Synchronous, and immediately before the first await
        #    of the winning path. None means THIS CALLER LOST: some other tap
        #    already settled the row. Two simultaneous Approves, an Approve racing
        #    a Reject, and a plain double-tap all arrive here, and the loser writes
        #    no roster, sends the applicant NOTHING AT ALL, and only tidies its own
        #    buttons away.
        decided = db_mgr.decide_registration(
            registration_id, admin_id,
            'approved' if approving else 'rejected',
            service_key if approving else None)
        if decided is None:
            await query.answer(MESSAGES["registration_already_handled"],
                               show_alert=True)
            await self._strip_registration_buttons(query)
            return

        # --- WINNER. Roster first, then the ack, then the applicant. ----------
        write_failed = False
        if approving:
            try:
                prior = db_mgr.get_member_profile_doc(
                    applicant, collection=svc.members_collection)
            except Exception as exc:
                prior = None
                logger.warning("Could not read the existing '%s' record for %s: %s",
                               svc.key, applicant, exc)
            if prior and prior.get('active') is False:
                # A re-authorization is not the same event as a first approval and
                # must be visible in the logs.
                logger.warning("Registration %s RE-ACTIVATES %s on the '%s' roster; "
                               "they had previously been deactivated",
                               registration_id, applicant, svc.key)

            # add_authorized_member is the existing idempotent upsert, so approving
            # somebody a CLI add already created simply re-confirms them. It must
            # come FIRST: mark_member_started does NOT upsert, and with no document
            # there would be nothing for it to set.
            if not db_mgr.add_authorized_member(
                    applicant, username=reg.get('username'), active=True,
                    collection=svc.members_collection):
                write_failed = True
                logger.error("Registration %s: could not add %s to the '%s' roster",
                             registration_id, applicant, svc.key)
            elif not db_mgr.mark_member_started(
                    applicant, True, collection=svc.members_collection):
                # /register is BY CONSTRUCTION a private message from the
                # applicant, which is exactly the claim has_started_bot encodes.
                # D39.
                write_failed = True
                logger.error("Registration %s: could not record has_started_bot for "
                             "%s on '%s'", registration_id, applicant, svc.key)

            # In memory too, so they can claim immediately instead of waiting out
            # the 300s roster refresh. The in-memory profile is LOADED FROM THE
            # JUST-WRITTEN MONGO DOCUMENT, not built from nothing: a RE-activated
            # supporter therefore keeps the /name override and /unavailable they
            # had before deactivation, rather than being listed under the Telegram
            # first name they chose to hide, as free (R15). A first-time applicant's
            # doc has no name yet, so they become LISTABLE (once the fork is on for
            # this track) from their next private message -- captured by
            # note_private_contact, the same capture point that records
            # has_started_bot -- or under a name they choose with /name. An admin
            # override via `set-profile --display-name` still works too. The
            # staged-rollout lever is PSS_DIRECTED_ENABLED (R11), not the absence
            # of a name.
            # Fallback when the write or the read-back failed: the bare id, as
            # before. mark_started is then a no-op unless a profile already exists,
            # where it keeps memory agreeing with Mongo until the next refresh; the
            # next private message builds the profile from Mongo instead.
            # Synchronous and before the first await: winner-only, pre-await.
            installed = False
            if not write_failed:
                try:
                    doc_after = db_mgr.get_member_profile_doc(
                        applicant, collection=svc.members_collection)
                except Exception as exc:
                    doc_after = None
                    logger.warning("Could not read back the '%s' record for %s: %s",
                                   svc.key, applicant, exc)
                installed = svc.roster.upsert_record(doc_after)
            if not installed:
                svc.roster.add(applicant)
                svc.roster.mark_started(applicant, True)

        # The first await of the winning path: a plain ack.
        await query.answer()

        notified = True
        if approving:
            # HF's copy is byte-identical: HF never sets supporter_names, so this
            # always picks registration_approved for that track.
            key = 'registration_approved_named' if svc.supporter_names else 'registration_approved'
            text = MESSAGES[key].format(member=svc.member_label)
        else:
            # VERBATIM from MESSAGES. No .format(), no concatenation, no
            # interpolation of any kind: nothing the applicant reads may name or
            # hint at who decided, or why. D44, and case (m) scans for it.
            text = MESSAGES["registration_rejected"]
        try:
            await context.bot.send_message(chat_id=applicant, text=text)
        except Exception as exc:
            notified = False
            logger.warning("Could not tell applicant %s the outcome of registration "
                           "%s: %s", applicant, registration_id, exc)
            if approving and self._looks_unreachable(exc):
                # Self-healing, identical to D16: an approved supporter who has
                # since blocked the bot must never be offered in the picker, and
                # returns automatically the next time they message it. A block is
                # not grounds to undo an approval, so the roster write STANDS.
                try:
                    db_mgr.mark_member_started(applicant, False,
                                               collection=svc.members_collection)
                except Exception as inner:
                    logger.warning("Could not clear has_started_bot for %s: %s",
                                   applicant, inner)
                svc.roster.mark_started(applicant, False)

        await self._settle_registration_cards(
            context, decided, query.from_user, svc if approving else None,
            notified, write_failed)

    async def _settle_registration_cards(self, context, decided: dict, approver,
                                         svc, notified: bool,
                                         write_failed: bool) -> None:
        """Rewrite EVERY admin's card to a settled state and strip its buttons.

        NEVER RAISES. Each edit is wrapped individually, so one admin who has
        since blocked the bot cannot stop the other admin's card being settled.
        An admin whose original DM failed has no admin_messages entry and is
        simply skipped -- there is no message to edit.

        Naming the approver here is deliberate and safe: the audience is the
        allowlist itself, and on a team that small, accountability for who granted
        read access to crisis conversations is worth more than mutual anonymity
        among the approvers. It never reaches the applicant --
        registration_approved and registration_rejected carry no approver slot at
        all, and nothing on this path sends to the applicant. D44.
        """
        name = (getattr(approver, "first_name", None)
                or getattr(approver, "username", None)
                or "admin %s" % getattr(approver, "id", "?"))
        approver_text = html.escape(str(name))

        if svc is not None:
            footer = MESSAGES["registration_settled_approved"].format(
                member=html.escape(svc.member_label), approver=approver_text)
        else:
            footer = MESSAGES["registration_settled_rejected"].format(
                approver=approver_text)
        if not notified:
            footer += "\n" + MESSAGES["registration_settled_unreachable"]
        if write_failed:
            footer += "\n" + MESSAGES["registration_settled_write_failed"]

        text = self._registration_card(decided, footer)
        for entry in (decided.get('admin_messages') or []):
            try:
                await context.bot.edit_message_text(
                    chat_id=entry['admin_id'], message_id=entry['message_id'],
                    text=text, reply_markup=None, parse_mode='HTML')
            except Exception as exc:
                logger.warning("Could not settle the registration card in admin "
                               "%s's chat: %s", entry.get('admin_id'), exc)

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
            name = (supporter_label(svc.key, entry.get('target_member_id'))
                    or svc.member_label)
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

        # THE FORK: "a specific peer supporter" or "any available peer supporter".
        # Shown iff directed mode is on for the track AND at least one listable
        # supporter (active, with a known name) exists other than the requester.
        # It is shown EVEN WHEN ALL OF THEM ARE BUSY: the list then marks every one
        # busy and puts send-to-anyone first, which is honest and still one tap from
        # the channel. HF (directed_enabled False) never reaches it, and neither do
        # the pre-Phase-5 suites, whose rosters are bare ints with no names -- they
        # keep driving the original path below untouched.
        svc = get_service(service_key)
        if directed_fork_offered(svc.key, user_id):
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

        # Registration approval. BEFORE the membership gate, with its own and
        # strictly narrower gate inside _handle_registration_callback.
        #
        # Placing these after the gate would be wrong in BOTH directions. Wrong one
        # way: a registration admin need not be on any roster at all -- the owner
        # and the welfare director may be neither an HF nor a PSS member -- so the
        # gate would refuse the only people entitled to decide. Wrong the other
        # way: passing the gate proves only "is on SOME roster", which is not the
        # authorization we want here. A rostered HF companion must not be able to
        # approve a PSS supporter, and after this change no roster membership
        # grants any approval power at all: claiming a conversation and granting
        # somebody else the ability to read one are different privileges. So this
        # goes before the gate carrying its own, exactly as svc_ and pk_* do for
        # requesters. D40.
        if (data.startswith(CB_REG_APPROVE + ":")
                or data.startswith(CB_REG_REJECT + ":")):
            await self._handle_registration_callback(update, context)
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
        """Render one page of the supporter list. False when nobody can be shown.

        EVERY listable supporter is shown, numbered; anyone who cannot be asked
        right now carries the one busy marker and no button, whatever the reason.
        Numbers are 1-based and PAGE-LOCAL and cover busy entries too, so a typed
        number means what is on screen; send_directed_request's live re-check is
        what refuses a busy one. The view is recorded BEFORE the message is sent so
        a very fast reply cannot race the record and be read against the previous
        page.

        `note` is already HTML-safe (its {name} was escaped by the caller) and is
        deliberately NOT escaped again here.
        """
        entry = self.queue_manager.get_queue_entry(queue_id)
        if entry is None:
            return False

        svc = get_service(entry.get('service'))
        # KILL SWITCH: with directed mode off, a picker button rendered before the
        # switch was flipped must not show a single name.
        entries = (picker_entries(svc.key, requester_id=user_id,
                                  declined=entry.get('declined_by') or [])
                   if svc.directed_enabled else [])

        if svc.directed_enabled:
            omitted = omitted_supporters(svc.key)
            if omitted:
                # Ids only -- these are exactly the people with no usable name. A
                # supporter who expected to be listed must be findable in the logs.
                logger.info("%d %s roster member(s) left off the list for want of a "
                            "usable name: %s", len(omitted), svc.key, omitted)

        prefix = (note + "\n\n") if note else ""

        if not entries:
            # NEVER render an empty list: a picker with no names is a dead end with no
            # way out but /cancel.
            picker_views.pop(user_id, None)
            await self._send_or_edit(
                query, context, user_id, prefix + MESSAGES["picker_nobody_free"],
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(MESSAGES["picker_anyone_button"],
                                          callback_data=CB_PICK_OPEN)],
                    [InlineKeyboardButton(MESSAGES["picker_cancel_button"],
                                          callback_data=CB_PICK_CANCEL)],
                ]), parse_mode='HTML')
            return False

        anyone_free = any(free for _profile, _label, free in entries)

        page_size = max(1, svc.picker_page_size)
        pages = max(1, (len(entries) + page_size - 1) // page_size)
        try:
            page = int(page)
        except (TypeError, ValueError):
            page = 0
        page = max(0, min(page, pages - 1))
        chunk = entries[page * page_size:(page + 1) * page_size]

        # Name only. The blurb stays in the data and the CLI but is never shown.
        lines = []
        for number, (_profile, label, free) in enumerate(chunk, start=1):
            line = f"{number}. {html.escape(label)}"
            if not free:
                line += " " + html.escape(MESSAGES["picker_busy_marker"])
            lines.append(line)

        text = prefix
        if not anyone_free:
            text += MESSAGES["picker_nobody_free"] + "\n\n"
        text += MESSAGES["picker_header"] + "\n\n" + "\n".join(lines)
        if anyone_free:
            text += "\n\n" + MESSAGES["picker_hint"]
        if pages > 1:
            text += "\n" + MESSAGES["picker_page"].format(page=page + 1, pages=pages)

        anyone_row = [InlineKeyboardButton(MESSAGES["picker_anyone_button"],
                                           callback_data=CB_PICK_OPEN)]
        rows = []
        if not anyone_free:
            # Nobody can be picked, so the way forward goes first, where it is seen.
            rows.append(anyone_row)
        # Buttons for the FREE only. Button text is plain -- no parse mode applies.
        rows.extend(
            [InlineKeyboardButton(f"{number}. {label}"[:60],
                                  callback_data=f"{CB_PICK_SELECT}:{profile.telegram_id}")]
            for number, (profile, label, free) in enumerate(chunk, start=1) if free)
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(MESSAGES["picker_back_button"],
                                            callback_data=f"{CB_PICK_LIST}:{page - 1}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton(MESSAGES["picker_next_button"],
                                            callback_data=f"{CB_PICK_LIST}:{page + 1}"))
        if nav:
            rows.append(nav)
        if anyone_free:
            rows.append(anyone_row)
        rows.append([InlineKeyboardButton(MESSAGES["picker_cancel_button"],
                                          callback_data=CB_PICK_CANCEL)])

        # RECORD BEFORE SENDING. Busy ids included, so a typed number lines up with
        # the numbered list on screen.
        picker_views[user_id] = {
            'queue_id': queue_id,
            'page': page,
            'ids': [profile.telegram_id for profile, _label, _free in chunk],
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
            outcome = await self.queue_manager.route_to_open_queue(queue_id)
            if outcome == 'ok':
                # Every choice on this message is now spent. route_to_open_queue
                # already sent queue_added, so only the keyboard needs to go.
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass
                return
            if outcome == 'post_failed':
                # The one real fault. The buttons are deliberately LEFT live: routing
                # reverted to 'choosing', so the same tap is a valid retry.
                try:
                    await context.bot.send_message(chat_id=user_id,
                                                   text=MESSAGES["channel_error"])
                except Exception as exc:
                    logger.warning("Could not report the channel error to %s: %s",
                                   user_id, exc)
                return
            # 'already_open' / 'gone': a repeat or stale tap. Tell them where their
            # request actually stands -- "the system is down" would send somebody
            # whose request is live in the channel off to retype it or give up.
            text = (self._open_request_status_text(user_id)
                    if self.queue_manager.get_queue_entry(queue_id) is not None
                    else MESSAGES["directed_gone"])
            await self._send_or_edit(query, context, user_id, text)
            return

        if data.startswith(CB_PICK_LIST + ":"):
            await query.answer()
            if (entry.get('routing') or 'open') == 'open':
                # A stale 'A specific peer supporter' / 'Choose someone else' button on
                # a request that is already in the channel. Rendering a list here would
                # invite a choice that can no longer happen.
                picker_views.pop(user_id, None)
                await self._send_or_edit(query, context, user_id,
                                         self._open_request_status_text(user_id))
                return
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
            name = supporter_label(entry.get('service'), member_id) if entry else ""
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

        if outcome in ('busy', 'unreachable'):
            # ONE wording for every reason, so a decline, a /unavailable, a
            # conversation and an unreachable chat are indistinguishable. Re-render
            # the page they were looking at, not page one.
            # With directed mode switched off (the kill switch) not even the note
            # may carry a name: the render below shows none either.
            entry = self.queue_manager.get_queue_entry(queue_id)
            svc = get_service(entry.get('service')) if entry is not None else None
            name = (supporter_label(svc.key, member_id)
                    if svc is not None and svc.directed_enabled else "") or "That person"
            view = picker_views.get(user_id)
            page = (view.get('page', 0)
                    if view is not None and view.get('queue_id') == queue_id else 0)
            await self._render_picker(
                query, context, user_id, queue_id, page,
                MESSAGES["picker_busy"].format(name=html.escape(name)))
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
        # Unescaped on purpose: _offer_next_step sends plain text.
        name = supporter_label(entry.get('service'), user_id) if entry is not None else ""

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
