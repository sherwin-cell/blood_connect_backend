"""Move ID photos and Blood Request Forms from public Cloudinary links to
private storage (files/).

Older app builds uploaded these with an unsigned preset, so anyone with the
link could open them. For every public link on our Cloudinary account in:

  id_verifications/{uid}: frontIdImageUrl, backIdImageUrl
  donors/{uid}:           frontIdImageUrl, backIdImageUrl, idVerification.*
  blood_requests/{id}:    brfFileUrl, idVerification.*

the file is downloaded once, uploaded privately under the owner's folder, and
every copy of the link is replaced with the secure:// reference. The public
copies are then deleted from Cloudinary (skip with --keep-public).

Dry run by default; pass --apply to write.

    python scripts/migrate_private_files.py            # show what would change
    python scripts/migrate_private_files.py --apply    # migrate
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# (dotted field path, upload kind) per collection.
FIELDS = {
    "id_verifications": [("frontIdImageUrl", "id_front"), ("backIdImageUrl", "id_back")],
    "donors": [
        ("frontIdImageUrl", "id_front"),
        ("backIdImageUrl", "id_back"),
        ("idVerification.frontIdImageUrl", "id_front"),
        ("idVerification.backIdImageUrl", "id_back"),
    ],
    "blood_requests": [
        ("brfFileUrl", "brf"),
        ("idVerification.frontIdImageUrl", "id_front"),
        ("idVerification.backIdImageUrl", "id_back"),
    ],
}


@dataclass(frozen=True)
class Move:
    collection: str
    doc_id: str
    field: str
    url: str
    kind: str
    owner: str


def owner_of(collection: str, doc_id: str, data: dict) -> Optional[str]:
    if collection == "blood_requests":
        return data.get("requesterId") or data.get("userId")
    return data.get("userId") or doc_id


def get_path(data: dict, dotted: str):
    value = data
    for part in dotted.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def plan(collection: str, docs: Iterable[tuple[str, dict]], is_public) -> list[Move]:
    """Public links to move, in one collection (pure, unit tested)."""
    moves = []
    for doc_id, data in docs:
        owner = owner_of(collection, doc_id, data)
        for field, kind in FIELDS[collection]:
            url = get_path(data, field)
            if owner and is_public(url):
                moves.append(Move(collection, doc_id, field, url, kind, owner))
    return moves


def raw_public_id(url: str) -> Optional[str]:
    """.../raw/upload/v123/folder/name.pdf -> folder/name.pdf (raw ids keep the extension)."""
    marker = "/raw/upload/"
    if marker not in url:
        return None
    parts = url.split(marker, 1)[1].split("?", 1)[0].split("/")
    if parts and len(parts[0]) > 1 and parts[0][0] == "v" and parts[0][1:].isdigit():
        parts = parts[1:]
    return "/".join(parts) or None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="migrate (default: dry run)")
    parser.add_argument("--keep-public", action="store_true",
                        help="do not delete the public copies after migrating")
    parser.add_argument("--key", default="serviceAccountKey.json",
                        help="service account key (default: serviceAccountKey.json)")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

    import firebase_admin
    import requests
    from firebase_admin import credentials, firestore

    from files.router import build_private_storage
    from files.service import FileSettings, detect_extension
    from files.storage import API_ROOT, _signed
    from verification.config import FaceVerificationSettings
    from verification.images import is_allowed_image_url
    from verification.storage import CloudinaryUploader, fetch_image

    cloud = FaceVerificationSettings.from_env()
    prefix = cloud.allowed_image_prefix
    private = build_private_storage()
    if private is None:
        print("Cloudinary API key/secret are not set in .env; cannot migrate.")
        return 1
    max_bytes = FileSettings().max_bytes("brf")

    firebase_admin.initialize_app(credentials.Certificate(args.key))
    db = firestore.client()

    moves: list[Move] = []
    for collection in FIELDS:
        docs = ((s.id, s.to_dict() or {}) for s in db.collection(collection).stream())
        moves.extend(plan(collection, docs, lambda url: is_allowed_image_url(url, prefix)))

    if not moves:
        print("No public ID photos or Blood Request Forms found. Nothing to do.")
        return 0

    for move in moves:
        print(f"{move.collection}/{move.doc_id} {move.field} ({move.kind}): {move.url}")
    if not args.apply:
        unique = len({(m.url, m.owner, m.kind) for m in moves})
        print(f"\nWould move {unique} file(s) referenced in {len(moves)} field(s). "
              "Run with --apply to migrate.")
        return 0

    destroy_image = CloudinaryUploader(cloud.cloudinary_cloud_name, cloud.cloudinary_api_key,
                                       cloud.cloudinary_api_secret, cloud.cloudinary_folder,
                                       prefix).destroy

    def destroy_public(url: str, kind: str) -> bool:
        if kind != "brf":
            return destroy_image(url)
        public_id = raw_public_id(url)
        if not public_id:
            return False
        form = _signed({"public_id": public_id, "timestamp": int(time.time())},
                       cloud.cloudinary_api_key, cloud.cloudinary_api_secret)
        response = requests.post(f"{API_ROOT}/{cloud.cloudinary_cloud_name}/raw/destroy",
                                 data=form, timeout=30)
        return response.ok and (response.json() or {}).get("result") in ("ok", "not found")

    refs: dict[tuple[str, str, str], str] = {}
    failed = 0
    for move in moves:
        key = (move.url, move.owner, move.kind)
        if key not in refs:
            try:
                data = fetch_image(move.url, prefix, max_bytes)
                extension = detect_extension(move.kind, data)
                if extension is None:
                    raise ValueError("file type does not match " + move.kind)
                refs[key] = str(private.upload(data, move.owner, move.kind, extension))
            except Exception as error:  # report and continue with the rest
                failed += 1
                print(f"  SKIPPED {move.url}: {error}")
                continue
        db.collection(move.collection).document(move.doc_id).update({move.field: refs[key]})
        print(f"  {move.collection}/{move.doc_id} {move.field} -> {refs[key]}")

    deleted = 0
    if not args.keep_public:
        for url, _owner, kind in refs:
            try:
                deleted += bool(destroy_public(url, kind))
            except Exception as error:
                print(f"  Could not delete public copy {url}: {error}")

    print(f"\nMigrated {len(refs)} file(s); {failed} skipped; "
          f"{'kept' if args.keep_public else f'deleted {deleted}'} public copies.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
