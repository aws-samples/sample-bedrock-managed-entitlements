"""Tests for the auto-accept offer handler."""

import os
import sys
from unittest.mock import MagicMock, patch

import boto3
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

from auto_accept_handler import _accept_offer, _list_private_offers, lambda_handler


class FakeDiscoveryClient:
    """Minimal fake for marketplace-discovery used in offer listing/accept tests."""

    def __init__(self, purchase_options, offers, offer_terms):
        self.purchase_options = purchase_options
        self.offers = offers
        self.offer_terms = offer_terms
        self.meta = MagicMock()
        self.meta.region_name = "us-east-1"

    def get_paginator(self, operation_name):
        if operation_name == "list_purchase_options":
            return _StaticPaginator([{"purchaseOptions": self.purchase_options}])
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

    def __init__(self, create_response, accept_response):
        self.create_response = create_response
        self.accept_response = accept_response
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


def test_list_private_offers_resolves_seller_name():
    """Private offers are resolved to seller name via GetOffer."""
    discovery = FakeDiscoveryClient(
        purchase_options=[
            {"purchaseOptionId": "offer-abc123", "purchaseOptionType": "OFFER"},
            {"purchaseOptionId": "offerset-xyz", "purchaseOptionType": "OFFERSET"},
        ],
        offers={
            "offer-abc123": {
                "offerId": "offer-abc123",
                "agreementProposalId": "prop-abc123",
                "sellerOfRecord": {"name": "Anthropic"},
            },
        },
        offer_terms={},
    )

    offers = _list_private_offers(discovery)

    assert offers == [
        {
            "offer_id": "offer-abc123",
            "agreement_proposal_id": "prop-abc123",
            "seller_name": "Anthropic",
        }
    ]


def test_list_private_offers_skips_offer_sets():
    """OFFERSET purchase options are not individually acceptable offers."""
    discovery = FakeDiscoveryClient(
        purchase_options=[
            {"purchaseOptionId": "offerset-xyz", "purchaseOptionType": "OFFERSET"},
        ],
        offers={},
        offer_terms={},
    )

    offers = _list_private_offers(discovery)

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
    offer = {"offer_id": "offer-abc123", "agreement_proposal_id": None, "seller_name": "Anthropic"}

    try:
        _accept_offer(FakeAgreementClient({}, {}), offer)
        assert False, "expected ValueError"
    except ValueError:
        pass


@mock_aws
@patch("auto_accept_handler._list_private_offers")
@patch("auto_accept_handler._accept_offer")
def test_lambda_handler_only_accepts_trusted_sellers(mock_accept, mock_list):
    """Offers from sellers not in the allow-list are skipped."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="mppo-allowed-sellers",
        KeySchema=[{"AttributeName": "proposerAccountId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "proposerAccountId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.put_item(Item={
        "proposerAccountId": "444455556666",
        "name": "Anthropic",
        "autoAcceptOffers": True,
    })

    sns = boto3.client("sns", region_name="us-east-1")
    topic = sns.create_topic(Name="mppo-grants-notifications")

    mock_list.return_value = [
        {"offer_id": "offer-trusted", "agreement_proposal_id": "prop-1", "seller_name": "Anthropic"},
        {"offer_id": "offer-untrusted", "agreement_proposal_id": "prop-2", "seller_name": "Some Other Vendor"},
    ]
    mock_accept.return_value = {"agreementId": "agmt-999"}

    with patch.dict(os.environ, {
        "SELLER_TABLE_NAME": "mppo-allowed-sellers",
        "SNS_TOPIC_ARN": topic["TopicArn"],
        "HOME_REGION": "us-east-1",
    }):
        result = lambda_handler({}, None)

    assert result["accepted"] == 1
    assert mock_accept.call_count == 1
    assert mock_accept.call_args[0][1]["offer_id"] == "offer-trusted"
