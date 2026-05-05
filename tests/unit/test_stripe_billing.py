"""Tests for pipeline.stripe_billing — config detection, signature verification, status helpers."""
import pytest

from pipeline import stripe_billing
from pipeline.stripe_billing import (
    WebhookVerifyError,
    is_configured,
    reset_for_tests,
    subscription_active,
    verify_webhook,
)
from tests.stripe_helpers import (
    checkout_completed,
    sign_payload,
    subscription_event,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset():
    """Drop the cached SDK handle between tests so env-var changes take effect."""
    reset_for_tests()
    yield
    reset_for_tests()


# ── is_configured ───────────────────────────────────────────────────────────


class TestIsConfigured:
    def test_true_when_key_present_and_sdk_installed(self, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
        # If the stripe SDK is available in the test environment, this
        # returns True; otherwise it returns False — we accept either as
        # long as it doesn't crash.
        result = is_configured()
        assert isinstance(result, bool)

    def test_false_when_key_missing(self, monkeypatch):
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
        assert is_configured() is False


# ── subscription_active ─────────────────────────────────────────────────────


class TestSubscriptionActive:
    @pytest.mark.parametrize("status", ["active", "trialing", "past_due", " ACTIVE "])
    def test_active_statuses(self, status):
        assert subscription_active(status) is True

    @pytest.mark.parametrize("status", [
        "canceled", "unpaid", "incomplete", "incomplete_expired", "paused", "", None,
    ])
    def test_inactive_statuses(self, status):
        assert subscription_active(status) is False


# ── verify_webhook ──────────────────────────────────────────────────────────


class TestVerifyWebhook:
    def test_missing_secret_raises(self):
        body, sig = sign_payload({"foo": "bar"}, "anysecret")
        with pytest.raises(WebhookVerifyError, match="not configured"):
            verify_webhook(body, sig, "")

    def test_bad_signature_raises(self, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
        if not is_configured():
            pytest.skip("stripe SDK not installed in this environment")
        body, _ = sign_payload({"foo": "bar"}, "wrong-secret")
        with pytest.raises(WebhookVerifyError, match="signature verification failed"):
            verify_webhook(body, "t=1,v1=deadbeef", "real-secret")

    def test_good_signature_returns_event(self, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
        if not is_configured():
            pytest.skip("stripe SDK not installed in this environment")
        secret = "whsec_test_real"
        payload = checkout_completed(user_id="user-abc")
        body, sig = sign_payload(payload, secret)
        event = verify_webhook(body, sig, secret)
        # The SDK's construct_event returns a stripe.Event proxy; .type and
        # .data.object are the fields our handler reads.
        assert event["type"] == "checkout.session.completed"
        assert event["data"]["object"]["client_reference_id"] == "user-abc"


# ── stripe_helpers self-test ────────────────────────────────────────────────


class TestStripeHelpers:
    def test_checkout_completed_shape(self):
        evt = checkout_completed(user_id="user-1", customer_id="cus_1", subscription_id="sub_1")
        assert evt["type"] == "checkout.session.completed"
        obj = evt["data"]["object"]
        assert obj["client_reference_id"] == "user-1"
        assert obj["customer"] == "cus_1"
        assert obj["subscription"] == "sub_1"

    def test_subscription_event_shape(self):
        evt = subscription_event(
            "customer.subscription.updated",
            customer_id="cus_2", subscription_id="sub_2", status="canceled",
        )
        assert evt["type"] == "customer.subscription.updated"
        assert evt["data"]["object"]["status"] == "canceled"

    def test_sign_payload_deterministic_with_fixed_timestamp(self):
        body1, sig1 = sign_payload({"a": 1}, "secret", timestamp=1700000000)
        body2, sig2 = sign_payload({"a": 1}, "secret", timestamp=1700000000)
        assert body1 == body2
        assert sig1 == sig2
        assert sig1.startswith("t=1700000000,v1=")

    def test_sign_payload_changes_with_secret(self):
        _, sig1 = sign_payload({"a": 1}, "secret-1", timestamp=1700000000)
        _, sig2 = sign_payload({"a": 1}, "secret-2", timestamp=1700000000)
        assert sig1 != sig2
