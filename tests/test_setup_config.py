"""Tests for interactive configuration setup."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.setup_config import build_seller_entry


def test_build_seller_entry_defaults_no_auto_accept():
    """A seller with no auto-accept opt-in gets no autoAcceptOffers field."""
    entry = build_seller_entry("Anthropic", "444455556666")

    assert entry == {
        "name": "Anthropic",
        "proposerAccountId": "444455556666",
        "autoActivateGrant": True,
        "replaceLegacyGrants": False,
    }
    assert "autoAcceptOffers" not in entry
    assert "sellerProfileId" not in entry


def test_build_seller_entry_auto_accept_with_profile_id():
    """A seller opted into auto-accept with a profile ID gets both fields set."""
    entry = build_seller_entry(
        "Anthropic", "444455556666", auto_accept=True, seller_profile_id="prof-anthropic",
    )

    assert entry["autoAcceptOffers"] is True
    assert entry["sellerProfileId"] == "prof-anthropic"


def test_build_seller_entry_auto_accept_without_profile_id_fails_closed_in_config():
    """A seller opted into auto-accept but missing a profile ID is written without
    sellerProfileId -- the Lambda and seed_sellers.py both fail closed on this and
    report why, rather than silently matching nothing.
    """
    entry = build_seller_entry("Anthropic", "444455556666", auto_accept=True)

    assert entry["autoAcceptOffers"] is True
    assert "sellerProfileId" not in entry
