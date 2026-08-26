"""Rebuild in-memory queue and session state from Mongo after a restart.

Synchronous on purpose. This module touches only blocking PyMongo (as the rest of
the codebase does) and in-memory dicts. Every user-visible side effect is delegated
to the expiry sweeps that main() runs immediately afterwards, so there is exactly
one code path that sends "your request expired" and one that sends "conversation
closed" -- and this module is trivially testable with a stub db_mgr and no event
loop.

Rehydration must NEVER prevent the bot from booting. Every failure path here logs
and returns; nothing raises out of restore_state().
"""

import logging
from typing import Any, Dict

import config
from config import (
    ServiceType,
    UserState,
    active_sessions,
    directed_by_member,
    get_service,
    queue_entries,
    queue_order,
    session_warnings,
    used_anonymous_ids,
    user_states,
    user_to_queue_map,
    user_to_session_map,
)
from src.timeutil import ensure_aware_utc, utcnow
from src.database.manager import db_mgr

logger = logging.getLogger(__name__)


def _empty_stats(**overrides) -> Dict[str, Any]:
    stats = {
        'db_available': True,
        'enabled': True,
        'dry_run': False,
        'aborted': False,
        'pending_restored': 0,
        'pending_skipped': 0,
        'pending_stale_closed': 0,
        # Broken out of pending_restored (they are a SUBSET of it, not extra) so the
        # R4 dry-run readout says how many people are mid-pick and how many are
        # sitting with one named supporter.
        'pending_choosing': 0,
        'pending_directed': 0,
        'active_restored': 0,
        'active_skipped': 0,
    }
    stats.update(overrides)
    return stats


def restore_state(queue_manager, session_manager) -> Dict[str, Any]:
    """Rebuild in-memory queue/session state from Mongo. Synchronous, no Telegram I/O."""
    # config.RESTORE_* are read as module attributes, not imported by value, so tests
    # (and an operator editing the environment between boots) can change them.
    dry_run = bool(config.RESTORE_DRY_RUN)

    if not config.RESTORE_ENABLED:
        logger.warning("Restart durability is DISABLED by RESTORE_ENABLED=false")
        return _empty_stats(enabled=False, dry_run=dry_run)

    if not db_mgr.db_available:
        logger.warning(
            "🟡 Mongo unavailable — restart durability is OFF. Anyone who was queued or "
            "mid-conversation before this restart has been silently dropped and will not "
            "be notified. Their /status and /cancel will report IDLE."
        )
        return _empty_stats(db_available=False, dry_run=dry_run)

    stats = _empty_stats(dry_run=dry_run)

    try:
        pending = db_mgr.get_pending_sessions()   # already sorted created_at ASC
        active = db_mgr.get_active_sessions()

        if (len(pending) > config.RESTORE_MAX_PENDING
                or len(active) > config.RESTORE_MAX_ACTIVE):
            logger.error(
                "Refusing to rehydrate: %d pending / %d active exceeds caps (%d/%d). "
                "Investigate the sessions collection before re-enabling.",
                len(pending), len(active),
                config.RESTORE_MAX_PENDING, config.RESTORE_MAX_ACTIVE,
            )
            stats['aborted'] = True
            return stats

        _restore_active(active, session_manager, stats, dry_run)
        _restore_pending(pending, queue_manager, stats, dry_run)

    except Exception:
        logger.exception("Rehydration failed; continuing with empty in-memory state")
        stats['aborted'] = True
        return stats

    logger.info(
        "🔄 Rehydration%s: %d pending restored (%d choosing, %d directed), "
        "%d pending stale-closed, %d pending skipped, "
        "%d active restored, %d active skipped",
        " [DRY RUN]" if dry_run else "",
        stats['pending_restored'], stats['pending_choosing'], stats['pending_directed'],
        stats['pending_stale_closed'], stats['pending_skipped'],
        stats['active_restored'], stats['active_skipped'],
    )
    return stats


def _close(session_id, user_id, end_reason, dry_run) -> None:
    """Close a Mongo row, unless this is a dry run."""
    if dry_run:
        return
    try:
        db_mgr.end_session(session_id, user_id, system_end=True, end_reason=end_reason)
    except Exception as exc:
        logger.warning("Could not close session %s (%s): %s", session_id, end_reason, exc)


