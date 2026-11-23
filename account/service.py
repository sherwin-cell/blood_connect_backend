"""In-app account deletion (Google Play requirement).

DELETE /api/account removes the caller's account. The app re-authenticates
the user first, so the ID token must be fresh (auth_time within
ACCOUNT_DELETE_MAX_AUTH_AGE_SECONDS, default 5 minutes); this stops someone
with an unlocked phone and an old session from deleting the account.

What happens, in order:
1. In-progress blood requests are cancelled and active donor offers are
   withdrawn, so other users and PRC are notified as usual.
2. Images the user uploaded to our Cloudinary account (ID photos, selfies,
   profile photo) are destroyed. Best effort: a failure is logged and
   counted, it does not stop the deletion.
3. Personal documents are deleted: users/{uid} (with fcm_tokens),
   donors/{uid} (with application_history), id_verifications, face
   verification attempts and quota, notifications and chats.
4. account_deletions/{uid} records when and what was deleted (counts only).
5. The Firebase Auth user is deleted last, so a failure earlier can be
   retried by the still-signed-in user. Every step is idempotent.

DELETE /api/admin/users/{uid} runs the same steps for a PRC admin removing
someone else's account from the admin portal. The caller must be an admin
(the `admin` custom claim or users/{uid}.role == "admin", as in
firestore.rules isAdmin()); admins cannot delete themselves this way or
delete other admins.

Closed blood requests and offers (fulfilled, cancelled, donated, ...) stay:
they are PRC's service records and also belong to the other party.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

log = logging.getLogger(__name__)

DEFAULT_MAX_AUTH_AGE_SECONDS = 300
CANCEL_REASON = "The requester deleted their Blood-Connect account."
ADMIN_CANCEL_REASON = "PRC removed the requester's Blood-Connect account."


class Code:
    DELETED = "deleted"
    REQUIRES_RECENT_LOGIN = "requires_recent_login"
    FAILED = "deletion_failed"
    FORBIDDEN = "forbidden"
    CANNOT_DELETE_SELF = "cannot_delete_self"
    CANNOT_DELETE_ADMIN = "cannot_delete_admin"


class AccountStore(Protocol):
    def close_open_requests(self, uid: str, reason: str) -> int: ...
    def withdraw_active_offers(self, uid: str) -> int: ...
    def image_urls(self, uid: str) -> list[str]: ...
    def delete_personal_data(self, uid: str) -> dict[str, int]: ...
    def record_deletion(self, uid: str, summary: dict, deleted_by: Optional[str] = None) -> None: ...
    def is_admin(self, uid: str) -> bool: ...


@dataclass
class Outcome:
    status_code: int
    code: str
    message: str
    summary: dict = field(default_factory=dict)

    def body(self) -> dict:
        return {
            "deleted": self.code == Code.DELETED,
            "code": self.code,
            "message": self.message,
            **({"summary": self.summary} if self.summary else {}),
        }


class AccountDeletionService:
    def __init__(
        self,
        store: AccountStore,
        delete_auth_user: Callable[[str], None],
        delete_image: Optional[Callable[[str], bool]] = None,
        max_auth_age_seconds: int = DEFAULT_MAX_AUTH_AGE_SECONDS,
        clock: Callable[[], float] = time.time,
    ):
        self._store = store
        self._delete_auth_user = delete_auth_user
        self._delete_image = delete_image
        self._max_auth_age = max_auth_age_seconds
        self._clock = clock

    def is_recent(self, auth_time) -> bool:
        try:
            age = self._clock() - float(auth_time)
        except (TypeError, ValueError):
            return False
        return -60 <= age <= self._max_auth_age  # small allowance for clock skew

    def delete(self, uid: str, auth_time) -> Outcome:
        if not self.is_recent(auth_time):
            return Outcome(
                401,
                Code.REQUIRES_RECENT_LOGIN,
                "For your security, please sign in again before deleting your account.",
            )

        return self._run(
            uid,
            CANCEL_REASON,
            deleted_by=None,
            done="Your account has been deleted.",
            failed="We could not finish deleting your account. Please try again.",
        )

    def delete_by_admin(self, uid: str, caller: dict) -> Outcome:
        """PRC deletes another user's account. `caller` is the admin's decoded ID token."""
        caller_uid = caller.get("uid")
        if not caller_uid or not (caller.get("admin") is True or self._store.is_admin(caller_uid)):
            return Outcome(403, Code.FORBIDDEN, "Only PRC administrators can delete user accounts.")
        if uid == caller_uid:
            return Outcome(400, Code.CANNOT_DELETE_SELF,
                           "You cannot delete your own administrator account here.")
        if self._store.is_admin(uid):
            return Outcome(403, Code.CANNOT_DELETE_ADMIN,
                           "Administrator accounts cannot be deleted from the portal.")
        return self._run(
            uid,
            ADMIN_CANCEL_REASON,
            deleted_by=caller_uid,
            done="The account and its personal data have been deleted.",
            failed="Could not finish deleting the account. Try again; finished steps are skipped.",
        )

    def _run(self, uid: str, reason: str, *, deleted_by: Optional[str], done: str,
             failed: str) -> Outcome:
        summary: dict = {}
        try:
            summary["requestsCancelled"] = self._store.close_open_requests(uid, reason)
            summary["offersWithdrawn"] = self._store.withdraw_active_offers(uid)
            summary.update(self._delete_images(uid))
            summary.update(self._store.delete_personal_data(uid))
            self._store.record_deletion(uid, summary, deleted_by=deleted_by)
            self._delete_auth_user(uid)
        except Exception:
            log.exception("Account deletion for %s failed (done so far: %s)", uid, summary)
            return Outcome(500, Code.FAILED, failed, summary)

        log.info("Deleted account %s%s: %s", uid,
                 f" (by admin {deleted_by})" if deleted_by else "", summary)
        return Outcome(200, Code.DELETED, done, summary)

    def _delete_images(self, uid: str) -> dict[str, int]:
        if self._delete_image is None:
            return {}
        deleted = failed = 0
        for url in dict.fromkeys(self._store.image_urls(uid)):  # unique, ordered
            try:
                if self._delete_image(url):
                    deleted += 1
                else:
                    failed += 1
            except Exception as error:  # best effort
                failed += 1
                log.warning("Could not delete image for %s: %s", uid, error)
        return {"imagesDeleted": deleted, "imageDeleteFailures": failed}
