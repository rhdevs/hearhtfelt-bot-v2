#!/usr/bin/env python3
"""
End-to-end flow test for the two-track (HF + PSS) umbrella.

Drives the REAL BotHandlers / QueueManager / SessionManager with a fake bot
and both services enabled (memory-only, no DB). Proves: the landing-page
chooser, per-service routing to the right channel, cross-roster claim
rejection, correct anonymous labels ("Peer Supporter" vs "Hearhtfelt Member"),
and that HF and PSS run in parallel without interfering.

Run directly: `python tests/test_pss_flow.py`
"""

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from config import UserState, ServiceType
from src.bot.handlers import BotHandlers
from src.bot.managers.queue import QueueManager
from src.bot.managers.session import SessionManager

HF_MEMBER = 1001
PSS_MEMBER = 2001
REQUESTER_PSS = 5000
REQUESTER_HF = 6000
HF_CHANNEL = "-100HF"
PSS_CHANNEL = "-100PSS"


class Rec:
    def __init__(self):
        self.sent = []      # (chat_id, text, reply_markup)
        self.replies = []   # (text, reply_markup) sent back to the acting user
        self.answers = []   # (text, show_alert)


class FakeBot:
    def __init__(self, rec):
        self.rec = rec
        self._mid = 0

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self._mid += 1
        self.rec.sent.append((str(chat_id), text, reply_markup))
        return SimpleNamespace(message_id=self._mid)

    async def edit_message_text(self, **kw):
        return None

    async def delete_message(self, **kw):
        return None

    async def send_photo(self, **kw):
        return None

    async def send_sticker(self, **kw):
        return None


def make_text_update(user_id, text, rec, username=None):
    async def reply_text(t, reply_markup=None, **kw):
        rec.replies.append((t, reply_markup))
    user = SimpleNamespace(id=user_id, username=username, first_name="User", last_name=None)
    msg = SimpleNamespace(text=text, reply_text=reply_text, photo=[], sticker=None)
    return SimpleNamespace(effective_user=user, message=msg, callback_query=None)


def make_callback_update(user_id, data, rec, username="mem", first="Mem"):
    async def answer(t=None, show_alert=False):
        rec.answers.append((t, show_alert))
    async def edit_message_text(t, **kw):
        rec.replies.append((t, None))
    fu = SimpleNamespace(id=user_id, username=username, first_name=first, last_name=None)
    q = SimpleNamespace(from_user=fu, data=data, answer=answer, edit_message_text=edit_message_text)
    return SimpleNamespace(effective_user=fu, message=None, callback_query=q)


def reset_state():
    for d in (config.user_states, config.user_to_service_map, config.queue_entries,
              config.user_to_queue_map, config.active_sessions, config.user_to_session_map,
              config.session_warnings):
        d.clear()
    config.queue_order.clear()


def enable_both_services():
    hf = config.SERVICES[ServiceType.HF.value]
    pss = config.SERVICES[ServiceType.PSS.value]
    hf.channel_id = HF_CHANNEL
    hf.enabled = True
    hf.roster.replace([HF_MEMBER])
    pss.channel_id = PSS_CHANNEL
    pss.enabled = True
    pss.roster.replace([PSS_MEMBER])
    assert [s.key for s in config.enabled_services()] == ["hf", "pss"], "both services should be runnable"


