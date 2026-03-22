"""Tests for VMInstance and CID allocation."""

import pytest
from fc_pool_manager.vm import VMInstance, VMState, CIDAllocator


class TestVMState:
    def test_valid_transitions(self):
        assert VMState.BOOTING.can_transition_to(VMState.IDLE)
        assert VMState.IDLE.can_transition_to(VMState.ASSIGNED)
        assert VMState.ASSIGNED.can_transition_to(VMState.STOPPING)
        assert VMState.BOOTING.can_transition_to(VMState.STOPPING)

    def test_invalid_transition(self):
        assert not VMState.IDLE.can_transition_to(VMState.BOOTING)
        assert not VMState.STOPPING.can_transition_to(VMState.IDLE)

    def test_assigned_to_idle_valid_for_recycle(self):
        assert VMState.ASSIGNED.can_transition_to(VMState.IDLE)


class TestCIDAllocator:
    def test_first_cid_is_three(self):
        alloc = CIDAllocator()
        assert alloc.allocate() == 3

    def test_sequential(self):
        alloc = CIDAllocator()
        assert alloc.allocate() == 3
        assert alloc.allocate() == 4

    def test_recycle(self):
        alloc = CIDAllocator()
        cid = alloc.allocate()
        alloc.release(cid)
        assert alloc.allocate() == cid


class TestVMInstance:
    def test_creation(self):
        vm = VMInstance(
            vm_id="vm-a1b2c3d4",
            short_id="a1b2c3d4",
            ip="172.16.0.2",
            cid=3,
            tap_name="tap-a1b2c3d4",
            mac="AA:FC:00:00:00:02",
            jail_path="/srv/jailer/firecracker/vm-a1b2c3d4/root",
            vsock_path="/srv/jailer/firecracker/vm-a1b2c3d4/root/v.sock",
        )
        assert vm.state == VMState.BOOTING
        assert vm.vm_id == "vm-a1b2c3d4"

    def test_transition(self):
        vm = VMInstance(
            vm_id="vm-test",
            short_id="test1234",
            ip="172.16.0.2",
            cid=3,
            tap_name="tap-test1234",
            mac="AA:FC:00:00:00:02",
            jail_path="/tmp/jail",
            vsock_path="/tmp/jail/v.sock",
        )
        vm.transition_to(VMState.IDLE)
        assert vm.state == VMState.IDLE

    def test_invalid_transition_raises(self):
        vm = VMInstance(
            vm_id="vm-test",
            short_id="test1234",
            ip="172.16.0.2",
            cid=3,
            tap_name="tap-test1234",
            mac="AA:FC:00:00:00:02",
            jail_path="/tmp/jail",
            vsock_path="/tmp/jail/v.sock",
        )
        with pytest.raises(ValueError, match="Invalid state transition"):
            vm.transition_to(VMState.ASSIGNED)
