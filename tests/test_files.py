"""Tests for private uploads (files/). Run: python -m unittest discover tests"""

import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient

from files import router as files_router
from files.refs import SecureRef, is_secure_ref, parse_ref
from files.service import Code, FileService, FileSettings
from files.storage import PrivateCloudinary
from verification.storage import CloudinaryUploader, StorageError

FOLDER = "blood_connect_private"
UID = "user-1"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100
PDF = b"%PDF-1.7\n" + b"0" * 100
FRONT = f"secure://image/{FOLDER}/{UID}/id_front/abc123.jpg"
BRF = f"secure://raw/{FOLDER}/{UID}/brf/def456.pdf"


class FakeStorage:
    folder = FOLDER

    def __init__(self, fail=False):
        self.fail = fail
        self.uploads = []

    def upload(self, data, uid, kind, extension):
        if self.fail:
            raise StorageError("down")
        self.uploads.append((uid, kind, extension))
        resource = "raw" if kind == "brf" else "image"
        return SecureRef(resource, FOLDER, uid, kind, f"n.{extension}")

    def signed_url(self, ref, ttl):
        return f"https://signed.example/{ref.public_id}?ttl={ttl}"


def make(storage="default", admins=()):
    return FileService(FakeStorage() if storage == "default" else storage,
                       lambda uid: uid in admins, FileSettings(folder=FOLDER))


class RefTest(unittest.TestCase):
    def test_round_trip(self):
        for value in (FRONT, BRF):
            ref = parse_ref(value, FOLDER)
            self.assertIsNotNone(ref, value)
            self.assertEqual(str(ref), value)
        self.assertEqual(parse_ref(FRONT, FOLDER).public_id, f"{FOLDER}/{UID}/id_front/abc123")
        self.assertEqual(parse_ref(FRONT, FOLDER).format, "jpg")
        # Raw files keep the extension in the public id and send no format.
        self.assertEqual(parse_ref(BRF, FOLDER).public_id, f"{FOLDER}/{UID}/brf/def456.pdf")
        self.assertEqual(parse_ref(BRF, FOLDER).format, "")

    def test_rejects_malformed_or_foreign_refs(self):
        bad = [
            "https://res.cloudinary.com/dlk8chosr/image/upload/x.jpg",
            f"secure://image/other_folder/{UID}/id_front/a.jpg",
            f"secure://image/{FOLDER}/../id_front/a.jpg",
            f"secure://image/{FOLDER}/{UID}/selfie/a.jpg",
            f"secure://raw/{FOLDER}/{UID}/id_front/a.jpg",
            f"secure://image/{FOLDER}/{UID}/brf/a.pdf",
            f"secure://image/{FOLDER}/{UID}/id_front/a.gif",
            f"secure://image/{FOLDER}/{UID}/id_front/sub/a.jpg",
            f"secure://image/{FOLDER}/{UID}/id_front/a.b.jpg",
            None, 42,
        ]
        for value in bad:
            self.assertIsNone(parse_ref(value, FOLDER), value)
        self.assertTrue(is_secure_ref(FRONT))
        self.assertFalse(is_secure_ref("https://x"))


class UploadTest(unittest.TestCase):
    def test_uploads_by_real_file_type(self):
        service = make()
        cases = [("id_front", JPEG, "jpg"), ("id_back", PNG, "png"), ("brf", PDF, "pdf")]
        for kind, data, extension in cases:
            outcome = service.upload(UID, kind, data)
            self.assertEqual((outcome.status, outcome.code), (200, Code.UPLOADED), kind)
            self.assertTrue(outcome.json()["ref"].startswith("secure://"))
            self.assertEqual(service._storage.uploads[-1], (UID, kind, extension))

    def test_rejects_wrong_type_size_or_kind(self):
        service = make()
        self.assertEqual(service.upload(UID, "brf", JPEG).code, Code.INVALID_FILE_TYPE)
        self.assertEqual(service.upload(UID, "id_front", PDF).code, Code.INVALID_FILE_TYPE)
        self.assertEqual(service.upload(UID, "id_front", b"").code, Code.EMPTY_FILE)
        self.assertEqual(service.upload(UID, "selfie", JPEG).code, Code.INVALID_KIND)
        too_big = JPEG + b"0" * service.settings.max_image_bytes
        self.assertEqual(service.upload(UID, "id_front", too_big).status, 413)
        self.assertEqual(service._storage.uploads, [])

    def test_not_configured_and_storage_failure(self):
        self.assertEqual(make(storage=None).upload(UID, "brf", PDF).status, 503)
        self.assertEqual(make(storage=FakeStorage(fail=True)).upload(UID, "brf", PDF).status, 502)


