"""
Payment handler — supports Square, Stripe, and Zelle.

Square:
  - Creates a Square Payment Link and returns its URL + link_id for later cancellation.
  - Set SQUARE_ACCESS_TOKEN and SQUARE_LOCATION_ID in config.py.
  - Set SQUARE_ENVIRONMENT = "sandbox" for testing, "production" for live.

Stripe:
  - Creates a Stripe Checkout Session and returns its URL + session_id for later expiry.
  - Set STRIPE_SECRET_KEY in config.py (sk_test_... for test, sk_live_... for live).

Zelle:
  - No API. Bot sends Zelle business tag ID + instructions.
  - Set ZELLE_BUSINESS_TAG, ZELLE_BUSINESS_NAME in config.py.
"""
import time
import logging
from config import (
    SQUARE_ACCESS_TOKEN,
    SQUARE_LOCATION_ID,
    SQUARE_ENVIRONMENT,
    STRIPE_SECRET_KEY,
    ZELLE_BUSINESS_TAG,
    ZELLE_BUSINESS_NAME,
)

logger = logging.getLogger(__name__)

SHIPPING = 20.00


# ── Square ─────────────────────────────────────────────────────────────────────

def create_square_link(order_id: str, cart: list[dict], subtotal: float) -> dict:
    """
    Create a Square Payment Link.
    Returns {"url": str, "link_id": str} on success, or {"url": None, "link_id": None} on failure.
    """
    try:
        import requests, uuid

        # Combine everything into one total price
        total_cents = int(round((subtotal + SHIPPING) * 100))

        base_url = (
            "https://connect.squareupsandbox.com"
            if SQUARE_ENVIRONMENT == "sandbox"
            else "https://connect.squareup.com"
        )

        headers = {
            "Authorization": f"Bearer {SQUARE_ACCESS_TOKEN}",
            "Content-Type":  "application/json",
            "Square-Version": "2024-01-18",
        }

        # Single line item as requested
        line_items = [{
            "name": "Professional Tools & Resource Access",
            "quantity": "1",
            "base_price_money": {
                "amount": total_cents,
                "currency": "USD",
            },
        }]

        payload = {
            "idempotency_key": str(uuid.uuid4()),
            "order": {
                "location_id": SQUARE_LOCATION_ID,
                "line_items":  line_items,
                "metadata":    {"order_id": order_id},
            },
            "checkout_options": {},
            "pre_populated_data": {},
        }

        resp = requests.post(
            f"{base_url}/v2/online-checkout/payment-links",
            json=payload,
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        link_id = data["payment_link"]["id"]
        url     = data["payment_link"]["url"]
        logger.info(f"Square link created: {link_id} for order {order_id}")
        return {"url": url, "link_id": link_id}

    except Exception as e:
        logger.error(f"Square API error for order {order_id}: {e}")
        logger.error(f"Square response body: {getattr(e.response, 'text', 'N/A')}")
        return {"url": None, "link_id": None}


def cancel_square_link(link_id: str) -> bool:
    """
    Delete/deactivate a Square Payment Link by its link_id.
    Returns True on success.
    """
    if not link_id:
        return False
    try:
        import requests

        base_url = (
            "https://connect.squareupsandbox.com"
            if SQUARE_ENVIRONMENT == "sandbox"
            else "https://connect.squareup.com"
        )

        headers = {
            "Authorization": f"Bearer {SQUARE_ACCESS_TOKEN}",
            "Content-Type":  "application/json",
            "Square-Version": "2024-01-18",
        }

        resp = requests.delete(
            f"{base_url}/v2/online-checkout/payment-links/{link_id}",
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(f"Square link {link_id} deleted successfully.")
        return True

    except Exception as e:
        logger.error(f"Failed to cancel Square link {link_id}: {e}")
        return False


# ── Stripe ─────────────────────────────────────────────────────────────────────

def create_stripe_session(order_id: str, cart: list[dict], subtotal: float) -> dict:
    """
    Create a Stripe Checkout Session.
    Returns {"url": str, "session_id": str} on success, or {"url": None, "session_id": None} on failure.
    """
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY

        # Combine everything into one total price
        total_cents = int(round((subtotal + SHIPPING) * 100))

        # Single line item as requested
        line_items = [{
            "price_data": {
                "currency":     "usd",
                "unit_amount":  total_cents,
                "product_data": {"name": "Professional Tools & Resource Access"},
            },
            "quantity": 1,
        }]

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=line_items,
            mode="payment",
            success_url="https://t.me/BioCivion_Bot",  
            cancel_url="https://t.me/BioCivion_Bot",            
            metadata={"order_id": order_id},
        )

        logger.info(f"Stripe session created: {session.id} for order {order_id}")
        return {"url": session.url, "session_id": session.id}

    except Exception as e:
        logger.error(f"Stripe API error for order {order_id}: {e}")
        return {"url": None, "session_id": None}


def cancel_stripe_session(session_id: str) -> bool:
    """
    Expire a Stripe Checkout Session by its session_id.
    Returns True on success.
    """
    if not session_id:
        return False
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY

        stripe.checkout.Session.expire(session_id)
        logger.info(f"Stripe session {session_id} expired successfully.")
        return True

    except Exception as e:
        logger.error(f"Failed to expire Stripe session {session_id}: {e}")
        return False


# ── Zelle ──────────────────────────────────────────────────────────────────────

def get_zelle_message(order_id: str, customer: dict, cart: list[dict], subtotal: float) -> str:
    total    = subtotal + SHIPPING
    apt_line = f", Unit {customer['apt']}" if customer.get("apt") else ""
    address  = f"{customer['street']}{apt_line}, {customer['city']}, {customer['state']}"

    return (
        f"💳 *Zelle Payment Information*\n\n"
        f"Select Business option. Use Zelle business tag ID: *{ZELLE_BUSINESS_TAG}*. "
        f"Please send payment to: *{ZELLE_BUSINESS_TAG}*. "
        f"It will show business name as *{ZELLE_BUSINESS_NAME}*.\n\n"
        f"*Order ID:* {order_id}\n"
        f"*Full Name:* {customer['first_name']} {customer['last_name']}\n"
        f"*Your Address:* {address}\n\n"
        f"💵 *Total Due: ${total:.2f}* (incl. $20 shipping)\n\n"
        f"⏳ *Action Required:*\n"
        f"Please click *'✅ Payment Complete'* after making Zelle payment within *20 minutes* to secure your order.\n"
        f"Do not put anything in the notes/memo section. Any notes or emojis will result in *immediate* order cancellation."
        f"All transactions are verified before shipping.\n"
    )
