"""Pytest configuration for direct-mode tests.

The gltest direct loader injects the transaction message into stdin (fd 0)
via a temp file, then immediately calls os.unlink while fd 0 still references
that file. On Windows this raises PermissionError because a file held open on
fd 0 cannot be removed. The message is only needed during the SDK import, so
this shim preserves the original POSIX behavior and simply defers cleanup when
immediate removal is not permitted. It is a no-op change on Linux.
"""

import atexit
import os
import tempfile


def _safe_unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _patched_inject(vm):
    try:
        from genlayer.py import calldata
        from genlayer.py.types import Address
    except ImportError:
        return

    sender_addr = vm.sender
    if isinstance(sender_addr, bytes):
        sender_addr = Address(sender_addr)

    contract_addr = vm._contract_address
    if isinstance(contract_addr, bytes):
        contract_addr = Address(contract_addr)

    origin_addr = vm.origin
    if isinstance(origin_addr, bytes):
        origin_addr = Address(origin_addr)

    message_data = {
        "contract_address": contract_addr,
        "sender_address": sender_addr,
        "origin_address": origin_addr,
        "stack": [],
        "value": vm._value,
        "datetime": vm._datetime,
        "is_init": False,
        "chain_id": vm._chain_id,
        "entry_kind": 0,
        "entry_data": b"",
        "entry_stage_data": None,
    }
    encoded = calldata.encode(message_data)

    fd, path = tempfile.mkstemp()
    try:
        os.write(fd, encoded)
        os.lseek(fd, 0, os.SEEK_SET)
        original_stdin = os.dup(0)
        vm._original_stdin_fd = original_stdin
        os.dup2(fd, 0)
    finally:
        os.close(fd)

    try:
        os.unlink(path)
    except PermissionError:
        atexit.register(_safe_unlink, path)


def pytest_configure(config):
    import gltest.direct.loader as loader

    loader._inject_message_to_fd0 = _patched_inject
