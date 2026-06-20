"""Persistence seam.

The abstract backends that decouple *what* GNSIS stores (resources, memory,
jobs) from *where* it is stored. The file-backed implementations live next to
their domains; the Postgres implementations (Railway) will plug in here without
touching the core or the worker.
"""

from .base import MemoryProvider, ResourceStoreBackend

__all__ = ["ResourceStoreBackend", "MemoryProvider"]
