"""
tests/stripe_helpers.py
───────────────────────
Hand-rolled Stripe webhook signing helper. Avoids requiring the ``stripe``
SDK in unit tests — and even when it's installed, hand-rolling the HMAC
gives us a deterministic, dependency-free contract test for our webhook
verification path.

The Stripe-Signature header format is:
    t=<unix_timestamp>,v1=<hex_hmac>

Where the signed string is ``f"{timestamp}.{payload_bytes}"`` and the HMAC
uses ``secret`` (the dashboard / `stripe listen` webhook signing secret),
SHA-256, hex-encoded.

This matches what stripe.Webhook.construct_event verifies, byte-for-byte.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any


def sign_payload(payload: dict[str, Any], secret: str,
                 timestamp: int | None = None) -> tuple[bytes, str]:
    """Return ``(body_bytes, sig_header)`` for a Stripe-style webhook test.

    *payload* is JSON-encoded into bytes (the raw bytes Stripe would have
    sent; ``construct_event`` re-verifies against these exact bytes).
    *timestamp* defaults to "now" but a fixed value is recommended in
    tests for reproducibility.
    """
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ts = int(timestamp if timestamp is not None else time.time())
    signed_string = f"{ts}.".encode("utf-8") + body
    digest = hmac.new(
        secret.encode("utf-8"), signed_string, hashlib.sha256
    ).hexdigest()
    header = f"t={ts},v1={digest}"
    return body, header


def make_event(event_type: str, data_object: dict, *,
               event_id: str = "evt_test_1", livemode: bool = False) -> dict:
    """Build a minimal Stripe ``Event`` envelope around *data_object*.

    Matches the shape Stripe.Event.construct_event returns and the field
    set our webhook handler actually reads — id, type, data.object — plus
    the boilerplate keys the SDK validates the structure of.
    """
    return {
        "id": event_id,
        "object": "event",
        "api_version": "2024-06-20",
        "created": 1_700_000_000,
        "data": {"object": data_object},
        "livemode": livemode,
        "pending_webhooks": 0,
        "request": {"id": None, "idempotency_key": None},
        "type": event_type,
    }


# ── Common event payloads tests reach for ───────────────────────────────────


def checkout_completed(*, user_id: str, customer_id: str = "cus_test_1",
                       subscription_id: str = "sub_test_1") -> dict:
    """Stripe ``checkout.session.completed`` event for *user_id*."""
    return make_event(
        "checkout.session.completed",
        {
            "id": "cs_test_1",
            "object": "checkout.session",
            "client_reference_id": user_id,
            "customer": customer_id,
            "subscription": subscription_id,
            "mode": "subscription",
            "status": "complete",
        },
    )


def subscription_event(event_type: str, *, customer_id: str = "cus_test_1",
                        subscription_id: str = "sub_test_1",
                        status: str = "active",
                        user_id: str | None = None) -> dict:
    """One of customer.subscription.{created,updated,deleted}."""
    obj = {
        "id": subscription_id,
        "object": "subscription",
        "customer": customer_id,
        "status": status,
        "metadata": {"user_id": user_id} if user_id else {},
    }
    return make_event(event_type, obj)
