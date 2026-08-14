"""Tests for the auto-accept offer handler."""

import os
import sys
from unittest.mock import MagicMock, patch

import boto3
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

from auto_accept_handler import (
    _accept_offer,
    _list_private_offers,
    _verify_agreement_proposer,
    lambda_handler,
)


class FakeDiscoveryClient:
    """Minimal fake for marketplace-discovery used in offer listing/accept tests."""

    def __init__(self, purchase_options, offers, offer_terms):
        self.purchase_options = purchase_options
        self.offers = offers
        self.offer_terms = offer_terms
        self.filter_calls = []
        self.meta = MagicMock()
        self.meta.region_name = "us-east-1"

    def get_paginator(self, operation_name):
        if operation_name == "list_purchase_options":
            def _paginate(filters):
                self.filter_calls.append(filters)
                return [{"purchaseOptions": self.purchase_options}]
            return _StaticPaginator(None, paginate_fn=_paginate)
        if operation_name == "get_offer_terms":
            def _paginate(offerId):
                terms = self.offer_terms.get(offerId, [])
                return [{"offerTerms": terms}]
            return _StaticPaginator(None, paginate_fn=_paginate)
        raise ValueError(operation_name)

    def get_offer(self, offerId):
        return self.offers[offerId]


class _StaticPaginator:
    def __init__(self, pages, paginate_fn=None):
        self._pages = pages
        self._paginate_fn = paginate_fn

    def paginate(self, **kwargs):
        if self._paginate_fn:
            return self._paginate_fn(**kwargs)
        return self._pages


class FakeAgreementClient:
    """Minimal fake for marketplace-agreement used in accept tests."""

    def __init__(self, create_response, accept_response, agreement=None):
        self.create_response = create_response
        self.accept_response = accept_response
        self.agreement = agreement or {}
        self.create_calls = []
        self.accept_calls = []
        self.meta = MagicMock()
        self.meta.region_name = "us-east-1"

    def create_agreement_request(self, **kwargs):
        self.create_calls.append(kwargs)
        return self.create_response

    def accept_agreement_request(self, **kwargs):
        self.accept_calls.append(kwargs)
        return self.accept_response

    def describe_agreement(self, agreementId):
        return self.agreement


def test_list_private_offers_resolves_seller_identity():
    """Private offers are resolved to seller profile ID and display name."""
    discovery = FakeDiscoveryClient(
        purchase_options=[
            {"purchaseOptionId": "offer-abc123", "purchaseOptionType": "OFFER"},
            {"purchaseOptionId": "offerset-xyz", "purchaseOptionType": "OFFERSET"},
        ],
        offers={
            "offer-abc123": {
                "offerId": "offer-abc123",
                "agreementProposalId": "prop-abc123",
                "sellerOfRecord": {
                    "sellerProfileId": "prof-anthropic",
                    "displayName": "Anthropic",
                },
            },
        },
        offer_terms={},
    )

    offers = _list_private_offers(discovery, ["prof-anthropic"])

    assert offers == [
        {
            "offer_id": "offer-abc123",
            "agreement_proposal_id": "prop-abc123",
            "seller_profile_id": "prof-anthropic",
            "seller_name": "Anthropic",
        }
    ]


def test_list_private_offers_filters_by_seller_profile_id():
    """Trusted seller profile IDs are pushed into the ListPurchaseOptions filter."""
    discovery = FakeDiscoveryClient(purchase_options=[], offers={}, offer_terms={})

    _list_private_offers(discovery, ["prof-a", "prof-b"])

    assert len(discovery.filter_calls) == 1
    filters = {f["filterType"]: f["filterValues"] for f in discovery.filter_calls[0]}
    assert filters["VISIBILITY_SCOPE"] == ["PRIVATE"]
    assert filters["SELLER_OF_RECORD_PROFILE_ID"] == ["prof-a", "prof-b"]


def test_list_private_offers_chunks_filter_values():
    """ListPurchaseOptions accepts at most 10 filter values, so IDs are chunked."""
    discovery = FakeDiscoveryClient(purchase_options=[], offers={}, offer_terms={})

    _list_private_offers(discovery, [f"prof-{i}" for i in range(23)])

    assert len(discovery.filter_calls) == 3
    sizes = [
        len({f["filterType"]: f["filterValues"] for f in call}["SELLER_OF_RECORD_PROFILE_ID"])
        for call in discovery.filter_calls
    ]
    assert sizes == [10, 10, 3]


