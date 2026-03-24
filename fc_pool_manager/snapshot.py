import hashlib
import json
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

METADATA_FILE = "metadata.json"
VMSTATE_FILE = "vmstate"
MEMORY_FILE = "memory"


@dataclass(frozen=True)
class SnapshotMetadata:
    kernel_hash: str
    rootfs_hash: str
    firecracker_path: str
    golden_tap_name: str = ""


class SnapshotManager:
    def __init__(
        self,
        snapshot_dir: str,
        kernel_path: str,
        rootfs_path: str,
        firecracker_path: str,
    ):
        self._dir = snapshot_dir
        self._kernel_path = kernel_path
        self._rootfs_path = rootfs_path
        self._firecracker_path = firecracker_path

    @property
    def vmstate_path(self) -> str:
        return os.path.join(self._dir, VMSTATE_FILE)

    @property
    def memory_path(self) -> str:
        return os.path.join(self._dir, MEMORY_FILE)

    def _metadata_path(self) -> str:
        return os.path.join(self._dir, METADATA_FILE)

    def _current_metadata(self, golden_tap_name: str = "") -> SnapshotMetadata:
        return SnapshotMetadata(
            kernel_hash=self._file_hash(self._kernel_path),
            rootfs_hash=self._file_hash(self._rootfs_path),
            firecracker_path=self._firecracker_path,
            golden_tap_name=golden_tap_name,
        )

    @staticmethod
    def _file_hash(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                digest.update(chunk)
        return digest.hexdigest()

    def has_valid_snapshot(self) -> bool:
        meta_path = self._metadata_path()
        if not os.path.isfile(meta_path):
            return False
        if not os.path.isfile(self.vmstate_path):
            return False
        if not os.path.isfile(self.memory_path):
            return False

        try:
            with open(meta_path) as f:
                saved = json.load(f)
            current = self._current_metadata()
            return (
                saved.get("kernel_hash") == current.kernel_hash
                and saved.get("rootfs_hash") == current.rootfs_hash
                and saved.get("firecracker_path") == current.firecracker_path
            )
        except Exception:
            logger.exception("Failed validating snapshot metadata")
            return False

    @property
    def golden_tap_name(self) -> str:
        meta_path = self._metadata_path()
        if os.path.isfile(meta_path):
            try:
                with open(meta_path) as f:
                    return json.load(f).get("golden_tap_name", "")
            except Exception:
                pass
        return ""

    def save_metadata(self, golden_tap_name: str = "") -> None:
        os.makedirs(self._dir, exist_ok=True)
        meta = self._current_metadata(golden_tap_name=golden_tap_name)
        with open(self._metadata_path(), "w") as f:
            json.dump(
                {
                    "kernel_hash": meta.kernel_hash,
                    "rootfs_hash": meta.rootfs_hash,
                    "firecracker_path": meta.firecracker_path,
                    "golden_tap_name": meta.golden_tap_name,
                },
                f,
            )

    def invalidate(self) -> None:
        for name in (METADATA_FILE, VMSTATE_FILE, MEMORY_FILE):
            path = os.path.join(self._dir, name)
            if os.path.exists(path):
                os.remove(path)
