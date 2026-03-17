"""
Stripe billing integration.

Plans:
  starter  – 500  emails/day  – $299/month
  pro      – 1000 emails/day  – $599/month
  agency   – unlimited        – $1499/month
"""
import os
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

PLANS: Dict[str, Dict[str, Any]] = {
    "starter": {
        "price_id": os.getenv("STRIPE_STARTER_PRICE_ID", ""),
        "daily_limit": 500,
        "monthly_price": 299.0,
        "display": "$299/month – 500 emails/day",
    },
    "pro": {
        "price_id": os.getenv("STRIPE_PRO_PRICE_ID", ""),
        "daily_limit": 1000,
        "monthly_price": 599.0,
        "display": "$599/month – 1,000 emails/day",
    },
    "agency": {
        "price_id": os.getenv("STRIPE_AGENCY_PRICE_ID", ""),
        "daily_limit": 100_000,
        "monthly_price": 1499.0,
        "display": "$1,499/month – unlimited emails/day",
    },
}


def _get_stripe():
    """Lazy-import stripe and configure API key."""
    import stripe  # imported lazily so the app starts without Stripe installed
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    return stripe


def create_checkout_session(
    user_email: str,
    plan: str,
    success_url: str,
    cancel_url: str,
) -> Dict[str, str]:
    """
    Create a Stripe Checkout Session for the given plan.
    Returns {"checkout_url": "...", "session_id": "..."}.
    """
    if plan not in PLANS:
        raise ValueError(f"Unknown plan '{plan}'. Choose from: {list(PLANS)}")

    price_id = PLANS[plan]["price_id"]
    if not price_id:
        raise RuntimeError(f"STRIPE_{plan.upper()}_PRICE_ID env var not configured")

    stripe = _get_stripe()
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=user_email,
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"plan": plan},
    )
    return {"checkout_url": session.url, "session_id": session.id}


def handle_webhook(payload: bytes, sig_header: str) -> Dict[str, Any]:
    """
    Validate and parse a Stripe webhook event.
    Returns the event dict on success; raises on failure.
    """
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if not webhook_secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET not configured")

    stripe = _get_stripe()
    event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    return event


def cancel_subscription(stripe_subscription_id: str) -> bool:
    """Cancel a Stripe subscription immediately."""
    stripe = _get_stripe()
    sub = stripe.Subscription.cancel(stripe_subscription_id)
    return sub.status == "canceled"