def test_list_private_offers_skips_offer_sets():
    """OFFERSET purchase options are not individually acceptable offers."""
    discovery = FakeDiscoveryClient(
        purchase_options=[
            {"purchaseOptionId": "offerset-xyz", "purchaseOptionType": "OFFERSET"},
        ],
        offers={},
        offer_terms={},
    )

    offers = _list_private_offers(discovery, ["prof-anthropic"])

    assert offers == []


@patch("auto_accept_handler.boto3.client")
def test_accept_offer_creates_and_accepts_agreement(mock_boto_client):
    """Accepting an offer fetches terms, creates the agreement request, then accepts it."""
    discovery = FakeDiscoveryClient(
        purchase_options=[],
        offers={},
        offer_terms={
            "offer-abc123": [
                {"fixedUpfrontPricingTerm": {"id": "term-1", "type": "FixedUpfrontPricingTerm"}},
                {"legalTerm": {"id": "term-2", "type": "LegalTerm"}},
            ]
        },
    )
    mock_boto_client.return_value = discovery

    agreement = FakeAgreementClient(
        create_response={"agreementRequestId": "req-123", "chargeSummary": {}},
        accept_response={"agreementId": "agmt-789"},
    )

    offer = {
        "offer_id": "offer-abc123",
        "agreement_proposal_id": "prop-abc123",
        "seller_profile_id": "prof-anthropic",
        "seller_name": "Anthropic",
    }

    result = _accept_offer(agreement, offer)

    assert result == {"agreementId": "agmt-789"}
    assert agreement.create_calls[0]["agreementProposalIdentifier"] == "prop-abc123"
    assert agreement.create_calls[0]["intent"] == "NEW"
    assert {"id": "term-1"} in agreement.create_calls[0]["requestedTerms"]
    assert {"id": "term-2"} in agreement.create_calls[0]["requestedTerms"]
    assert agreement.accept_calls[0]["agreementRequestId"] == "req-123"


def test_accept_offer_raises_without_agreement_proposal_id():
    """An offer missing agreementProposalId cannot be accepted."""
    offer = {
        "offer_id": "offer-abc123",
        "agreement_proposal_id": None,
        "seller_profile_id": "prof-anthropic",
        "seller_name": "Anthropic",
    }

    try:
        _accept_offer(FakeAgreementClient({}, {}), offer)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_verify_agreement_proposer_accepts_matching_account():
    """A proposer account matching the allow-list record reports no problem."""
    agreement = FakeAgreementClient({}, {}, agreement={"proposer": {"accountId": "444455556666"}})

    assert _verify_agreement_proposer(agreement, "agmt-789", "444455556666") == ""


def test_verify_agreement_proposer_flags_mismatched_account():
    """A proposer account differing from the allow-list record is reported."""
    agreement = FakeAgreementClient({}, {}, agreement={"proposer": {"accountId": "999988887777"}})

    problem = _verify_agreement_proposer(agreement, "agmt-789", "444455556666")

    assert "999988887777" in problem
    assert "444455556666" in problem


