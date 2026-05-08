#!/usr/bin/env python3
"""
scripts/setup_stripe.py
───────────────────────
One-shot bootstrap that creates the Pro Product + recurring Price in your
Stripe account, then prints the IDs to copy into ``.env``.

Run once after creating a Stripe account:

    pip install stripe
    export STRIPE_SECRET_KEY=sk_test_...        # or sk_live_...
    python scripts/setup_stripe.py

The script is idempotent in the human sense — re-running it asks before
creating duplicates if a "Jobs AI Pro" product already exists. Stripe
itself never deduplicates products by name, so the check is ours.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


PRODUCT_NAME = "Jobs AI Pro"
PRODUCT_DESCRIPTION = (
    "Unlock the high-quality cloud Ollama models in Jobs AI — frontier-class "
    "scoring, tailoring, and résumé critique, hosted by us, no API keys needed. "
    "Anthropic Claude is in active development and will be included when it ships."
)
PRICE_AMOUNT_CENTS = 400          # $4.00
PRICE_CURRENCY = "usd"
PRICE_INTERVAL = "month"          # 'month' or 'year'
PRICE_LOOKUP_KEY = "jobs_ai_pro_monthly"   # so you can reference it elsewhere


def _load_env() -> None:
    """Load .env if python-dotenv is around — same convention as app.py."""
    try:
        from dotenv import load_dotenv, find_dotenv
        env = find_dotenv(filename=".env", usecwd=False)
        if env:
            load_dotenv(env, override=False)
    except ImportError:
        pass


def _confirm(prompt: str) -> bool:
    ans = input(f"{prompt} [y/N]: ").strip().lower()
    return ans in ("y", "yes")


def main() -> int:
    _load_env()
    key = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
    if not key:
        print("ERROR: STRIPE_SECRET_KEY is not set.", file=sys.stderr)
        print("       Set it in your .env or environment, then re-run.", file=sys.stderr)
        return 2

    try:
        import stripe
    except ImportError:
        print("ERROR: stripe package is not installed. Run: pip install 'stripe>=8.0.0'",
              file=sys.stderr)
        return 2

    stripe.api_key = key
    stripe.api_version = "2024-06-20"

    is_live = key.startswith("sk_live_")
    print(f"Using {'LIVE' if is_live else 'TEST'} mode key ({key[:7]}…)")
    if is_live and not _confirm("This will create real billing entities. Proceed?"):
        return 1

    # ── Product ──────────────────────────────────────────────────────────────
    existing = list(stripe.Product.list(limit=100, active=True).auto_paging_iter())
    matches = [p for p in existing if (p.get("name") or "").strip() == PRODUCT_NAME]
    product = None
    if matches:
        print(f"Found {len(matches)} existing product(s) named {PRODUCT_NAME!r}:")
        for p in matches:
            print(f"  • {p['id']}  created={p.get('created')}")
        if _confirm(f"Reuse existing product {matches[0]['id']}?"):
            product = matches[0]
    if product is None:
        print(f"Creating new product {PRODUCT_NAME!r}…")
        product = stripe.Product.create(
            name=PRODUCT_NAME,
            description=PRODUCT_DESCRIPTION,
        )
        print(f"  → product.id = {product['id']}")

    # ── Price ────────────────────────────────────────────────────────────────
    # Lookup keys are unique per account, so reusing one is the right call.
    price = None
    try:
        existing_price_list = stripe.Price.list(
            lookup_keys=[PRICE_LOOKUP_KEY], expand=[], limit=10,
        )
        existing_prices = list(existing_price_list.auto_paging_iter())
    except Exception:
        existing_prices = []

    if existing_prices:
        price = existing_prices[0]
        print(f"Reusing existing price with lookup_key={PRICE_LOOKUP_KEY!r}: {price['id']} "
              f"(${price['unit_amount']/100:.2f}/{price['recurring']['interval']})")
    else:
        print(f"Creating recurring price ${PRICE_AMOUNT_CENTS/100:.2f}/{PRICE_INTERVAL}…")
        price = stripe.Price.create(
            product=product["id"],
            unit_amount=PRICE_AMOUNT_CENTS,
            currency=PRICE_CURRENCY,
            recurring={"interval": PRICE_INTERVAL},
            lookup_key=PRICE_LOOKUP_KEY,
            nickname=f"Jobs AI Pro — ${PRICE_AMOUNT_CENTS/100:.2f}/{PRICE_INTERVAL}",
        )
        print(f"  → price.id = {price['id']}")

    # ── Output env block ─────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("Add these lines to your .env (or update the existing ones):")
    print("=" * 60)
    print(f"STRIPE_PRICE_ID_PRO_MONTHLY={price['id']}")
    print()
    print("Then to wire up the webhook locally:")
    print("  stripe login")
    print("  stripe listen --forward-to http://localhost:8000/api/webhooks/stripe")
    print("  # copy the whsec_... it prints into STRIPE_WEBHOOK_SECRET")
    print()
    print("For production at https://lark.tailaa3a85.ts.net (or your final host):")
    print("  1. Stripe dashboard → Developers → Webhooks → Add endpoint")
    print("  2. URL: https://YOUR-HOST/api/webhooks/stripe")
    print("  3. Events: checkout.session.completed, customer.subscription.created,")
    print("            customer.subscription.updated, customer.subscription.deleted")
    print("  4. Copy the signing secret into STRIPE_WEBHOOK_SECRET")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