async def run():
    enable_both_services()
    reset_state()

    rec = Rec()
    bot = FakeBot(rec)
    ctx = SimpleNamespace(bot=bot)
    sm = SessionManager(bot)
    qm = QueueManager(bot)
    handlers = BotHandlers(sm, qm)

    # 1. Requester runs /help -> chooser with two buttons
    await handlers.help_command(make_text_update(REQUESTER_PSS, "/help", rec), ctx)
    assert config.user_states[REQUESTER_PSS] == UserState.WAITING_FOR_SERVICE
    last_text, markup = rec.replies[-1]
    buttons = [b for row in markup.inline_keyboard for b in row]
    cbs = sorted(b.callback_data for b in buttons)
    assert cbs == ["svc_hf", "svc_pss"], f"expected two service buttons, got {cbs}"
    print("OK  1. /help shows the two-service chooser")

    # 2. Requester picks PSS
    await handlers.handle_callback_query(make_callback_update(REQUESTER_PSS, "svc_pss", rec), ctx)
    assert config.user_to_service_map[REQUESTER_PSS] == "pss"
    assert config.user_states[REQUESTER_PSS] == UserState.WAITING_FOR_DESCRIPTION
    print("OK  2. Requester selects PSS")

    # 3. Requester describes issue -> posts to the PSS channel with PSS title
    await handlers.handle_message(make_text_update(REQUESTER_PSS, "I need peer support", rec), ctx)
    pss_posts = [s for s in rec.sent if s[0] == PSS_CHANNEL]
    assert pss_posts, "expected a post to the PSS channel"
    assert "New Peer Support Request" in pss_posts[-1][1]
    claim_btn = pss_posts[-1][2].inline_keyboard[0][0]
    assert claim_btn.callback_data.startswith("claim_")
    queue_id = claim_btn.callback_data[len("claim_"):]
    assert not any(s[0] == HF_CHANNEL for s in rec.sent), "PSS request must NOT hit the HF channel"
    print("OK  3. PSS request routes to the PSS channel with the PSS title")

    # 4. An HF member cannot claim a PSS request (cross-roster rejection)
    await handlers.handle_callback_query(make_callback_update(HF_MEMBER, f"claim_{queue_id}", rec), ctx)
    assert any("not authorized to claim" in (a[0] or "") for a in rec.answers), "HF member should be blocked from PSS claim"
    assert queue_id in config.queue_entries, "entry must survive a rejected cross-roster claim"
    print("OK  4. HF member is blocked from claiming a PSS request")

    # 5. A PSS member claims successfully -> session created with service 'pss'
    await handlers.handle_callback_query(make_callback_update(PSS_MEMBER, f"claim_{queue_id}", rec), ctx)
    assert config.user_states[PSS_MEMBER] == UserState.IN_CONVERSATION
    assert config.user_states[REQUESTER_PSS] == UserState.IN_CONVERSATION
    session_id = sm.get_session_by_user(REQUESTER_PSS)
    assert sm.get_session_info(session_id)["service"] == "pss"
    print("OK  5. PSS member claims; session tagged service=pss")

    # 6. Relay labels: requester sees "Peer Supporter", member sees "RHesident #NNNN"
    rec.sent.clear()
    await handlers.handle_message(make_text_update(PSS_MEMBER, "Hi, I'm here for you", rec), ctx)
    to_requester = [s for s in rec.sent if s[0] == str(REQUESTER_PSS)]
    assert to_requester and "Peer Supporter:" in to_requester[-1][1], "requester should see 'Peer Supporter'"
    rec.sent.clear()
    await handlers.handle_message(make_text_update(REQUESTER_PSS, "thank you", rec), ctx)
    to_member = [s for s in rec.sent if s[0] == str(PSS_MEMBER)]
    assert to_member and "RHesident #" in to_member[-1][1], "PSS member should see the anonymous RHesident id"
    print("OK  6. Anonymous labels correct (requester->'Peer Supporter', member->'RHesident')")

    # 7. HF track still works in parallel with the HF label
    rec2 = Rec()
    bot2 = FakeBot(rec2)
    ctx2 = SimpleNamespace(bot=bot2)
    sm.bot = bot2  # relay uses the manager's bot
    qm.bot = bot2
    await handlers.help_command(make_text_update(REQUESTER_HF, "/help", rec2), ctx2)
    await handlers.handle_callback_query(make_callback_update(REQUESTER_HF, "svc_hf", rec2), ctx2)
    await handlers.handle_message(make_text_update(REQUESTER_HF, "just need a listening ear", rec2), ctx2)
    hf_posts = [s for s in rec2.sent if s[0] == HF_CHANNEL]
    assert hf_posts and "New Help Request" in hf_posts[-1][1]
    hf_qid = hf_posts[-1][2].inline_keyboard[0][0].callback_data[len("claim_"):]
    await handlers.handle_callback_query(make_callback_update(HF_MEMBER, f"claim_{hf_qid}", rec2), ctx2)
    rec2.sent.clear()
    await handlers.handle_message(make_text_update(HF_MEMBER, "hello", rec2), ctx2)
    to_hf_requester = [s for s in rec2.sent if s[0] == str(REQUESTER_HF)]
    assert to_hf_requester and "Hearhtfelt Member:" in to_hf_requester[-1][1], "HF requester should see 'Hearhtfelt Member'"
    print("OK  7. HF track runs in parallel with the 'Hearhtfelt Member' label")


if __name__ == "__main__":
    try:
        asyncio.run(run())
        print("\nAll PSS end-to-end flow assertions passed!")
    finally:
        # Restore PSS to its inert default so importing this test can't leak state.
        pss = config.SERVICES[ServiceType.PSS.value]
        pss.channel_id = config.PSS_CHANNEL_ID
        pss.enabled = False
        pss.roster.replace([])
