"""Edge case tests for VM state machine and CID allocation."""

import pytest
from fc_pool_manager.vm import VMInstance, VMState, CIDAllocator


def make_vm(vm_id="vm-test1234", state=VMState.BOOTING):
    return VMInstance(
        vm_id=vm_id,
        short_id=vm_id.replace("vm-", ""),
        ip="172.16.0.2",
        cid=3,
        tap_name=f"tap-{vm_id.replace('vm-', '')}",
        mac="AA:FC:00:00:00:02",
        jail_path="/tmp/jail",
        vsock_path="/tmp/jail/v.sock",
        state=state,
    )


class TestVMStateExhaustiveTransitions:
    """Test every possible state transition (valid and invalid)."""

    @pytest.mark.parametrize(
        "from_state,to_state,valid",
        [
            # From BOOTING
            (VMState.BOOTING, VMState.IDLE, True),
            (VMState.BOOTING, VMState.STOPPING, True),
            (VMState.BOOTING, VMState.ASSIGNED, False),
            (VMState.BOOTING, VMState.BOOTING, False),
            # From IDLE
            (VMState.IDLE, VMState.ASSIGNED, True),
            (VMState.IDLE, VMState.STOPPING, True),
            (VMState.IDLE, VMState.BOOTING, False),
            (VMState.IDLE, VMState.IDLE, False),
            # From ASSIGNED
            (VMState.ASSIGNED, VMState.STOPPING, True),
            (VMState.ASSIGNED, VMState.BOOTING, False),
            (VMState.ASSIGNED, VMState.IDLE, False),
            (VMState.ASSIGNED, VMState.ASSIGNED, False),
            # From STOPPING — terminal state, no transitions
            (VMState.STOPPING, VMState.BOOTING, False),
            (VMState.STOPPING, VMState.IDLE, False),
            (VMState.STOPPING, VMState.ASSIGNED, False),
            (VMState.STOPPING, VMState.STOPPING, False),
        ],
    )
    def test_transition_matrix(self, from_state, to_state, valid):
        assert from_state.can_transition_to(to_state) == valid

    @pytest.mark.parametrize(
        "from_state,to_state",
        [
            (VMState.BOOTING, VMState.ASSIGNED),
            (VMState.ASSIGNED, VMState.IDLE),
            (VMState.STOPPING, VMState.IDLE),
            (VMState.STOPPING, VMState.BOOTING),
        ],
    )
    def test_invalid_transition_raises_on_vm(self, from_state, to_state):
        vm = make_vm()
        # Walk the VM to from_state
        if from_state == VMState.IDLE:
            vm.transition_to(VMState.IDLE)
        elif from_state == VMState.ASSIGNED:
            vm.transition_to(VMState.IDLE)
            vm.transition_to(VMState.ASSIGNED)
        elif from_state == VMState.STOPPING:
            vm.transition_to(VMState.IDLE)
            vm.transition_to(VMState.ASSIGNED)
            vm.transition_to(VMState.STOPPING)
        with pytest.raises(ValueError, match="Invalid state transition"):
            vm.transition_to(to_state)

    def test_full_lifecycle_path(self):
        """BOOTING -> IDLE -> ASSIGNED -> STOPPING is the happy path."""
        vm = make_vm()
        assert vm.state == VMState.BOOTING
        vm.transition_to(VMState.IDLE)
        assert vm.state == VMState.IDLE
        vm.transition_to(VMState.ASSIGNED)
        assert vm.state == VMState.ASSIGNED
        vm.transition_to(VMState.STOPPING)
        assert vm.state == VMState.STOPPING

    def test_early_abort_path(self):
        """BOOTING -> STOPPING is valid for boot failures."""
        vm = make_vm()
        vm.transition_to(VMState.STOPPING)
        assert vm.state == VMState.STOPPING

    def test_idle_to_stopping_path(self):
        """IDLE -> STOPPING is valid for health check failures."""
        vm = make_vm()
        vm.transition_to(VMState.IDLE)
        vm.transition_to(VMState.STOPPING)
        assert vm.state == VMState.STOPPING


class TestCIDAllocatorEdgeCases:
    def test_release_unallocated_cid_is_noop(self):
        """Releasing a CID that was never allocated should not crash."""
        alloc = CIDAllocator()
        alloc.release(999)
        # The free list should not contain 999
        assert 999 not in alloc._free

    def test_double_release_same_cid(self):
        """Releasing the same CID twice should only add it to free once."""
        alloc = CIDAllocator()
        cid = alloc.allocate()
        alloc.release(cid)
        alloc.release(cid)  # Second release — CID is no longer in _allocated
        assert alloc._free.count(cid) == 1

    def test_many_allocations_are_unique(self):
        """Bulk allocations should all be unique."""
        alloc = CIDAllocator()
        cids = [alloc.allocate() for _ in range(100)]
        assert len(set(cids)) == 100

    def test_recycle_order(self):
        """Released CIDs should be reused before new ones are minted."""
        alloc = CIDAllocator()
        a = alloc.allocate()  # 3
        b = alloc.allocate()  # 4
        alloc.release(a)  # free: [3]
        c = alloc.allocate()  # should get 3 back
        assert c == a
        d = alloc.allocate()  # should get 5 (new)
        assert d == 5

    def test_custom_start(self):
        """CIDAllocator with custom start value."""
        alloc = CIDAllocator(start=100)
        assert alloc.allocate() == 100
        assert alloc.allocate() == 101

    def test_allocated_tracking(self):
        """Allocated set should reflect current state."""
        alloc = CIDAllocator()
        c1 = alloc.allocate()
        c2 = alloc.allocate()
        assert c1 in alloc._allocated
        assert c2 in alloc._allocated
        alloc.release(c1)
        assert c1 not in alloc._allocated
        assert c2 in alloc._allocated


class TestVMInstanceEdgeCases:
    def test_default_state_is_booting(self):
        vm = make_vm()
        assert vm.state == VMState.BOOTING

    def test_jailer_process_defaults_to_none(self):
        vm = make_vm()
        assert vm.jailer_process is None

    def test_transition_error_includes_vm_id(self):
        """Error message should contain the VM ID for debugging."""
        vm = make_vm(vm_id="vm-deadbeef")
        with pytest.raises(ValueError, match="vm-deadbeef"):
            vm.transition_to(VMState.ASSIGNED)

    def test_transition_error_includes_states(self):
        """Error message should contain both from and to states."""
        vm = make_vm()
        with pytest.raises(ValueError, match="booting.*assigned"):
            vm.transition_to(VMState.ASSIGNED)
