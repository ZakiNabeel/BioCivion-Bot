"""
Quantity Reservation System
Replaces the old single-lock approach.

Instead of blocking an entire product for one user, we track
how many units each user has reserved. This allows multiple
customers to shop simultaneously as long as total reservations
don't exceed actual stock.

No Django or database needed — pure Python threading.
Works perfectly for single-server bot deployment.

Usage:
  reserve(item_name, user_id, qty)     → True/False
  release(item_name, user_id)          → releases that user's reservation
  release_all(user_id)                 → releases all reservations for user
  get_available(item_name, stock_qty)  → stock_qty minus all active reservations
  is_fully_reserved(item_name, stock_qty, requested_qty, user_id) → True/False
"""

import threading
import time
import logging

logger = logging.getLogger(__name__)

# { item_name: { user_id: { "qty": int, "expires_at": float } } }
_reservations: dict[str, dict[int, dict]] = {}
_mutex = threading.Lock()

RESERVATION_TIMEOUT = 1800  # 30 minutes max hold (covers payment 20min + buffer)


def reserve(item_name: str, user_id: int, qty: int, stock_qty: int) -> tuple[bool, int]:
    """
    Try to reserve qty units of item_name for user_id.
    Returns (success, available_qty).
    - Replaces any existing reservation this user has for this item.
    - Checks that total reservations by ALL users don't exceed stock_qty.
    """
    with _mutex:
        now = time.time()
        _cleanup_expired(item_name, now)

        item_reservations = _reservations.get(item_name, {})

        reserved_by_others = sum(
            r["qty"] for uid, r in item_reservations.items()
            if uid != user_id and r["expires_at"] > now
        )

        available = stock_qty - reserved_by_others

        if qty > available:
            return False, available

        if item_name not in _reservations:
            _reservations[item_name] = {}

        _reservations[item_name][user_id] = {
            "qty":        qty,
            "expires_at": now + RESERVATION_TIMEOUT,
        }
        logger.info(f"Reserved {qty}x '{item_name}' for user {user_id} ({reserved_by_others} reserved by others, {stock_qty} in stock)")
        return True, available


def release(item_name: str, user_id: int):
    """Release this user's reservation for item_name."""
    with _mutex:
        if item_name in _reservations and user_id in _reservations[item_name]:
            del _reservations[item_name][user_id]
            if not _reservations[item_name]:
                del _reservations[item_name]
            logger.info(f"Released reservation: '{item_name}' by user {user_id}")


def release_all(user_id: int):
    """Release all reservations held by user_id (on cancel/timeout)."""
    with _mutex:
        released = []
        for item_name in list(_reservations.keys()):
            if user_id in _reservations[item_name]:
                del _reservations[item_name][user_id]
                released.append(item_name)
                if not _reservations[item_name]:
                    del _reservations[item_name]
        if released:
            logger.info(f"Released all reservations for user {user_id}: {released}")


def get_available(item_name: str, stock_qty: int) -> int:
    """
    Return how many units are actually available to a new customer.
    = stock_qty minus all active (non-expired) reservations by other users.
    """
    with _mutex:
        now = time.time()
        _cleanup_expired(item_name, now)
        item_reservations = _reservations.get(item_name, {})
        total_reserved    = sum(r["qty"] for r in item_reservations.values() if r["expires_at"] > now)
        return max(0, stock_qty - total_reserved)


def get_user_reserved(item_name: str, user_id: int) -> int:
    """Return how many units user_id currently has reserved for item_name."""
    with _mutex:
        now      = time.time()
        item_res = _reservations.get(item_name, {})
        user_res = item_res.get(user_id)
        if user_res and user_res["expires_at"] > now:
            return user_res["qty"]
        return 0


def _cleanup_expired(item_name: str, now: float):
    """Remove expired reservations for item_name. Must be called inside _mutex."""
    if item_name not in _reservations:
        return
    expired = [uid for uid, r in _reservations[item_name].items() if r["expires_at"] <= now]
    for uid in expired:
        del _reservations[item_name][uid]
        logger.info(f"Expired reservation: '{item_name}' by user {uid}")
    if not _reservations[item_name]:
        del _reservations[item_name]


# Legacy aliases so old imports don't break during transition
def acquire_lock(item_name: str, user_id: int) -> bool:
    return True

def release_lock(item_name: str, user_id: int):
    release(item_name, user_id)

def release_all_locks(user_id: int):
    release_all(user_id)

def is_locked(item_name: str, user_id: int = None) -> bool:
    return False
