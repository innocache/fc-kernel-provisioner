"""Unix domain socket HTTP server for the pool manager API."""

import argparse
import asyncio
import logging
import signal

from aiohttp import web

from .config import PoolConfig
from .manager import PoolManager

logger = logging.getLogger(__name__)


def create_app(manager: PoolManager) -> web.Application:
    """Create the aiohttp application with all routes."""
    app = web.Application()
    app["manager"] = manager

    app.router.add_post("/api/vms/acquire", handle_acquire)
    app.router.add_delete("/api/vms/{vm_id}", handle_release)
    app.router.add_get("/api/vms/{vm_id}/health", handle_health)
    app.router.add_get("/api/pool/status", handle_pool_status)

    return app


async def handle_acquire(request: web.Request) -> web.Response:
    manager: PoolManager = request.app["manager"]
    body = await request.json()
    vcpu = body.get("vcpu", 1)
    mem_mib = body.get("mem_mib", 512)

    try:
        result = await manager.acquire(vcpu=vcpu, mem_mib=mem_mib)
        return web.json_response(result)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except RuntimeError as e:
        if "pool_exhausted" in str(e):
            return web.json_response(
                {"error": "pool_exhausted", "retry_after_ms": 5000},
                status=503,
            )
        return web.json_response({"error": str(e)}, status=500)


async def handle_release(request: web.Request) -> web.Response:
    manager: PoolManager = request.app["manager"]
    vm_id = request.match_info["vm_id"]
    try:
        if request.can_read_body:
            body = await request.json()
            destroy = body.get("destroy", True)
        else:
            destroy = True
    except Exception:
        destroy = True

    try:
        await manager.release(vm_id, destroy=destroy)
        return web.json_response({"ok": True})
    except Exception as e:
        logger.error("Failed to release VM %s: %s", vm_id, e)
        return web.json_response({"error": str(e)}, status=500)


async def handle_health(request: web.Request) -> web.Response:
    manager: PoolManager = request.app["manager"]
    vm_id = request.match_info["vm_id"]
    result = await manager.is_alive(vm_id)
    return web.json_response(result)


async def handle_pool_status(request: web.Request) -> web.Response:
    manager: PoolManager = request.app["manager"]
    return web.json_response(manager.pool_status())


async def run_server(config_path: str, socket_path: str) -> None:
    """Start the pool manager and HTTP server."""
    config = PoolConfig.from_yaml(config_path)
    manager = PoolManager(config)

    app = create_app(manager)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, socket_path)
    await site.start()

    logger.info("Pool manager listening on %s", socket_path)

    await manager.replenish()

    health_task = asyncio.create_task(manager.health_check_loop())

    stop_event = asyncio.Event()

    def on_signal():
        stop_event.set()

    running_loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        running_loop.add_signal_handler(sig, on_signal)

    await stop_event.wait()

    logger.info("Shutting down...")
    health_task.cancel()
    await manager.shutdown()
    await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description="Firecracker VM Pool Manager")
    parser.add_argument("--config", required=True, help="Path to fc-pool.yaml")
    parser.add_argument(
        "--socket", default="/var/run/fc-pool.sock",
        help="Unix socket path (default: /var/run/fc-pool.sock)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    asyncio.run(run_server(args.config, args.socket))


if __name__ == "__main__":
    main()
