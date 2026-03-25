import time
from unittest.mock import patch

from fc_pool_manager.vm import VMInstance, VMState


def _make_vm(**kwargs) -> VMInstance:
    defaults = dict(
        vm_id="vm-test", short_id="test1234", ip="172.16.0.10",
        cid=3, tap_name="tap-test", mac="AA:FC:00:00:00:0A",
        jail_path="/tmp/jail", vsock_path="/tmp/jail/v.sock",
    )
    defaults.update(kwargs)
    return VMInstance(**defaults)


class TestVMTimestamps:
    def test_created_at_set_on_construction(self):
        before = time.monotonic()
        vm = _make_vm()
        after = time.monotonic()
        assert before <= vm.created_at <= after

    def test_created_at_immutable_across_transitions(self):
        vm = _make_vm()
        original = vm.created_at
        vm.transition_to(VMState.IDLE)
        assert vm.created_at == original
        vm.transition_to(VMState.ASSIGNED)
        assert vm.created_at == original

    def test_assigned_at_none_initially(self):
        vm = _make_vm()
        assert vm.assigned_at is None

    def test_assigned_at_set_on_assign(self):
        vm = _make_vm()
        vm.transition_to(VMState.IDLE)
        before = time.monotonic()
        vm.transition_to(VMState.ASSIGNED)
        after = time.monotonic()
        assert vm.assigned_at is not None
        assert before <= vm.assigned_at <= after

    def test_assigned_at_cleared_on_idle(self):
        vm = _make_vm()
        vm.transition_to(VMState.IDLE)
        vm.transition_to(VMState.ASSIGNED)
        assert vm.assigned_at is not None
        vm.transition_to(VMState.IDLE)
        assert vm.assigned_at is None

    def test_assigned_at_preserved_through_stopping(self):
        vm = _make_vm()
        vm.transition_to(VMState.IDLE)
        vm.transition_to(VMState.ASSIGNED)
        ts = vm.assigned_at
        vm.transition_to(VMState.STOPPING)
        assert vm.assigned_at == ts