class ViewTest(unittest.TestCase):
    def test_owner_gets_short_lived_url(self):
        outcome = make().view({"uid": UID}, FRONT)
        self.assertEqual(outcome.status, 200)
        self.assertEqual(outcome.json()["expiresIn"], 600)

    def test_admin_by_role_or_claim(self):
        self.assertEqual(make(admins={"prc"}).view({"uid": "prc"}, BRF).status, 200)
        self.assertEqual(make().view({"uid": "prc", "admin": True}, BRF).status, 200)

    def test_other_users_are_refused(self):
        outcome = make().view({"uid": "someone-else"}, FRONT)
        self.assertEqual((outcome.status, outcome.code), (403, Code.FORBIDDEN))
        self.assertNotIn("url", outcome.json())

    def test_invalid_ref(self):
        self.assertEqual(make().view({"uid": UID}, "https://res.cloudinary.com/x").status, 400)


class StorageTest(unittest.TestCase):
    def storage(self):
        return PrivateCloudinary("cloud", "key", "secret", FOLDER, clock=lambda: 1_000,
                                 new_name=lambda: "fixed")

    def test_signed_url_expires_and_is_signed(self):
        ref = parse_ref(FRONT, FOLDER)
        url = self.storage().signed_url(ref, 600)
        parts = urlsplit(url)
        self.assertEqual(parts.netloc, "api.cloudinary.com")
        self.assertEqual(parts.path, "/v1_1/cloud/image/download")
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.assertEqual(query["type"], "private")
        self.assertEqual(query["expires_at"], "1600")
        self.assertEqual(query["format"], "jpg")
        signed = {k: v for k, v in query.items() if k not in ("api_key", "signature")}
        self.assertEqual(query["signature"], CloudinaryUploader.signature(signed, "secret"))

    def test_raw_url_has_no_format(self):
        url = self.storage().signed_url(parse_ref(BRF, FOLDER), 60)
        self.assertIn("/raw/download", url)
        self.assertNotIn("format=", url)

    def test_upload_is_private_with_signed_params(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"type": "private",
                                      "public_id": f"{FOLDER}/{UID}/brf/fixed.pdf"}
        with mock.patch("files.storage.requests.post", return_value=response) as post:
            ref = self.storage().upload(PDF, UID, "brf", "pdf")
        self.assertEqual(str(ref), f"secure://raw/{FOLDER}/{UID}/brf/fixed.pdf")
        url = post.call_args.args[0]
        form = post.call_args.kwargs["data"]
        self.assertEqual(url, "https://api.cloudinary.com/v1_1/cloud/raw/upload")
        self.assertEqual(form["type"], "private")
        self.assertNotIn("upload_preset", form)

    def test_upload_rejects_a_public_result(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"type": "upload",
                                      "public_id": f"{FOLDER}/{UID}/brf/fixed.pdf"}
        with mock.patch("files.storage.requests.post", return_value=response):
            with self.assertRaises(StorageError):
                self.storage().upload(PDF, UID, "brf", "pdf")


class EndpointTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(files_router.router)
        files_router.configure(make())
        self.client = TestClient(app)

    def tearDown(self):
        files_router.configure(None)

    def test_requires_sign_in(self):
        self.assertEqual(self.client.get("/api/files/view", params={"ref": FRONT}).status_code, 401)

    def test_upload_and_view(self):
        with mock.patch("auth.auth.verify_id_token", return_value={"uid": UID}):
            upload = self.client.post("/api/files/brf", headers={"Authorization": "Bearer t"},
                                      files={"file": ("form.pdf", PDF, "application/pdf")})
            self.assertEqual(upload.status_code, 200)
            view = self.client.get("/api/files/view", params={"ref": upload.json()["ref"]},
                                   headers={"Authorization": "Bearer t"})
        self.assertEqual(view.status_code, 200)
        self.assertEqual(view.headers["cache-control"], "no-store")

    def test_other_user_cannot_view(self):
        with mock.patch("auth.auth.verify_id_token", return_value={"uid": "intruder"}):
            view = self.client.get("/api/files/view", params={"ref": FRONT},
                                   headers={"Authorization": "Bearer t"})
        self.assertEqual(view.status_code, 403)


if __name__ == "__main__":
    unittest.main()


class MigrationPlanTest(unittest.TestCase):
    def test_finds_public_links_with_their_owner_and_kind(self):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
        from migrate_private_files import plan, raw_public_id

        public = lambda url: isinstance(url, str) and url.startswith("https://res.cloudinary.com/x/")
        docs = [
            ("req1", {"requesterId": "u1", "brfFileUrl": "https://res.cloudinary.com/x/raw/upload/v1/a.pdf",
                      "idVerification": {"frontIdImageUrl": "https://res.cloudinary.com/x/image/upload/f.jpg",
                                         "backIdImageUrl": FRONT}}),
            ("req2", {"userId": "u2", "brfFileUrl": BRF}),
        ]
        moves = plan("blood_requests", docs, public)
        self.assertEqual([(m.doc_id, m.field, m.kind, m.owner) for m in moves], [
            ("req1", "brfFileUrl", "brf", "u1"),
            ("req1", "idVerification.frontIdImageUrl", "id_front", "u1"),
        ])
        self.assertEqual(plan("id_verifications", [("u3", {"frontIdImageUrl": "https://res.cloudinary.com/x/i.png"})],
                              public)[0].owner, "u3")
        self.assertEqual(raw_public_id("https://res.cloudinary.com/x/raw/upload/v12/brf/a.pdf"), "brf/a.pdf")
