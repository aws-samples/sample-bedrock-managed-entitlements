"""Regression tests for steps.discover_license.

These exist because the ambiguity-guard filter added in PR #2 compared
License Manager's `CreateTime` (a string of Unix epoch seconds, e.g.
"1786405121") directly against the EventBridge event's `acceptanceTime`
(ISO8601, e.g. "2026-08-15T04:37:13Z") with a plain string `<` comparison.
That comparison is lexicographic, not numeric/temporal, so
"1786405121" < "2026-08-15T04:37:13Z" evaluates True for basically any real
timestamp (because "1" < "2" as characters) -- meaning the filter silently
excluded every real, correctly-issued license, every time. This was only
found by live-invoking the deployed Lambda against a real account; the
existing test_handler.py suite could not catch it because it mocks
discover_license() entirely and never exercises this comparison.

test_handler.py's mocking approach is intentionally left alone -- these
tests specifically target the real comparison logic instead.
"""

import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

from steps.discover_license import discover_license, _to_epoch_seconds  # noqa: E402


def _license(
    license_arn="arn:aws:license-manager::294406891311:license:l-test",
    issuer_name="AWS/Marketplace",
    create_time="1786405121",  # real License Manager shape: epoch-seconds string
    status="AVAILABLE",
    product_name="Test Product",
    product_sku="prod-test",
):
    return {
        "LicenseArn": license_arn,
        "Issuer": {"Name": issuer_name},
        "CreateTime": create_time,
        "Status": status,
        "ProductName": product_name,
        "ProductSKU": product_sku,
    }


def _client_returning(licenses):
    client = MagicMock()
    client.list_received_licenses.return_value = {"Licenses": licenses}
    return client


class TestToEpochSeconds:
    def test_epoch_string(self):
        assert _to_epoch_seconds("1786405121") == 1786405121.0

    def test_epoch_number(self):
        assert _to_epoch_seconds(1786405121) == 1786405121.0

    def test_iso8601_string(self):
        # 2026-08-15T04:37:13Z as epoch seconds
        result = _to_epoch_seconds("2026-08-15T04:37:13Z")
        assert result is not None
        assert result > 1_700_000_000  # sane recent epoch, not garbage

    def test_epoch_and_iso_are_comparable(self):
        # This is the actual bug: these two real-world values must compare
        # correctly once normalized, even though the raw strings would sort
        # the wrong way lexicographically ("1" < "2").
        create_time_epoch_str = "1786405121"
        acceptance_time_iso = "2026-08-15T04:37:13Z"
        assert create_time_epoch_str < acceptance_time_iso  # sanity: string compare is wrong
        create_epoch = _to_epoch_seconds(create_time_epoch_str)
        acceptance_epoch = _to_epoch_seconds(acceptance_time_iso)
        # The license (created 2026-08-10 per this real epoch value) was in
        # fact created BEFORE this acceptance time -- numeric comparison
        # must say so, unlike the old string comparison which said the
        # opposite.
        assert create_epoch < acceptance_epoch

    def test_empty_and_none(self):
        assert _to_epoch_seconds("") is None
        assert _to_epoch_seconds(None) is None

    def test_unparseable_returns_none_not_raise(self):
        assert _to_epoch_seconds("not-a-timestamp") is None

    def test_epoch_zero_is_valid_not_none(self):
        # Epoch 0 (1970-01-01) is a falsy int/float but a genuinely valid
        # timestamp -- must not be conflated with "empty/unparseable".
        assert _to_epoch_seconds(0) == 0.0
        assert _to_epoch_seconds(0.0) == 0.0
        assert _to_epoch_seconds("0") == 0.0

    def test_naive_iso8601_assumed_utc(self):
        # A naive ISO8601 string (no Z/offset) must be treated as UTC, not
        # the ambient system timezone -- otherwise the same input parses to
        # a different epoch value depending on where the code runs.
        naive = _to_epoch_seconds("2026-08-15T04:37:13")
        aware = _to_epoch_seconds("2026-08-15T04:37:13Z")
        assert naive == aware


