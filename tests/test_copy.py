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


# Every spelling of the pre-Phase-1 brand. Compared against a lowercased string,
# so "Heartfelt", "HeaRHtfelt" and "HEARTFELT" are all covered.
STALE_BRAND_TOKENS = ("heartfelt", "hearhtfelt")

# Words that must never appear in anything the REQUESTER reads about a supporter.
# A decline and a 24-hour silence are the same event to them; a release is not
# something that was done to them. See D24 and D28.
BLAME_WORDS = ("declin", "rejected", "dropped", "abandoned", "gave up",
               "turned you down", "said no", "left you", "walked")

# Requester-facing keys added by Phases 5 and 6. released_* is matched by prefix as
# well, so a key added later is covered without editing this list.
NO_BLAME_KEYS = (
    "comfort_question", "comfort_specific_button", "comfort_anyone_button",
    "picker_nobody_free", "picker_lost_view", "picker_not_a_number",
    "picker_busy", "picker_unreachable", "picker_header", "picker_hint",
    "directed_sent", "directed_unavailable", "next_step_question",
    "directed_status", "choosing_status", "choosing_expired", "directed_gone",
)


def test_welcome_is_rebranded():
    welcome = MESSAGES["welcome"]

    # Pin the greeting line itself, not merely "Care Network appears somewhere".
    # `assert "Care Network" in welcome` stayed true when the FIRST line -- the
    # most-read string in the product, the one every requester sees on /start --
    # was reverted to the old brand, because the two later mentions still
    # satisfied it. See test_no_message_mentions_the_old_brand.
    assert welcome.startswith("Welcome to the Care Network Bot"), (
        "welcome must open with the Care Network greeting, got: "
        f"{welcome.splitlines()[0]!r}"
    )

    assert "Care Network" in welcome
    assert "HeaRHtfelt Companion Helpline" not in welcome
    assert "/chat" in welcome


def test_no_message_mentions_the_old_brand():
    """No requester-facing string may carry the pre-rebrand name.

    This is the assertion that makes a PARTIAL rebrand fail. MESSAGES["welcome"]
    says "Care Network" three times; reverting any ONE of them to "Heartfelt"
    left `"Care Network" in welcome` true and all nine suites green, so a
    half-rebranded greeting could reach a live helpline straight through the CI
    gate. Asserting the ABSENCE of the old brand cannot be satisfied by a
    surviving mention elsewhere in the same string.

    Message KEYS may still say heartfelt -- conversation_ended_heartfelt and
    session_expired_heartfelt are the member-facing variants. Their VALUES may not.
    """
    for key, value in MESSAGES.items():
        assert isinstance(value, str), key
        lowered = value.lower()
        for token in STALE_BRAND_TOKENS:
            assert token not in lowered, (
                f"MESSAGES[{key!r}] still mentions the old brand ({token!r}). "
                "Phase 1 rebranded requester-facing copy to 'Care Network'; a "
                "surviving mention means the rewrite was only partial. "
                f"Value: {value!r}"
            )


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


def test_requester_facing_copy_never_blames_a_supporter():
    """The single most important copy rule in this feature.

    "{name} isn't free right now" is the ONLY wording for both a decline and a
    24-hour silence. Anything that distinguishes the two turns a supporter's
    completely legitimate "not tonight" into a rejection the requester carries.
    """
    keys = [k for k in NO_BLAME_KEYS if k in MESSAGES]
    keys += [k for k in MESSAGES if k.startswith("released_")]
    assert len(keys) >= 15, (
        "far fewer requester-facing keys than expected (%d); this scan would be "
        "nearly vacuous: %s" % (len(keys), sorted(keys)))
    for key in keys:
        lowered = MESSAGES[key].lower()
        for word in BLAME_WORDS:
            assert word not in lowered, (
                f"MESSAGES[{key!r}] is requester-facing and says {word!r}. A decline "
                "and a silence must be indistinguishable, and a release is not "
                f"something that was done to them. Value: {MESSAGES[key]!r}")


def test_phase_five_and_six_templates_render():
    """Every template with a slot renders, and leaves nothing unrendered behind."""
    rendered = {
        "directed_unavailable": MESSAGES["directed_unavailable"].format(name="Alex"),
        "directed_status": MESSAGES["directed_status"].format(name="Alex"),
        "directed_sent": MESSAGES["directed_sent"].format(name="Alex"),
        "picker_page": MESSAGES["picker_page"].format(page=2, pages=3),
        "directed_request": MESSAGES["directed_request"].format(
            description="a thing", window="24 hours"),
    }
    for key, text in rendered.items():
        assert "{" not in text and "}" not in text, (key, text)
        assert text.strip(), key
    assert "Alex" in rendered["directed_unavailable"]
    assert "Page 2 of 3" == rendered["picker_page"]


def test_member_addendum_names_the_member_only_commands():
    """These three are deliberately absent from set_my_commands -- that menu is the
    requester's surface -- so this addendum is the ONLY way a supporter finds them."""
    addendum = MESSAGES["member_addendum"]
    for command in ("/available", "/unavailable", "/release"):
        assert command in addendum, (command, addendum)


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

    # A driver that discovers its own tests reports success when it discovers
    # NOTHING. Verified: renaming the `test_` prefix in this file made it print
    # "All 0 tests passed!" and exit 0 -- a fully green CI step, in front of a
    # deploy to a live helpline, having run zero assertions. A refactor into a
    # class, a rename, an import shadow or a bad merge all reach that state.
    # Coverage here may grow; it may not silently shrink.
    assert len(tests) >= 13, (
        "expected at least 13 tests, collected %d (%s). Test discovery has "
        "regressed -- fix the discovery, do not lower this number."
        % (len(tests), ", ".join(t.__name__ for t in tests) or "none")
    )
    for t in tests:
        t()
        print(f"OK  {t.__name__}")
    print(f"\nAll {len(tests)} copy tests passed!")
