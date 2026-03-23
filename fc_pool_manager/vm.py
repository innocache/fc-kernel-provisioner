"""VM instance state management and CID allocation."""

from asyncio.subprocess import Process
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class VMState(Enum):
    """VM lifecycle states."""
    BOOTING = "booting"
    IDLE = "idle"
    ASSIGNED = "assigned"
    STOPPING = "stopping"

    def can_transition_to(self, target: "VMState") -> bool:
        return target in _VALID_TRANSITIONS.get(self, set())


_VALID_TRANSITIONS = {
    VMState.BOOTING: {VMState.IDLE, VMState.STOPPING},
    VMState.IDLE: {VMState.ASSIGNED, VMState.STOPPING},
    VMState.ASSIGNED: {VMState.IDLE, VMState.STOPPING},
    VMState.STOPPING: set(),
}


class CIDAllocator:
    """Allocates unique vsock CIDs starting at 3 (0-2 are reserved).

    The vsock CID space is a 32-bit unsigned integer. CIDs 0, 1, and 2
    are reserved by the kernel, so the valid range is [3, 2^32 - 1).
    """

    MAX_CID = (1 << 32) - 1  # 4294967295

    def __init__(self, start: int = 3):
        self._next = start
        self._free: list[int] = []
        self._allocated: set[int] = set()

    def allocate(self) -> int:
        if self._free:
            cid = self._free.pop(0)
        else:
            if self._next >= self.MAX_CID:
                raise RuntimeError("CID space exhausted")
            cid = self._next
            self._next += 1
        self._allocated.add(cid)
        return cid

    def release(self, cid: int) -> None:
        if cid in self._allocated:
            self._allocated.discard(cid)
            self._free.append(cid)


@dataclass
class VMInstance:
    """Represents a single Firecracker microVM."""

    vm_id: str
    short_id: str
    ip: str
    cid: int
    tap_name: str
    mac: str
    jail_path: str
    vsock_path: str
    state: VMState = field(default=VMState.BOOTING)
    jailer_process: Optional[Process] = field(default=None, repr=False)

    def transition_to(self, new_state: VMState) -> None:
        if not self.state.can_transition_to(new_state):
            raise ValueError(
                f"Invalid state transition: {self.state.value} -> {new_state.value} "
                f"for VM {self.vm_id}"
            )
        self.state = new_state
