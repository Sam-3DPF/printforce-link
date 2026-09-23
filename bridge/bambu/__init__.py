"""Link-owned Bambu transport. MQTT is ``session``; implicit FTPS is ``ftps``."""

from .session import LinkSession

__all__ = ["LinkSession"]
