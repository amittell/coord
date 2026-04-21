from __future__ import annotations

from functools import lru_cache

from coordination.service import CoordinationService, build_service


@lru_cache
def get_service() -> CoordinationService:
    return build_service()