class TestDiscoverLicenseRealShapeRegression:
    """Exercises the real comparison path with monkeypatched boto3 client,
    using CreateTime/acceptanceTime shapes exactly as seen in a real account
    (epoch-seconds string vs ISO8601 string) -- the combination the old
    string comparison got wrong.

    Real production flow: a Marketplace license is created ~1-2 minutes
    AFTER the agreement's acceptance is processed (that's exactly why
    discover_license retries with backoff). So the true happy-path case is
    create_epoch > acceptance_epoch (license newer than acceptance). The old
    buggy string comparison ('1786848000' < '2026-08-15T04:37:13Z' -- True,
    because '1' < '2' as characters) excluded this exact case 100% of the
    time for any current-era timestamp, silently breaking every real event.
    """

    ACCEPTANCE_ISO = "2026-08-15T04:37:13Z"

    def test_finds_license_created_shortly_after_acceptance(self, monkeypatch):
        # The real happy path: license appears ~2 minutes after acceptance.
        # The old string comparison always wrongly excluded this.
        acceptance_epoch = _to_epoch_seconds(self.ACCEPTANCE_ISO)
        create_time = str(int(acceptance_epoch) + 120)
        lic = _license(create_time=create_time)
        client = _client_returning([lic])
        monkeypatch.setattr(
            "steps.discover_license.boto3.client", lambda *a, **k: client
        )

        result = discover_license(
            proposer_account_id="764576996850",
            issuer_name="AWS/Marketplace",
            acceptance_time=self.ACCEPTANCE_ISO,
            max_retries=1,
            base_delay=0,
        )

        assert result is not None
        assert result["license_arn"] == lic["LicenseArn"]

    def test_excludes_license_created_well_before_acceptance(self, monkeypatch):
        # A stale, pre-existing license from an unrelated earlier
        # subscription (created 10 days before this agreement's acceptance)
        # must not be mistaken for the license issued by this agreement.
        acceptance_epoch = _to_epoch_seconds(self.ACCEPTANCE_ISO)
        create_time = str(int(acceptance_epoch) - 864000)  # 10 days earlier
        lic = _license(create_time=create_time)
        client = _client_returning([lic])
        monkeypatch.setattr(
            "steps.discover_license.boto3.client", lambda *a, **k: client
        )

        result = discover_license(
            proposer_account_id="764576996850",
            issuer_name="AWS/Marketplace",
            acceptance_time=self.ACCEPTANCE_ISO,
            max_retries=1,
            base_delay=0,
        )

        assert result is None

    def test_excludes_wrong_issuer(self, monkeypatch):
        lic = _license(issuer_name="Some Other Issuer")
        client = _client_returning([lic])
        monkeypatch.setattr(
            "steps.discover_license.boto3.client", lambda *a, **k: client
        )

        result = discover_license(
            proposer_account_id="764576996850",
            issuer_name="AWS/Marketplace",
            acceptance_time="2026-08-15T04:37:13Z",
            max_retries=1,
            base_delay=0,
        )

        assert result is None

    def test_fails_closed_on_ambiguous_match(self, monkeypatch):
        lic_a = _license(license_arn="arn:aws:license-manager::294406891311:license:l-a")
        lic_b = _license(license_arn="arn:aws:license-manager::294406891311:license:l-b")
        client = _client_returning([lic_a, lic_b])
        monkeypatch.setattr(
            "steps.discover_license.boto3.client", lambda *a, **k: client
        )

        result = discover_license(
            proposer_account_id="764576996850",
            issuer_name="AWS/Marketplace",
            acceptance_time="2026-08-15T04:37:13Z",
            max_retries=1,
            base_delay=0,
        )

        assert result is None

    def test_unparseable_timestamp_does_not_wrongly_exclude(self, monkeypatch):
        # If CreateTime is somehow unparseable, fail open on the timestamp
        # check specifically (rely on issuer match) rather than silently
        # excluding a genuinely matching license due to a parse failure.
        lic = _license(create_time="garbage-not-a-timestamp")
        client = _client_returning([lic])
        monkeypatch.setattr(
            "steps.discover_license.boto3.client", lambda *a, **k: client
        )

        result = discover_license(
            proposer_account_id="764576996850",
            issuer_name="AWS/Marketplace",
            acceptance_time="2026-08-15T04:37:13Z",
            max_retries=1,
            base_delay=0,
        )

        assert result is not None
        assert result["license_arn"] == lic["LicenseArn"]