# --------------------------------------------------------------------------- pass 1
def _restore_active(docs, session_manager, stats, dry_run) -> None:
    """ACTIVE sessions restore FIRST.

    An in-flight conversation outranks a stale pending row, and going first lets
    pass 2 skip anyone already accounted for. It does not disturb queue_order,
    which is built only in pass 2.
    """
    for doc in docs:
        sid = doc.get('session_id')
        user_id = doc.get('user_id')
        member_id = doc.get('heartfelt_member_id')
        service = doc.get('service') or ServiceType.HF.value

        if not sid or not user_id:
            logger.warning("Malformed active session doc (session_id=%r, user_id=%r); skipping",
                           sid, user_id)
            stats['active_skipped'] += 1
            continue

        if member_id is None:
            logger.error(
                "Session %s is active with no claimer; not restoring — the expiry sweep "
                "will close it", sid)
            stats['active_skipped'] += 1
            continue

        if user_id == member_id:
            logger.error("Session %s has the same user as requester and claimer; corrupt, skipping",
                         sid)
            stats['active_skipped'] += 1
            continue

        if user_id in user_to_session_map or member_id in user_to_session_map:
            logger.warning(
                "Session %s duplicates a party already restored; oldest wins, skipping", sid)
            stats['active_skipped'] += 1
            continue

        if dry_run:
            logger.info("[DRY RUN] would restore active %s (user %s, member %s, service %s)",
                        sid, user_id, member_id, service)
            stats['active_restored'] += 1
            continue

        anon = doc.get('anonymous_user_id') or session_manager._generate_anonymous_id(
            get_service(service).anon_prefix)

        active_sessions[sid] = {
            'user_id': user_id,
            'heartfelt_member_id': member_id,
            'created_at': ensure_aware_utc(doc.get('claimed_at') or doc.get('created_at')) or utcnow(),
            'last_activity_at': ensure_aware_utc(
                doc.get('last_activity_at')
                or doc.get('claimed_at')
                or doc.get('created_at')
            ) or utcnow(),
            'anonymous_user_id': anon,
            'service': service,
            'restored': True,
        }
        user_to_session_map[user_id] = sid
        user_to_session_map[member_id] = sid
        user_states[user_id] = UserState.IN_CONVERSATION
        user_states[member_id] = UserState.IN_CONVERSATION
        used_anonymous_ids.add(anon)
        # session_warnings is memory-only, so a conversation already warned before the
        # restart may be warned a second time. Benign, and strictly better than killing
        # it with no warning at all.
        session_warnings.pop(sid, None)

        stats['active_restored'] += 1
        logger.info("Restored active session %s (user %s, member %s, service %s)",
                    sid, user_id, member_id, service)


