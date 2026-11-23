"""Firebase ID token authentication for API endpoints."""

from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth

# auto_error=False so a missing header is a 401 (not FastAPI's default), the
# same status as a bad token.
_bearer = HTTPBearer(auto_error=False)


def verify_firebase_token(
    credentials_obj: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    """Verify the request's Firebase ID token and return the caller's UID."""
    if credentials_obj is None or not credentials_obj.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Firebase authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        decoded_token = auth.verify_id_token(credentials_obj.credentials)
        return decoded_token["uid"]
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Firebase authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def verify_firebase_claims(
    credentials_obj: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    """Like verify_firebase_token, but returns all decoded claims (uid,
    auth_time, ...) for endpoints that need more than the UID."""
    if credentials_obj is None or not credentials_obj.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Firebase authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return auth.verify_id_token(credentials_obj.credentials)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Firebase authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
