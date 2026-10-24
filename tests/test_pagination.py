"""Pagination tests against real botocore service models.

botocore raises OperationNotPageableError from get_paginator() for
operations without a paginator, so a mocked client cannot catch a wrong
operation name. These tests use real clients.
"""

import os
import re
import sys

import boto3
from botocore.stub import Stubber

LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "lambda")
sys.path.insert(0, LAMBDA_DIR)

from steps.monitor import _list_received_licenses  # noqa: E402

# Every operation the Lambda code passes to get_paginator(), with its service.
PAGINATED_OPERATIONS = {
    "list_purchase_options": "marketplace-discovery",
    "get_offer_terms": "marketplace-discovery",
    "list_accounts": "organizations",
}


def _client(service):
    return boto3.client(
        service,
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


def test_monitor_lists_received_licenses_across_pages():
    """ListReceivedLicenses has no botocore paginator, so the monitor follows NextToken itself."""
    client = _client("license-manager")
    first = {"LicenseArn": "arn:aws:license-manager::111122223333:license:l-1"}
    second = {"LicenseArn": "arn:aws:license-manager::111122223333:license:l-2"}

    with Stubber(client) as stubber:
        stubber.add_response(
            "list_received_licenses",
            {"Licenses": [first], "NextToken": "page-2"},
            {"MaxResults": 100},
        )
        stubber.add_response(
            "list_received_licenses",
            {"Licenses": [second]},
            {"MaxResults": 100, "NextToken": "page-2"},
        )

        licenses = _list_received_licenses(client)
        stubber.assert_no_pending_responses()

    assert [lic["LicenseArn"] for lic in licenses] == [
        first["LicenseArn"],
        second["LicenseArn"],
    ]


def test_every_paginated_operation_has_a_botocore_paginator():
    """Each get_paginator() call in lambda/ names an operation botocore can paginate."""
    called = set()
    for root, _, files in os.walk(LAMBDA_DIR):
        for name in files:
            if name.endswith(".py"):
                with open(os.path.join(root, name)) as f:
                    called.update(re.findall(r'get_paginator\(\s*"([a-z_]+)"', f.read()))

    assert called == set(PAGINATED_OPERATIONS), (
        "Update PAGINATED_OPERATIONS to match the get_paginator() calls in lambda/"
    )
    for operation, service in PAGINATED_OPERATIONS.items():
        assert _client(service).can_paginate(operation), f"{service}.{operation}"