def _seed_table(items):
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="mppo-allowed-sellers",
        KeySchema=[{"AttributeName": "proposerAccountId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "proposerAccountId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    for item in items:
        table.put_item(Item=item)
    return table


@mock_aws
@patch("auto_accept_handler._list_private_offers")
@patch("auto_accept_handler._accept_offer")
def test_lambda_handler_only_accepts_trusted_sellers(mock_accept, mock_list):
    """Offers whose seller profile is not in the allow-list are skipped."""
    _seed_table([{
        "proposerAccountId": "444455556666",
        "name": "Anthropic",
        "sellerProfileId": "prof-anthropic",
        "autoAcceptOffers": True,
    }])

    sns = boto3.client("sns", region_name="us-east-1")
    topic = sns.create_topic(Name="mppo-grants-notifications")

    mock_list.return_value = [
        {
            "offer_id": "offer-trusted",
            "agreement_proposal_id": "prop-1",
            "seller_profile_id": "prof-anthropic",
            "seller_name": "Anthropic",
        },
        {
            "offer_id": "offer-untrusted",
            "agreement_proposal_id": "prop-2",
            "seller_profile_id": "prof-someone-else",
            "seller_name": "Some Other Vendor",
        },
    ]
    mock_accept.return_value = {"agreementId": "agmt-999"}

    with patch.dict(os.environ, {
        "SELLER_TABLE_NAME": "mppo-allowed-sellers",
        "SNS_TOPIC_ARN": topic["TopicArn"],
        "HOME_REGION": "us-east-1",
    }):
        result = lambda_handler({}, None)

    assert result["accepted"] == 1
    assert result["unauthorized"] == 1
    assert mock_accept.call_count == 1
    assert mock_accept.call_args[0][1]["offer_id"] == "offer-trusted"


@mock_aws
@patch("auto_accept_handler._list_private_offers")
@patch("auto_accept_handler._accept_offer")
def test_lambda_handler_rejects_display_name_impersonation(mock_accept, mock_list):
    """A seller presenting a trusted display name is still rejected on profile ID."""
    _seed_table([{
        "proposerAccountId": "444455556666",
        "name": "Anthropic",
        "sellerProfileId": "prof-anthropic",
        "autoAcceptOffers": True,
    }])

    sns = boto3.client("sns", region_name="us-east-1")
    topic = sns.create_topic(Name="mppo-grants-notifications")

    mock_list.return_value = [{
        "offer_id": "offer-impersonator",
        "agreement_proposal_id": "prop-1",
        "seller_profile_id": "prof-impersonator",
        "seller_name": "Anthropic",
    }]

    with patch.dict(os.environ, {
        "SELLER_TABLE_NAME": "mppo-allowed-sellers",
        "SNS_TOPIC_ARN": topic["TopicArn"],
        "HOME_REGION": "us-east-1",
    }):
        result = lambda_handler({}, None)

    assert result["accepted"] == 0
    assert result["unauthorized"] == 1
    assert mock_accept.call_count == 0


@mock_aws
@patch("auto_accept_handler._list_private_offers")
def test_lambda_handler_skips_sellers_without_profile_id(mock_list):
    """A seller opted into auto-accept but missing sellerProfileId fails closed."""
    _seed_table([{
        "proposerAccountId": "444455556666",
        "name": "Anthropic",
        "autoAcceptOffers": True,
    }])

    sns = boto3.client("sns", region_name="us-east-1")
    topic = sns.create_topic(Name="mppo-grants-notifications")

    with patch.dict(os.environ, {
        "SELLER_TABLE_NAME": "mppo-allowed-sellers",
        "SNS_TOPIC_ARN": topic["TopicArn"],
        "HOME_REGION": "us-east-1",
    }):
        result = lambda_handler({}, None)

    assert result["status"] == "no_verifiable_sellers"
    assert result["unconfigured"] == 1
    assert mock_list.call_count == 0


@mock_aws
@patch("auto_accept_handler._list_private_offers")
@patch("auto_accept_handler._accept_offer")
def test_lambda_handler_reports_proposer_mismatch(mock_accept, mock_list):
    """A mismatch between the accepted agreement's proposer and the record alerts."""
    _seed_table([{
        "proposerAccountId": "444455556666",
        "name": "Anthropic",
        "sellerProfileId": "prof-anthropic",
        "autoAcceptOffers": True,
    }])

    sns = boto3.client("sns", region_name="us-east-1")
    topic = sns.create_topic(Name="mppo-grants-notifications")

    mock_list.return_value = [{
        "offer_id": "offer-trusted",
        "agreement_proposal_id": "prop-1",
        "seller_profile_id": "prof-anthropic",
        "seller_name": "Anthropic",
    }]
    mock_accept.return_value = {"agreementId": "agmt-999"}

    with patch.dict(os.environ, {
        "SELLER_TABLE_NAME": "mppo-allowed-sellers",
        "SNS_TOPIC_ARN": topic["TopicArn"],
        "HOME_REGION": "us-east-1",
    }), patch(
        "auto_accept_handler._verify_agreement_proposer",
        return_value="proposer mismatch on agmt-999",
    ):
        result = lambda_handler({}, None)

    assert result["accepted"] == 1
    assert result["errors"] == 1