# --------------------------------------------------------------------------- pass 2
def _restore_pending(docs, queue_manager, stats, dry_run) -> None:
    """PENDING requests. get_pending_sessions sorts created_at ascending, so appending
    to queue_order preserves FIFO and get_queue_position stays correct across a restart.
    """
    now = utcnow()

    for doc in docs:
        sid = doc.get('session_id')
        user_id = doc.get('user_id')
        svc = get_service(doc.get('service') or ServiceType.HF.value)

        if not sid or not user_id:
            logger.warning("Malformed pending session doc (session_id=%r, user_id=%r); skipping",
                           sid, user_id)
            stats['pending_skipped'] += 1
            continue

        if sid in queue_entries:
            logger.warning("Pending %s is already in queue_entries at boot; skipping", sid)
            stats['pending_skipped'] += 1
            continue

        if user_id in user_to_session_map:
            # Pass 1 already gave this person an active conversation. The pending row is
            # stale; close it so it cannot come back on the next boot.
            logger.info("Pending %s superseded by an active session for user %s; closing it",
                        sid, user_id)
            _close(sid, None, 'superseded_by_active', dry_run)
            stats['pending_skipped'] += 1
            continue

        if user_id in user_to_queue_map:
            logger.warning("User %s has more than one pending request; oldest wins, closing %s",
                           user_id, sid)
            _close(sid, None, 'duplicate_pending', dry_run)
            stats['pending_skipped'] += 1
            continue

        # TWO variables, two jobs. `created` is immutable: it feeds entry['created_at']
        # and the channel post's "Requested:" line. `waiting` is the requester's wait
        # budget, restarted every time the decision is handed back to them. Measuring
        # the stale horizon against created_at instead would silently close, at boot,
        # an old request that was re-picked five minutes ago -- see mutation M18.
        created = ensure_aware_utc(doc.get('created_at'))
        waiting = ensure_aware_utc(doc.get('waiting_since')) or created
        age_minutes = (now - waiting).total_seconds() / 60.0 if waiting else 0.0

        # A DIRECTED row is on a DIFFERENT CLOCK and must be judged against it.
        # waiting_since is the requester's queue budget; a directed request's budget is
        # directed_response_minutes measured from directed_at, and the two diverge by
        # however long the requester spent at the picker before choosing. Judging a
        # directed row by the queue horizon silently closes, at boot, a request whose
        # supporter still has time left to accept -- the requester is told nothing and
        # the Accept button in the supporter's DM goes dead. With the base Service
        # defaults (queue_expire_minutes=60 vs directed_response_minutes=1440) that
        # would be EVERY directed request more than three hours old, on every restart.
        #
        # Past this horizon nothing is sent either way: sweep_directed_requests owns
        # the directed lane end to end and has its own, identical, silent horizon.
        stale_routing = doc.get('routing') or 'open'
        if stale_routing == 'directed':
            budget = svc.directed_response_minutes
            directed_ref = ensure_aware_utc(doc.get('directed_at'))
            if directed_ref is not None and directed_ref <= now:
                age_minutes = (now - directed_ref).total_seconds() / 60.0
            # An unusable directed_at falls back to waiting_since against the DIRECTED
            # budget: still generous enough not to destroy a live request, still
            # bounded so a corrupt clock cannot resurrect an ancient row forever.
        else:
            budget = svc.queue_expire_minutes

        horizon = budget + config.STALE_NOTIFY_GRACE_MINUTES
        if age_minutes > horizon:
            # Do NOT restore and do NOT notify. Because cleanup_expired_queues and
            # remove_from_queue never closed their Mongo rows before this branch existed,
            # the collection holds a long tail of pending documents going back months.
            # Without this horizon the first boot of this feature would DM every past
            # requester of a mental-health helpline "your place in the queue expired".
            logger.info("Pending %s is %.1f min old (> %d min horizon); closing silently",
                        sid, age_minutes, horizon)
            _close(sid, None, 'stale_startup_sweep', dry_run)
            stats['pending_stale_closed'] += 1
            continue

        if dry_run:
            logger.info("[DRY RUN] would restore pending %s (user %s, service %s, age %.1f min)",
                        sid, user_id, svc.key, age_minutes)
            stats['pending_restored'] += 1
            continue

        anon = doc.get('anonymous_user_id') or queue_manager._generate_anonymous_id(svc.anon_prefix)
        routing = doc.get('routing') or 'open'
        if routing not in ('open', 'choosing', 'directed'):
            logger.warning("Pending %s has unknown routing %r; treating it as 'open'",
                           sid, routing)
            routing = 'open'

        entry = {
            'user_id': user_id,
            'description': doc.get('description') or '',
            'created_at': created or now,
            'waiting_since': waiting or created or now,
            'anonymous_id': anon,
            'message_id': doc.get('queue_message_id'),
            'channel_id': doc.get('queue_channel_id') or svc.channel_id,
            'service': svc.key,
            'routing': routing,
            'target_member_id': None,
            'directed_at': None,
            'directed_message_id': doc.get('directed_message_id'),
            'notice_channel_id': doc.get('notice_channel_id'),
            'notice_message_id': doc.get('notice_message_id'),
            'declined_by': list(doc.get('declined_by') or []),
            'restored': True,
        }

        if routing == 'directed':
            target = doc.get('target_member_id')
            try:
                target = int(target)
            except (TypeError, ValueError):
                logger.warning(
                    "Pending %s is routed 'directed' but its target_member_id is %r; "
                    "downgrading to 'choosing' so the requester is asked again rather "
                    "than waiting on nobody", sid, doc.get('target_member_id'))
                target = None
                routing = 'choosing'
                entry['routing'] = 'choosing'

            if target is not None and target in directed_by_member:
                # Should be unreachable: available_supporters() excludes anyone already
                # in this index, so two live requests cannot name the same supporter.
                # Keep the OLDER one (docs arrive created_at ASC) and say so loudly.
                logger.warning(
                    "Pending %s targets member %s who already holds request %s; "
                    "leaving the index pointing at the older one", sid, target,
                    directed_by_member[target])
                # No rebinding here: the setdefault below is what keeps the older one.

            if entry['routing'] == 'directed':
                directed_at = ensure_aware_utc(doc.get('directed_at'))
                if directed_at is None or directed_at > now:
                    # FAIL TOWARD WAITING, never toward a spurious lapse-DM at boot: an
                    # unusable clock must not read as "this has been silent for a day".
                    logger.warning(
                        "Pending %s has an unusable directed_at (%r); treating it as "
                        "just sent", sid, doc.get('directed_at'))
                    directed_at = utcnow()
                entry['target_member_id'] = target
                entry['directed_at'] = directed_at

        queue_entries[sid] = entry
        user_to_queue_map[user_id] = sid
        used_anonymous_ids.add(anon)
        routing = entry['routing']

        if routing == 'open':
            # Today's path, verbatim. get_pending_sessions sorts created_at ASC, so
            # appending here preserves FIFO across a restart.
            queue_order.append(sid)
            user_states[user_id] = UserState.IN_QUEUE
        elif routing == 'choosing':
            # NOT in queue_order: a request nobody outside this chat can see has no
            # queue position. And do NOT re-send the picker -- the buttons already in
            # the requester's chat are still live and resolve through user_to_queue_map.
            # Re-sending would DM every mid-pick requester on every single restart.
            user_states[user_id] = UserState.CHOOSING_SUPPORTER
            stats['pending_choosing'] += 1
        else:  # 'directed'
            # IN_QUEUE, not CHOOSING_SUPPORTER: they ARE waiting, on one named person.
            user_states[user_id] = UserState.IN_QUEUE
            # Deliberately NOT touching user_states[target]. A supporter holding a
            # directed request is not IN_QUEUE -- that state means "I asked for help",
            # and setting it would make /chat tell them they are already queued.
            # Their availability comes from directed_by_member, nothing else.
            if entry['target_member_id'] is not None:
                directed_by_member.setdefault(entry['target_member_id'], sid)
            stats['pending_directed'] += 1

        stats['pending_restored'] += 1
        # Anything already past its window is restored anyway: the sweep_expired_queues()
        # pass main() runs before polling starts expires it and sends one correctly-timed
        # notification, through the same code path as a live expiry.
        logger.info("Restored pending %s (user %s, service %s, routing %s, age %.1f min)",
                    sid, user_id, svc.key, routing, age_minutes)
