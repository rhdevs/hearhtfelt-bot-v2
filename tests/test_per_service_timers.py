#!/usr/bin/env python3
"""
Per-track timer behaviour (Phase 2).

Proves an HF request and a PSS request of the same age get different fates,
that warnings fire at (timeout - lead) rather than at the lead itself, and that
the warning copy renders each track's own lead time.

Memory-only: db_mgr is stubbed with db_available = False everywhere, so nothing
here touches Mongo. Run directly: `python tests/test_per_service_timers.py`
"""

import asyncio
import datetime
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from config import ServiceType, UserState
from src.timeutil import utcnow

import src.bot.managers.expiry as expiry_mod
import src.bot.managers.queue as queue_mod
import src.bot.managers.session as session_mod
from src.bot.managers.expiry import SessionExpiryManager
from src.bot.managers.queue import QueueManager
from src.bot.managers.session import SessionManager

HF_MEMBER = 1001
PSS_MEMBER = 2001
HF_CHANNEL = "-100HF"
PSS_CHANNEL = "-100PSS"


class OfflineDB:
    """Stands in for db_mgr with the database unavailable."""
    db_available = False

    def end_session(self, *a, **kw):
        raise AssertionError("end_session must not be called when db_available is False")

    def get_sessions_by_activity(self, cutoff):
        raise AssertionError("get_sessions_by_activity must not be called when db_available is False")


class FakeBot:
    def __init__(self):
        self.sent = []     # (chat_id, text)
        self.edited = []   # (chat_id, message_id, text)
        self.deleted = []
        self._mid = 0

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self._mid += 1
        self.sent.append((str(chat_id), text))
        return SimpleNamespace(message_id=self._mid)

    async def edit_message_text(self, chat_id=None, message_id=None, text=None, **kw):
        self.edited.append((str(chat_id), message_id, text))
        return None

    async def delete_message(self, chat_id=None, message_id=None, **kw):
        self.deleted.append((str(chat_id), message_id))
        return None

    def texts_to(self, user_id):
        return [t for c, t in self.sent if c == str(user_id)]


def reset_state():
    for d in (config.user_states, config.user_to_service_map, config.queue_entries,
              config.user_to_queue_map, config.active_sessions, config.user_to_session_map,
              config.session_warnings):
        d.clear()
    config.queue_order.clear()
    config.used_anonymous_ids.clear()
    config.safety_logs.clear()
    # Phase 5 indices. A suite that forgets these leaks a directed request or a
    # rendered picker view into whatever runs next, and available_supporters()
    # silently starts hiding people.
    config.directed_by_member.clear()
    config.picker_views.clear()


def enable_both_services():
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]
    hf.channel_id = HF_CHANNEL
    hf.enabled = True
    hf.roster.replace([HF_MEMBER])
    pss.channel_id = PSS_CHANNEL
    pss.enabled = True
    pss.roster.replace([PSS_MEMBER])
    assert [s.key for s in config.enabled_services()] == ["hf", "pss"]


def seed_queue_entry(qm, queue_id, user_id, service, age_minutes):
    config.queue_entries[queue_id] = {
        'user_id': user_id,
        'description': 'needs a listening ear',
        'created_at': utcnow() - datetime.timedelta(minutes=age_minutes),
        'anonymous_id': f'RHesident #{user_id}',
        'message_id': 500 + (user_id % 100),
        'channel_id': config.get_service(service).channel_id,
        'service': service,
    }
    config.user_to_queue_map[user_id] = queue_id
    config.queue_order.append(queue_id)
    config.user_states[user_id] = UserState.IN_QUEUE


def seed_session(session_id, user_id, member_id, service, idle_minutes):
    last = utcnow() - datetime.timedelta(minutes=idle_minutes)
    config.active_sessions[session_id] = {
        'user_id': user_id,
        'heartfelt_member_id': member_id,
        'created_at': last,
        'last_activity_at': last,
        'anonymous_user_id': f'RHesident #{user_id}',
        'service': service,
    }
    config.user_to_session_map[user_id] = session_id
    config.user_to_session_map[member_id] = session_id
    config.user_states[user_id] = UserState.IN_CONVERSATION
    config.user_states[member_id] = UserState.IN_CONVERSATION


async def test_queue_expiry_is_per_service():
    reset_state()
    bot = FakeBot()
    qm = QueueManager(bot)

    # HF window is 60 min, PSS window is 1440 (24h).
    seed_queue_entry(qm, "hf-old", 7001, "hf", 61)
    seed_queue_entry(qm, "hf-young", 7002, "hf", 59)
    seed_queue_entry(qm, "pss-61", 7003, "pss", 61)
    seed_queue_entry(qm, "pss-old", 7004, "pss", 1441)

    expired = await qm.sweep_expired_queues()
    expired_ids = sorted(e['queue_id'] for e in expired)
    assert expired_ids == ["hf-old", "pss-old"], expired_ids

    assert "hf-old" not in config.queue_entries
    assert "pss-old" not in config.queue_entries
    assert "hf-young" in config.queue_entries, "a 59-minute HF request is still live"
    assert "pss-61" in config.queue_entries, "a 61-minute PSS request is nowhere near its 24h window"

    assert bot.texts_to(7001), "expired HF requester must be told"
    assert "expired" in bot.texts_to(7001)[0].lower()
    assert bot.texts_to(7004), "expired PSS requester must be told"
    assert not bot.texts_to(7002)
    assert not bot.texts_to(7003)

    # The requester-facing copy names the right helper for each track.
    assert "Hearhtfelt Member" in bot.texts_to(7001)[0]
    assert "Peer Supporter" in bot.texts_to(7004)[0]

    # Expired entries have their user state and index cleaned up.
    assert config.user_states[7001] == UserState.IDLE
    assert 7001 not in config.user_to_queue_map
    assert config.user_states[7002] == UserState.IN_QUEUE

    print("OK  1. queue expiry uses each entry's own service window (HF 60m, PSS 24h)")


