try:
    from .provisioner import FirecrackerProvisioner
except ImportError:
    # jupyter_client not installed — provisioner unavailable but
    # submodules (vsock_client, pool_client) still work.
    FirecrackerProvisioner = None

__all__ = ["FirecrackerProvisioner"]
