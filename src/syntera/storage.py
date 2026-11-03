"""Storage interface with a local implementation (roadmap I2).

Objects are bundles of named files stored under a key. :class:`LocalStorage` keeps them
on disk; an object-store implementation (S3-compatible, for example) can provide the
same :class:`Storage` protocol later. Every bundle carries a manifest of SHA-256 hashes,
and a bundle whose files do not match their manifest is treated as missing.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Protocol

KEY = re.compile(r"^[0-9a-f]{64}$")
MANIFEST = "manifest.json"


class StorageError(ValueError):
    """An invalid key or file name."""


class Storage(Protocol):
    def put(self, key: str, files: dict[str, bytes]) -> None: ...

    def get(self, key: str) -> dict[str, bytes] | None: ...

    def exists(self, key: str) -> bool: ...


def _check_name(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts or name == MANIFEST:
        raise StorageError(f"invalid file name in a bundle: {name!r}")
    return str(path)


def _check_key(key: str) -> str:
    if not KEY.match(key):
        raise StorageError(f"invalid storage key {key!r}")
    return key


class LocalStorage:
    """Bundles under ``root/objects/<k[:2]>/<key>/`` with a hash manifest."""

    def __init__(self, root: Path):
        self.root = root

    def _folder(self, key: str) -> Path:
        key = _check_key(key)
        return self.root / "objects" / key[:2] / key

    def put(self, key: str, files: dict[str, bytes]) -> None:
        folder = self._folder(key)
        manifest = {}
        for name, data in sorted(files.items()):
            target = folder / _check_name(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            manifest[name] = hashlib.sha256(data).hexdigest()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                       encoding="utf-8")  # fmt: skip

    def get(self, key: str) -> dict[str, bytes] | None:
        folder = self._folder(key)
        manifest_path = folder / MANIFEST
        if not manifest_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            files = {name: (folder / _check_name(name)).read_bytes() for name in manifest}
        except (OSError, json.JSONDecodeError, StorageError):
            return None
        for name, data in files.items():
            if hashlib.sha256(data).hexdigest() != manifest[name]:
                return None
        return files

    def exists(self, key: str) -> bool:
        return self.get(key) is not None
