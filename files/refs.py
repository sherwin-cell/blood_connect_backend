"""secure:// references to private Cloudinary files (pure, unit tested).

Format: secure://<resource_type>/<folder>/<uid>/<kind>/<name>.<ext>
  e.g.  secure://image/blood_connect_private/AbC123/id_front/5f3c....jpg
        secure://raw/blood_connect_private/AbC123/brf/9a1e....pdf

Images: Cloudinary public_id is the path without the extension and the
extension is the format. Raw files (PDF): public_id includes the extension.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

SCHEME = "secure://"

# Upload kinds: resource type and allowed extensions.
KINDS = {
    "id_front": ("image", ("jpg", "png")),
    "id_back": ("image", ("jpg", "png")),
    "brf": ("raw", ("pdf",)),
}

_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
_NAME = re.compile(r"^[A-Za-z0-9_-]+\.([a-z0-9]+)$")


@dataclass(frozen=True)
class SecureRef:
    resource_type: str  # "image" | "raw"
    folder: str
    uid: str
    kind: str
    name: str  # file name with extension

    @property
    def extension(self) -> str:
        return self.name.rsplit(".", 1)[1]

    @property
    def path(self) -> str:
        return f"{self.folder}/{self.uid}/{self.kind}/{self.name}"

    @property
    def public_id(self) -> str:
        if self.resource_type == "raw":
            return self.path
        return self.path.rsplit(".", 1)[0]

    @property
    def format(self) -> str:
        """Cloudinary `format` parameter: the extension for images, empty for raw."""
        return "" if self.resource_type == "raw" else self.extension

    def __str__(self) -> str:
        return f"{SCHEME}{self.resource_type}/{self.path}"


def is_secure_ref(value: object) -> bool:
    return isinstance(value, str) and value.startswith(SCHEME)


def parse_ref(value: object, folder: str) -> Optional[SecureRef]:
    """The reference, or None if it is malformed or outside `folder`.

    Strict on purpose: every segment is a plain name, so a reference cannot
    point outside the private folder or at another user's files by `..`.
    """
    if not is_secure_ref(value):
        return None
    parts = value[len(SCHEME):].split("/")
    if len(parts) != 5:
        return None
    resource_type, ref_folder, uid, kind, name = parts
    if ref_folder != folder or kind not in KINDS:
        return None
    expected_type, extensions = KINDS[kind]
    if resource_type != expected_type:
        return None
    if not _SEGMENT.match(uid):
        return None
    match = _NAME.match(name)
    if not match or match.group(1) not in extensions:
        return None
    return SecureRef(resource_type, ref_folder, uid, kind, name)
