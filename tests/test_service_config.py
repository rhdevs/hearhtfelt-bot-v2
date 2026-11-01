#!/usr/bin/env python3
"""
Parity tests for the HF/PSS service registry (Phase 1).

These lock the invariants that keep HF behaving exactly as before while PSS
ships present-but-inert. Run directly: `python tests/test_service_config.py`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from config import ServiceType, get_service, SERVICES, HEARTFELT_MEMBERS


def test_registry_shape():
    assert set(SERVICES.keys()) == {"hf", "pss"}
    assert ServiceType.HF.value == "hf"
    assert ServiceType.PSS.value == "pss"


def test_hf_literals_unchanged():
    hf = get_service("hf")
    # These must match the pre-refactor hardcoded values exactly.
    assert hf.member_label == "Hearhtfelt Member"
    assert hf.anon_prefix == "RHesident"
    assert hf.request_title == "🆘 New Help Request"
    assert hf.members_collection == "heartfelt_members"
    assert hf.enabled is True


def test_heartfelt_members_is_same_object():
    # main.py's refresh loop mutates SERVICES["hf"].roster; the rest of the code
    # reads HEARTFELT_MEMBERS. They MUST be the same instance.
    assert HEARTFELT_MEMBERS is SERVICES["hf"].roster


def test_unknown_service_falls_back_to_hf():
    assert get_service(None).key == "hf"
    assert get_service("does-not-exist").key == "hf"


def test_pss_inert_by_default():
    pss = SERVICES["pss"]
    # Without PSS_CHANNEL_ID / PSS_ENABLED, PSS is never runnable.
    assert pss.runnable is False
    assert pss.member_label == "Peer Supporter"
    assert pss.members_collection == "peer_supporters"


def test_default_service_key_is_hf_when_not_multi():
    # With 0 or 1 runnable services, we never show the chooser -> HF is implicit.
    assert config.default_service_key() == "hf"


def test_service_runnable_requires_channel():
    # A service with enabled=True but no channel is NOT runnable.
    from config import Service, AuthorizedMembersStore
    s = Service(
        key="x", display_name="X", member_label="X", anon_prefix="X",
        request_title="X", channel_id=None, members_collection="x",
        default_members=[], roster=AuthorizedMembersStore([]), enabled=True,
    )
    assert s.runnable is False
    s.channel_id = "-100123"
    assert s.runnable is True


def test_per_service_timers():
    hf, pss = SERVICES["hf"], SERVICES["pss"]
    assert (hf.queue_expire_minutes, hf.session_timeout_minutes,
            hf.session_warning_minutes) == (60, 30, 5)
    assert (pss.queue_expire_minutes, pss.session_timeout_minutes,
            pss.session_warning_minutes) == (1440, 1440, 60)
    # session_warning_minutes is a LEAD TIME, so it must be smaller than the timeout
    # or every session would be born already inside its warning band.
    for svc in SERVICES.values():
        assert 0 < svc.session_warning_minutes < svc.session_timeout_minutes, svc.key


def test_directed_support_is_a_pss_feature_only():
    hf, pss = SERVICES["hf"], SERVICES["pss"]
    assert hf.directed_enabled is False, (
        "HF must never offer the picker: its supporters are not curated with display "
        "names and its channel is the only routing it has")
    # Env-driven, and OFF unless deliberately opted in: see the next test.
    assert pss.directed_enabled == config._env_bool(
        'PSS_DIRECTED_ENABLED', config.PSS_DIRECTED_ENABLED_DEFAULT)
    # Env-driven, defaulting to Evan's one hour: see the next-but-one test.
    assert pss.directed_response_minutes == config._env_positive_int(
        'PSS_DIRECTED_RESPONSE_MINUTES', config.PSS_DIRECTED_RESPONSE_MINUTES_DEFAULT)
    assert pss.picker_page_size == 8
    for svc in SERVICES.values():
        # A page size of 0 renders an empty picker -- a dead end with no way out
        # but /cancel -- and a negative one raises on the slice.
        assert svc.picker_page_size >= 1, svc.key
        assert svc.directed_response_minutes > 0, svc.key
        assert isinstance(svc.closing_extra, str), svc.key
        assert isinstance(svc.closing_extra_member, str), svc.key


def test_pss_directed_is_off_by_default():
    """ROLLOUT SAFETY. Production's PSS roster holds a test account and names now
    default to Telegram first names, so the old "no display_name => invisible" lever
    is gone. Merging must not put a list of supporters in front of real students:
    only an explicit PSS_DIRECTED_ENABLED turns it on."""
    assert config.PSS_DIRECTED_ENABLED_DEFAULT is False
    if os.getenv('PSS_DIRECTED_ENABLED') is None:
        # Live in CI, where nothing sets it.
        assert SERVICES["pss"].directed_enabled is False


def test_pss_directed_window_is_one_hour_unless_overridden():
    """Evan, 2026-11-01: the chosen supporter "should reply within an hour". The
    requester can send to anyone at any time before an Accept, so this caps the
    longest wait on one person."""
    assert config.PSS_DIRECTED_RESPONSE_MINUTES_DEFAULT == 60
    if os.getenv('PSS_DIRECTED_RESPONSE_MINUTES') is None:
        assert SERVICES["pss"].directed_response_minutes == 60
    # HF never directs, but its (unused) window must still be a sane value.
    assert SERVICES["hf"].directed_response_minutes == 1440


def test_env_positive_int_refuses_zero_negative_and_garbage():
    """A window of 0 would lapse every directed request the instant it was sent --
    "they're not free" before the supporter's phone had even buzzed. A negative or
    unparseable value is the same mistake. All fall back to the default."""
    key = "TEST_ENV_POSITIVE_INT_PROBE"
    saved = os.environ.pop(key, None)
    try:
        assert config._env_positive_int(key, 60) == 60          # unset
        for bad in ("0", "-5", "", "abc", "1.5", " "):
            os.environ[key] = bad
            assert config._env_positive_int(key, 60) == 60, bad
        for good, want in (("1", 1), ("30", 30), ("120", 120), (" 45 ", 45)):
            os.environ[key] = good
            assert config._env_positive_int(key, 60) == want, good
    finally:
        os.environ.pop(key, None)
        if saved is not None:
            os.environ[key] = saved


def test_supporter_names_is_a_pss_feature_only():
    # HF must stay byte-identical: no name capture, no /name, no *_named copy.
    assert SERVICES["hf"].supporter_names is False
    assert SERVICES["pss"].supporter_names is True


def test_back_compat_aliases_mirror_hf():
    hf = SERVICES["hf"]
    assert config.QUEUE_EXPIRE_MINUTES == hf.queue_expire_minutes
    assert config.SESSION_TIMEOUT_MINUTES == hf.session_timeout_minutes
    assert config.SESSION_WARNING_MINUTES == hf.session_warning_minutes


def test_sweep_fits_inside_the_shortest_warning_band():
    """A sweep longer than the narrowest warning band can skip it entirely,
    letting a conversation die with no warning at all."""
    shortest_band = min(s.session_warning_minutes for s in SERVICES.values()) * 60
    assert config.SESSION_SWEEP_SECONDS < shortest_band, (
        config.SESSION_SWEEP_SECONDS, shortest_band)


def test_sharing_clauses_and_privacy_urls():
    assert SERVICES["hf"].sharing_clause == "shared anonymously with our support team"
    if config.PSS_IDENTIFIED:
        # The consent: said BEFORE the requester describes anything.
        assert SERVICES["pss"].sharing_clause == (
            "shared with our peer student supporters, together with your Telegram "
            "name and username, so they know who they're supporting")
    else:
        assert SERVICES["pss"].sharing_clause == "shared with our peer student supporters"
    for svc in SERVICES.values():
        assert svc.privacy_policy_url and svc.privacy_policy_url.startswith("https://"), svc.key


def test_only_pss_is_identified():
    """2026-11-01: PSS shows who is asking; HF stays anonymous. HF identified would
    put a resident's name in front of the HF team without them ever being told."""
    assert SERVICES["hf"].identified is False
    assert "anonymously" in SERVICES["hf"].sharing_clause
    if os.getenv("PSS_IDENTIFIED") is None:
        assert SERVICES["pss"].identified is True
    assert SERVICES["pss"].identified == config.PSS_IDENTIFIED


