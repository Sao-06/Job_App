"""
pipeline/stripe_billing.py
──────────────────────────
Thin wrapper around the Stripe SDK so app.py can stay focused on routing.

The module exposes:

* ``is_configured()``         — both SDK installed AND STRIPE_SECRET_KEY set
* ``ensure_customer(...)``    — idempotent get-or-create Customer for a user
* ``create_checkout_session(...)``
* ``create_portal_session(...)``
* ``verify_webhook(...)``     — signature-verified ``stripe.Event``
* ``subscription_active(status)``

Loaded lazily so a missing ``stripe`` package or absent secret key don't
crash the rest of the app at import time — billing endpoints just
respond 503 ``Billing is not configured``.
"""

from __future__ import annotations

import os
from typing import Any

# ── Lazy SDK access ───────────────────────────────────────────────────────────
# We import inside helpers rather than at module top so a fresh checkout
# without the `stripe` package can still boot the rest of app.py. When
# present, we keep a single configured handle so callers don't re-key per
# request (Stripe's SDK uses a module-level api_key).

_stripe_module: Any = None
_configured: bool = False


def _load_stripe():
    """Import + configure the SDK once. Raises RuntimeError when unavailable."""
    global _stripe_module, _configured
    if _configured and _stripe_module is not None:
        return _stripe_module
    try:
        import stripe as _stripe
    except ImportError as exc:
        raise RuntimeError(
            "stripe package is not installed — run: pip install 'stripe>=8.0.0'"
        ) from exc
    key = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "STRIPE_SECRET_KEY is not set — copy .env.example and fill it in."
        )
    _stripe.api_key = key
    # Pin a recent stable API version so dashboard upgrades don't silently
    # change the shape of objects we're parsing in webhook handlers. Update
    # this deliberately when migrating, not by accident.
    _stripe.api_version = "2024-06-20"
    _stripe_module = _stripe
    _configured = True
    return _stripe


def is_configured() -> bool:
    """True iff the SDK is importable AND a key is set. Safe to call anywhere."""
    try:
        _load_stripe()
        return True
    except RuntimeError:
        return False


def reset_for_tests() -> None:
    """Drop the cached SDK handle so a test can re-load with new env."""
    global _stripe_module, _configured
    _stripe_module = None
    _configured = False


# ── Customers ─────────────────────────────────────────────────────────────────

def ensure_customer(user_store, user: dict) -> str:
    """Idempotent: return the persisted ``stripe_customer_id`` for *user*,
    creating a new Stripe Customer (and persisting the id) when absent.

    *user_store* is the SQLiteSessionStore instance. *user* is the dict
    produced by ``get_user_by_id`` / ``get_user_by_email`` — must carry
    at minimum ``id`` and ``email``.
    """
    cid = (user.get("stripe_customer_id") or "").strip()
    if cid:
        return cid
    s = _load_stripe()
    customer = s.Customer.create(
        email=user.get("email") or None,
        metadata={"user_id": user["id"]},
    )
    cid = customer["id"]
    user_store.set_user_stripe_customer(user["id"], cid)
    return cid


# ── Checkout / Portal ─────────────────────────────────────────────────────────

def create_checkout_session(*, customer_id: str, client_reference_id: str,
                            price_id: str, success_url: str, cancel_url: str) -> dict:
    """Subscription-mode Checkout. Both ``customer`` and
    ``client_reference_id`` are passed so the webhook has two independent
    routes back to the user (customer is on every later event;
    client_reference_id only on the initial checkout).
    """
    s = _load_stripe()
    session = s.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        client_reference_id=client_reference_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        # Surface the user_id to the subscription itself so a future
        # `customer.subscription.*` event without the customer pre-loaded
        # can still resolve back to the user.
        subscription_data={"metadata": {"user_id": client_reference_id}},
        allow_promotion_codes=True,
    )
    return {"id": session["id"], "url": session["url"]}


def create_portal_session(*, customer_id: str, return_url: str) -> dict:
    """Stripe-hosted Customer Portal — lets users update payment method,
    cancel, view invoices, etc. without us building a UI."""
    s = _load_stripe()
    session = s.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )
    return {"id": session["id"], "url": session["url"]}


# ── Webhook ───────────────────────────────────────────────────────────────────

class WebhookVerifyError(RuntimeError):
    """Wraps a signature-verification failure so the route can return 400
    without leaking whether the SDK is missing or the secret is wrong."""


def verify_webhook(payload: bytes, sig_header: str, secret: str):
    """Return the verified ``stripe.Event``. Raises WebhookVerifyError on
    bad signature, missing secret, or SDK absence.

    *payload* MUST be the raw bytes from the request body — passing a
    re-encoded JSON string will fail signature verification because
    Stripe signs the exact bytes it sent.
    """
    if not secret:
        raise WebhookVerifyError("webhook secret is not configured")
    try:
        s = _load_stripe()
    except RuntimeError as exc:
        raise WebhookVerifyError(str(exc)) from exc
    try:
        return s.Webhook.construct_event(
            payload=payload, sig_header=sig_header or "", secret=secret
        )
    except Exception as exc:
        # Includes stripe.error.SignatureVerificationError + ValueError on
        # bad payload. Don't leak which one — both mean "reject the event".
        raise WebhookVerifyError(f"signature verification failed: {exc}") from exc


# ── Status helpers ────────────────────────────────────────────────────────────

# Statuses where we treat the user as Pro. Stripe's full enum:
#   incomplete, incomplete_expired, trialing, active, past_due, canceled, unpaid, paused
# We deliberately keep ``past_due`` here so a brief failed-payment retry
# doesn't bounce a paying user; if Stripe gives up dunning the subscription
# moves to ``canceled`` and we downgrade then.
_ACTIVE_SUB_STATUSES = frozenset({"active", "trialing", "past_due"})


def subscription_active(status: str | None) -> bool:
    return (status or "").strip().lower() in _ACTIVE_SUB_STATUSES