async def test_expired_post_is_retired():
    reset_state()
    bot = FakeBot()
    qm = QueueManager(bot)
    seed_queue_entry(qm, "hf-old", 7101, "hf", 61)

    await qm.sweep_expired_queues()

    assert bot.edited, "the channel post should have been edited, not left with a live Claim button"
    chat_id, message_id, text = bot.edited[-1]
    assert chat_id == HF_CHANNEL
    assert "EXPIRED" in text
    assert not bot.deleted, "delete is only the fallback when the edit fails"
    print("OK  2. an expired request's channel post is retired to an EXPIRED card")


async def test_retire_falls_back_to_delete():
    reset_state()
    bot = FakeBot()

    async def boom(**kw):
        raise RuntimeError("message can't be edited")
    bot.edit_message_text = boom

    qm = QueueManager(bot)
    seed_queue_entry(qm, "hf-old", 7201, "hf", 61)
    await qm.sweep_expired_queues()

    assert bot.deleted, "a failed edit must fall back to deleting the post"
    assert bot.texts_to(7201), "and the requester must still be notified"
    print("OK  3. a failed edit falls back to delete and never blocks the notification")


async def test_session_expiry_is_per_service():
    reset_state()
    bot = FakeBot()
    sm = SessionManager(bot)
    em = SessionExpiryManager(bot, sm)

    # HF: timeout 30, lead 5 -> warns at 25, dies at 30.
    seed_session("hf-expire", 8001, HF_MEMBER, "hf", 31)
    seed_session("hf-warn", 8002, HF_MEMBER + 1, "hf", 26)
    seed_session("hf-quiet", 8003, HF_MEMBER + 2, "hf", 20)
    # PSS: timeout 1440, lead 60 -> warns at 1380 (23h), dies at 1440 (24h).
    seed_session("pss-expire", 9001, PSS_MEMBER, "pss", 25 * 60)
    seed_session("pss-warn", 9002, PSS_MEMBER + 1, "pss", 23 * 60 + 10)
    seed_session("pss-quiet", 9003, PSS_MEMBER + 2, "pss", 22 * 60)

    await em.run_once()

    assert "hf-expire" not in config.active_sessions
    assert "pss-expire" not in config.active_sessions
    assert "hf-warn" in config.active_sessions, "26 min idle is a warning, not an expiry"
    assert "pss-warn" in config.active_sessions
    assert "hf-quiet" in config.active_sessions, "20 min idle is below HF's 25-minute warning band"
    assert "pss-quiet" in config.active_sessions, "22h idle is below PSS's 23h warning band"

    assert config.session_warnings.get("hf-warn") is True
    assert config.session_warnings.get("pss-warn") is True
    assert not config.session_warnings.get("hf-quiet")
    assert not config.session_warnings.get("pss-quiet")

    # Warning copy renders each track's own lead time.
    hf_warning = bot.texts_to(8002)
    assert hf_warning and "5 minutes" in hf_warning[0], hf_warning
    pss_warning = bot.texts_to(9002)
    assert pss_warning and "1 hour" in pss_warning[0], pss_warning
    for texts in (hf_warning, pss_warning):
        assert "{duration}" not in texts[0], "the warning template was sent raw"

    # A 22h-idle PSS conversation would have been killed by the old global 30-min rule.
    assert config.user_states[9003] == UserState.IN_CONVERSATION

    print("OK  4. session expiry and warnings use each session's own service window")


async def test_hf_and_pss_warning_bands_are_distinct():
    """A PSS session idle for 26 minutes must be untouched -- the exact age that
    warns an HF session."""
    reset_state()
    bot = FakeBot()
    sm = SessionManager(bot)
    em = SessionExpiryManager(bot, sm)

    seed_session("pss-26min", 9101, PSS_MEMBER, "pss", 26)
    await em.run_once()

    assert "pss-26min" in config.active_sessions
    assert not config.session_warnings.get("pss-26min")
    assert not bot.sent, f"nothing should have been sent: {bot.sent}"
    print("OK  5. 26 minutes idle warns HF but is silent for PSS")


def test_sweep_fits_inside_shortest_warning_band():
    shortest = min(s.session_warning_minutes for s in config.SERVICES.values()) * 60
    assert config.SESSION_SWEEP_SECONDS < shortest, (config.SESSION_SWEEP_SECONDS, shortest)
    print("OK  6. SESSION_SWEEP_SECONDS fits inside the shortest warning band")


async def run():
    enable_both_services()
    await test_queue_expiry_is_per_service()
    await test_expired_post_is_retired()
    await test_retire_falls_back_to_delete()
    await test_session_expiry_is_per_service()
    await test_hf_and_pss_warning_bands_are_distinct()
    test_sweep_fits_inside_shortest_warning_band()


if __name__ == "__main__":
    stub = OfflineDB()
    real = (queue_mod.db_mgr, session_mod.db_mgr, expiry_mod.db_mgr)
    queue_mod.db_mgr = session_mod.db_mgr = expiry_mod.db_mgr = stub
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]
    hf_channel, hf_enabled = hf.channel_id, hf.enabled
    try:
        asyncio.run(run())
        print("\nAll per-service timer assertions passed!")
    finally:
        queue_mod.db_mgr, session_mod.db_mgr, expiry_mod.db_mgr = real
        hf.channel_id, hf.enabled = hf_channel, hf_enabled
        hf.roster.replace(config.DEFAULT_HEARTFELT_MEMBERS)
        pss.channel_id = config.PSS_CHANNEL_ID
        pss.enabled = False
        pss.roster.replace([])
        reset_state()
