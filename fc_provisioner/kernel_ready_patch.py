"""Monkey-patch jupyter_client to reduce kernel_info poll interval.

The default _async_wait_for_ready polls every 1.0s (shell_channel.get_msg
timeout). This patch reduces it to 0.1s for faster kernel startup detection.

Apply once at import time:
    import fc_provisioner.kernel_ready_patch  # noqa: F401
"""

import asyncio
import time

from jupyter_client.client import KernelClient
from jupyter_client.utils import ensure_async

_POLL_INTERVAL = 0.1
_IOPUB_TIMEOUT = 0.05


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
            msg = await ensure_async(self.shell_channel.get_msg(timeout=_POLL_INTERVAL))
        except Exception:
            pass
        else:
            if msg["msg_type"] == "kernel_info_reply":
                try:
                    await ensure_async(self.iopub_channel.get_msg(timeout=_IOPUB_TIMEOUT))
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
            await ensure_async(self.iopub_channel.get_msg(timeout=_IOPUB_TIMEOUT))
        except Exception:
            break


KernelClient._async_wait_for_ready = _fast_wait_for_ready
