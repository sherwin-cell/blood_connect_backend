"""POST /api/admin/requests/{request_id}/prc-stock"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from firebase_admin import firestore
from pydantic import BaseModel, Field

from auth import verify_firebase_claims

from .service import Notice, PrcStockService
from .store import FirestorePrcStockStore, outbox_notifier

router = APIRouter(prefix="/api/admin/requests", tags=["PRC stock"])

_service: Optional[PrcStockService] = None


class PrcStockNotice(BaseModel):
    reservedUnits: int = Field(..., description="Units PRC reserved (1 to the units requested).")
    pickupInstructions: str = Field(..., description="Where and how to claim the blood at PRC Sorsogon.")
    chatMessage: Optional[str] = Field(
        None, description="Also post this in the requester's PRC Support chat.")


def build_service(db) -> PrcStockService:
    return PrcStockService(FirestorePrcStockStore(db), outbox_notifier(db),
                           timestamp=firestore.SERVER_TIMESTAMP)


def configure(service: Optional[PrcStockService]) -> None:
    global _service
    _service = service


def get_service() -> PrcStockService:
    if _service is None:
        raise HTTPException(status_code=503, detail="PRC stock notices are not available.")
    return _service


@router.post("/{request_id}/prc-stock")
def notify_prc_stock(
    request_id: str,
    body: PrcStockNotice,
    claims: dict = Depends(verify_firebase_claims),
    service: PrcStockService = Depends(get_service),
):
    """PRC reserved blood for this request: record it and notify the requester.

    Records prcStock / prcStockNotices on the request (a pending request is
    approved in the same step), writes a prc_stock_available notification that
    the worker pushes by FCM, and optionally posts in the PRC Support chat.
    """
    outcome = service.notify(claims, request_id, Notice(
        body.reservedUnits, body.pickupInstructions, body.chatMessage))
    return JSONResponse(status_code=outcome.status, content=outcome.json())
