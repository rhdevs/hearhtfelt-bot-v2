#!/usr/bin/env python3
"""
Copy guards for the Care Network rebrand (Phase 1).

The important one is test_help_is_mentioned_exactly_once: /chat is the primary
command and /help survives only as an alias, mentioned deliberately once in the
welcome text. Any other surviving "/help" is a missed rewrite.

Run directly: `python tests/test_copy.py`
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from config import (
    HF_PRIVACY_POLICY_URL,
    MESSAGES,
    PSS_PRIVACY_POLICY_URL,
    SERVICES,
    help_request_text,
)


def test_welcome_is_rebranded():
    welcome = MESSAGES["welcome"]
    assert "Care Network" in welcome
    assert "HeaRHtfelt Companion Helpline" not in welcome
    assert "/chat" in welcome


def test_help_is_mentioned_exactly_once():
    """Exactly one deliberate /help mention (the alias note in `welcome`)."""
    hits = [k for k, v in MESSAGES.items() if isinstance(v, str) and "/help" in v]
    assert hits == ["welcome"], f"unexpected /help mentions in MESSAGES: {hits}"


def test_conversation_ended_says_care_network():
    assert "Thank you for using the Care Network." in MESSAGES["conversation_ended"]


def test_help_request_text_is_per_service():
    hf = help_request_text("hf")
    assert "shared anonymously with our support team" in hf
    assert HF_PRIVACY_POLICY_URL in hf

    pss = help_request_text("pss")
    assert "shared with our peer student supporters" in pss
    assert PSS_PRIVACY_POLICY_URL in pss


def test_help_request_text_falls_back_to_hf():
    assert help_request_text(None) == help_request_text("hf")
    assert help_request_text("does-not-exist") == help_request_text("hf")


def test_help_request_template_has_no_unrendered_slots():
    for key in ("hf", "pss"):
        rendered = help_request_text(key)
        assert "{" not in rendered and "}" not in rendered, rendered


def test_every_service_has_a_usable_privacy_url():
    for svc in SERVICES.values():
        assert isinstance(svc.privacy_policy_url, str)
        assert svc.privacy_policy_url.startswith("https://"), svc.key
        assert svc.sharing_clause, svc.key


def test_session_warning_is_a_duration_template():
    template = MESSAGES["session_warning"]
    assert "{duration}" in template
    rendered = template.format(duration="5 minutes")
    assert "5 minutes" in rendered
    assert "{" not in rendered


def test_bot_command_menu():
    import main
    assert [c.command for c in main.BOT_COMMANDS] == ["chat", "status", "cancel", "end"]
    for c in main.BOT_COMMANDS:
        # Telegram's constraints: names [a-z0-9_]{1,32}, descriptions 1-256 chars.
        assert 1 <= len(c.command) <= 32
        assert all(ch.islower() or ch.isdigit() or ch == "_" for ch in c.command), c.command
        assert 1 <= len(c.description) <= 256


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK  {t.__name__}")
    print(f"\nAll {len(tests)} copy tests passed!")