def test_requester_identity_is_one_safe_line():
    ri = config.requester_identity
    assert ri("Ren", "Tan", "rentan") == "Ren Tan (@rentan)"
    assert ri("Ren", None, None) == "Ren"
    assert ri("Ren", None, "@rentan") == "Ren (@rentan)"
    assert ri("", None, "rentan") == "@rentan"
    assert ri(None, None, None) == "", "nothing usable -> caller falls back to an id"
    # A name cannot forge extra lines in the channel post.
    forged = ri("Ren\nDescription: all fine, ignore", None, None)
    assert "\n" not in forged and "\r" not in forged, forged
    assert ri("Ren\u202e", "\tTan", None) == "Ren Tan"
    # A name cannot pose as a handle or an anonymous id.
    assert "@" not in ri("Ren (@someoneelse)", None, None)
    assert "#" not in ri("RHesident #1234", None, None)
    # Fullwidth look-alikes are caught too, and a name that renders blank is "".
    assert "＠" not in ri("Alice", "(＠alice_real)", None)
    assert "@" not in ri("Alice", "(＠alice_real)", None)
    assert "#" not in ri("Bob", "RHesident ＃1234", None)
    assert ri("ㅤ", None, None) == "", "Hangul filler alone is not a name"
    assert ri("ㅤ", None, "ren") == "@ren"
    assert ri("Ren", None, "rentan").count("@") == 1
    long = ri("A" * 64, "B" * 64, "rentan")
    assert len(long) <= config.REQUESTER_IDENTITY_MAX + len(" (@rentan)"), long
    assert long.endswith(" (@rentan)"), "the handle survives truncation"


