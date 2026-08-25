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
        "🔄 Rehydration%s: %d pending restored, %d pending stale-closed, %d pending skipped, "
        "%d active restored, %d active skipped",
        " [DRY RUN]" if dry_run else "",
        stats['pending_restored'], stats['pending_stale_closed'], stats['pending_skipped'],
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

        created = ensure_aware_utc(doc.get('created_at'))
        if created is None:
            age_minutes = 0.0
        else:
            age_minutes = (now - created).total_seconds() / 60.0

        horizon = svc.queue_expire_minutes + config.STALE_NOTIFY_GRACE_MINUTES
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

        queue_entries[sid] = {
            'user_id': user_id,
            'description': doc.get('description') or '',
            'created_at': created or now,
            'anonymous_id': anon,
            'message_id': doc.get('queue_message_id'),
            'channel_id': doc.get('queue_channel_id') or svc.channel_id,
            'service': svc.key,
            'restored': True,
        }
        user_to_queue_map[user_id] = sid
        queue_order.append(sid)
        user_states[user_id] = UserState.IN_QUEUE
        used_anonymous_ids.add(anon)

        stats['pending_restored'] += 1
        # Anything already past its window is restored anyway: the sweep_expired_queues()
        # pass main() runs before polling starts expires it and sends one correctly-timed
        # notification, through the same code path as a live expiry.
        logger.info("Restored pending %s (user %s, service %s, age %.1f min)",
                    sid, user_id, svc.key, age_minutes)
