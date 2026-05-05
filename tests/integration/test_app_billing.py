"""Integration tests for the Stripe billing endpoints."""
import json

import pytest

pytestmark = pytest.mark.integration


def _has_stripe_sdk() -> bool:
    try:
        import stripe  # noqa: F401
        return True
    except ImportError:
        return False


SKIP_NO_STRIPE = pytest.mark.skipif(
    not _has_stripe_sdk(), reason="stripe SDK not installed"
)


# ── Webhook (does not require auth) ─────────────────────────────────────────


class TestStripeWebhook:
    @SKIP_NO_STRIPE
    def test_invalid_signature_400(self, fastapi_client, monkeypatch):
        client, _, _ = fastapi_client
        # The webhook is unauthenticated; verification should fail with 400
        # for a bad signature.
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_real")
        body = json.dumps({"foo": "bar"}).encode()
        r = client.post(
            "/api/webhooks/stripe", content=body,
            headers={"stripe-signature": "t=1,v1=deadbeef",
                     "content-type": "application/json"},
        )
        assert r.status_code == 400


# ── Checkout / Portal (auth-gated) ──────────────────────────────────────────


class TestBillingAuth:
    def test_checkout_requires_auth(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        r = client.post("/api/billing/checkout", json={})
        assert r.status_code == 401

    def test_portal_requires_auth(self, fastapi_client):
        client, _, _ = fastapi_client
        client.cookies.clear()
        r = client.post("/api/billing/portal", json={})
        assert r.status_code == 401


class TestBillingNotConfigured:
    def test_checkout_returns_503_when_unconfigured(self, fastapi_client, monkeypatch):
        # Force the billing module to report not-configured.
        from pipeline import stripe_billing
        monkeypatch.setattr(stripe_billing, "is_configured", lambda: False)
        client, _, _ = fastapi_client
        r = client.post("/api/billing/checkout", json={})
        # 503 Service Unavailable is the documented response when billing
        # has no key configured — see docstring on stripe_billing.is_configured.
        assert r.status_code == 503
