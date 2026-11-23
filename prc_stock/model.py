"""PRC stock fields on blood_requests/{id} (no Firestore access; also used by
the notification worker).

    prcStock: {                    latest notice
        reservedUnits, pickupInstructions, notifiedAt, notifiedBy,
        noticeCount, chatMessageSent
    }
    prcStockNotices: [             every notice, oldest first (admin timeline)
        {reservedUnits, pickupInstructions, notifiedAt, notifiedBy, approvedRequest}
    ]

Both are written by the backend only; firestore.rules keep requesters from
changing them. The status keeps its usual values: a pending request becomes
"active" (approved) when PRC reserves stock for it.
"""

from __future__ import annotations

from typing import Any

PENDING = "pending"
OPEN = ("approved", "active", "matched")
LEGACY_ARCHIVED = "archived"


def norm(value: Any) -> str:
    return str(value or "").strip().lower()


def status_of(request: dict) -> str:
    return norm(request.get("status")) or PENDING


def units_needed(request: dict) -> int:
    """Same as unitsNeededOf in the admin portal."""
    try:
        value = int(request.get("unitsRequired") or request.get("unitsNeeded") or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, value)


def reserved_units(request: dict) -> int:
    stock = request.get("prcStock")
    if not isinstance(stock, dict):
        return 0
    try:
        return max(0, int(stock.get("reservedUnits") or 0))
    except (TypeError, ValueError):
        return 0


def covered_by_prc_stock(request: dict) -> bool:
    """PRC reserved every unit, so no donors need to be alerted."""
    return reserved_units(request) >= units_needed(request)


def is_archived(request: dict) -> bool:
    return request.get("archived") is True or status_of(request) == LEGACY_ARCHIVED
