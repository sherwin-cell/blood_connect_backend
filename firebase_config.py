
import os
import json
import firebase_admin
from firebase_admin import credentials, firestore

def initialize_firebase():
    if not firebase_admin._apps:
        service_account_env = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")

        if service_account_env:
            cred = credentials.Certificate(json.loads(service_account_env))
        elif os.path.exists("serviceAccountKey.json"):
            cred = credentials.Certificate("serviceAccountKey.json")
        else:
            raise RuntimeError(
                "Firebase credentials not configured."
            )

        firebase_admin.initialize_app(cred)

    return firestore.client()