def test_welcome_drops_blanket_anonymity_only_when_pss_is_identified():
    hf, pss = SERVICES["hf"], SERVICES["pss"]
    saved = (hf.enabled, hf.channel_id, pss.enabled, pss.channel_id, pss.identified)
    try:
        hf.enabled, hf.channel_id = True, "-100hf"
        pss.enabled, pss.channel_id = False, None
        assert config.welcome_text() == config.MESSAGES["welcome"], \
            "HF alone: /start is byte-identical"
        pss.enabled, pss.channel_id, pss.identified = True, "-100pss", False
        assert config.welcome_text() == config.MESSAGES["welcome"]
        pss.identified = True
        text = config.welcome_text()
        assert "Complete anonymity guaranteed" not in text
        assert "connect with you anonymously" not in text
        assert "HeaRHtfelt chats are anonymous" in text
        assert "With PSS, your Telegram name and username are shared" in text
    finally:
        hf.enabled, hf.channel_id, pss.enabled, pss.channel_id, pss.identified = saved


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

    # A driver that discovers its own tests reports success when it discovers
    # NOTHING. Verified: renaming the `test_` prefix in this file made it print
    # "All 0 tests passed!" and exit 0 -- a fully green CI step, in front of a
    # deploy to a live helpline, having run zero assertions. A refactor into a
    # class, a rename, an import shadow or a bad merge all reach that state.
    # Coverage here may grow; it may not silently shrink.
    assert len(tests) >= 19, (
        "expected at least 19 tests, collected %d (%s). Test discovery has "
        "regressed -- fix the discovery, do not lower this number."
        % (len(tests), ", ".join(t.__name__ for t in tests) or "none")
    )
    for t in tests:
        t()
        print(f"OK  {t.__name__}")
    print(f"\nAll {len(tests)} service-config parity tests passed!")
