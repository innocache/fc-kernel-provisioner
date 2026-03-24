"""Monkey-patch jupyter_client to reduce kernel_info poll interval from 1.0s to 0.1s.

The jupyter_server nudge interval (0.5s) must be patched via sed on the
vendored file — see scripts/patch_jupyter_polling.sh.

Import once at provisioner load time to apply the client-side patch.
"""

import asyncio
import time

from jupyter_client.client import KernelClient
from jupyter_client.utils import ensure_async

_FAST_INTERVAL = 0.1


async def _fast_wait_for_ready(self, timeout=None):
    if timeout is None:
        timeout = float("inf")
    abs_timeout = time.time() + timeout

    from jupyter_client.manager import KernelManager

    if not isinstance(self.parent, KernelManager):
        while not await self._async_is_alive():
            if time.time() > abs_timeout:
                raise RuntimeError(
                    "Kernel didn't respond to heartbeats in %d seconds and timed out" % timeout
                )
            await asyncio.sleep(0.05)

    while True:
        self.kernel_info()
        try:
            msg = await ensure_async(self.shell_channel.get_msg(timeout=_FAST_INTERVAL))
        except Exception:
            pass
        else:
            if msg["msg_type"] == "kernel_info_reply":
                try:
                    await ensure_async(self.iopub_channel.get_msg(timeout=0.05))
                except Exception:
                    pass
                else:
                    self._handle_kernel_info_reply(msg)
                    break

        if not await self._async_is_alive():
            raise RuntimeError("Kernel died before replying to kernel_info")

        if time.time() > abs_timeout:
            raise RuntimeError("Kernel didn't respond in %d seconds" % timeout)

    while True:
        try:
            await ensure_async(self.iopub_channel.get_msg(timeout=0.05))
        except Exception:
            break


KernelClient._async_wait_for_ready = _fast_wait_for_ready
