
import os
import json
import logging
from contextlib import asynccontextmanager

import firebase_admin

from firebase_admin import credentials, firestore
from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Import donor notification router
from routers.donor_notifications import router as donor_notifications_router
from notifications.config import Settings
from notifications.worker import NotificationWorker
from jobs.request_expiration import ExpirationSettings, RequestExpirer
from verification import router as face_verification
from account import router as account_router
from files import router as files_router

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("blood_connect")


# ------------------------------------------------------------------------------
# 1. Firebase Admin SDK Initialization
# ------------------------------------------------------------------------------

service_account_env = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")

if service_account_env:
    cert_dict = json.loads(service_account_env)
    cred = credentials.Certificate(cert_dict)

elif os.path.exists("serviceAccountKey.json"):
    cred = credentials.Certificate("serviceAccountKey.json")

else:
    cred = None


if cred and not firebase_admin._apps:
    firebase_admin.initialize_app(cred)


db = firestore.client() if cred else None


# ------------------------------------------------------------------------------
# 2. Notification worker (Firestore listeners -> notifications -> FCM)
# ------------------------------------------------------------------------------

notification_worker = None
request_expirer = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global notification_worker, request_expirer

    settings = Settings.from_env()
    if settings.enabled and db is not None:
        notification_worker = NotificationWorker(db, settings)
        notification_worker.start()
    elif settings.enabled:
        log.warning("Firebase credentials missing; notification worker not started.")

    # Marks open blood requests past their needed-by date as expired.
    expiration_settings = ExpirationSettings.from_env()
    if expiration_settings.enabled and db is not None:
        request_expirer = RequestExpirer(db, expiration_settings)
        request_expirer.start()
    elif expiration_settings.enabled:
        log.warning("Firebase credentials missing; request expiration not started.")

    # Face matching for POST /api/verification/face.
    if db is not None:
        face_verification.configure(face_verification.build_service(db))
        account_router.configure(account_router.build_service(db))
        files_router.configure(files_router.build_service(db))
    else:
        log.warning("Firebase credentials missing; face verification and "
                    "account deletion disabled.")

    yield

    face_verification.configure(None)
    account_router.configure(None)
    files_router.configure(None)
    if request_expirer is not None:
        request_expirer.stop()
    if notification_worker is not None:
        notification_worker.stop()


# ------------------------------------------------------------------------------
# 3. FastAPI Setup
# ------------------------------------------------------------------------------

app = FastAPI(
    lifespan=lifespan,
    title="Blood-Connect API",
    description=(
        "Backend API for Blood-Connect. Also runs the notification worker, "
        "which turns Firestore events into in-app notifications and FCM pushes."
    ),
    version="2.2.0"
)

# The PRC admin portal (blood-connect-admin) calls the API from the browser.
# Comma-separated origins; set ADMIN_CORS_ORIGINS to the deployed portal URL.
# The mobile app is not a browser and needs no CORS.
admin_origins = [
    origin.strip().rstrip("/")
    for origin in os.getenv(
        "ADMIN_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=admin_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# ------------------------------------------------------------------------------
# 4. Register API Routers
# ------------------------------------------------------------------------------

# Deprecated: the notification worker now sends donor approval/rejection
# notifications automatically. Kept so older admin builds do not error.
# POST /api/donors/notify-status
app.include_router(donor_notifications_router)

# POST /api/verification/face (Firebase ID token required)
app.include_router(face_verification.router)

# DELETE /api/account (Firebase ID token from a recent sign-in required)
# DELETE /api/admin/users/{uid} (PRC admin's Firebase ID token required)
app.include_router(account_router.router)

# POST /api/files/{id_front|id_back|brf}, GET /api/files/view (Firebase ID token)
app.include_router(files_router.router)


# ------------------------------------------------------------------------------
# 4b. Health Check Endpoints
# ------------------------------------------------------------------------------

@app.get("/health", tags=["System"])
def health_check():
    return {
        "status": "ok",
        "service": "Blood-Connect API",
        "notificationWorker": (
            notification_worker.status() if notification_worker else "disabled"
        ),
        "requestExpiration": (
            request_expirer.status() if request_expirer else "disabled"
        ),
    }


@app.get("/", tags=["System"])
def root():
    return {
        "message": "Blood-Connect API is running!",
        "version": "2.1.0"
    }


# ------------------------------------------------------------------------------
# 5. Legacy identity verification endpoint (removed)
# ------------------------------------------------------------------------------

@app.post("/api/verify", tags=["Identity Verification"], status_code=status.HTTP_410_GONE)
def verify_identity_removed():
    """Removed: it trusted a match result sent by the app.

    Use POST /api/verification/face, which runs the match on the server.
    """
    return JSONResponse(
        status_code=status.HTTP_410_GONE,
        content={
            "detail": "This endpoint was removed. Use POST /api/verification/face.",
            "replacement": "/api/verification/face",
        },
    )
