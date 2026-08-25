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
    assert SERVICES["pss"].sharing_clause == "shared with our peer student supporters"
    for svc in SERVICES.values():
        assert svc.privacy_policy_url and svc.privacy_policy_url.startswith("https://"), svc.key


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"OK  {t.__name__}")
    print(f"\nAll {len(tests)} service-config parity tests passed!")
