from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("coordination")


def _base_url() -> str:
    return os.environ.get("COORD_API_URL", "http://127.0.0.1:8080").rstrip("/")


def _headers() -> dict[str, str]:
    token = os.environ.get("COORD_AUTH_TOKEN", "")
    h: dict[str, str] = {"Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


@mcp.tool()
async def list_claims(
    active_only: bool = True,
    engineer: str | None = None,
    module: str | None = None,
) -> dict[str, Any]:
    """List coordination claims (who is working on which paths)."""
    params: dict[str, Any] = {"active_only": str(active_only).lower()}
    if engineer:
        params["engineer"] = engineer
    if module:
        params["module"] = module
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{_base_url()}/claims", params=params, headers=_headers())
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def check_conflicts(files: list[str], engineer: str) -> dict[str, Any]:
    """Check whether planned file paths conflict with other engineers' active claims."""
    # Annotated with the broader value type httpx.AsyncClient.get accepts so
    # mypy sees an invariant-compatible list when we pass it as `params`.
    params: list[tuple[str, str | int | float | bool | None]] = [
        ("pattern", f) for f in files
    ]
    params.append(("engineer", engineer))
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{_base_url()}/conflicts", params=params, headers=_headers())
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def claim_files(
    engineer: str,
    patterns: list[str],
    description: str | None = None,
    branch: str | None = None,
    shared_files: list[str] | None = None,
    ttl_hours: int | None = None,
) -> dict[str, Any]:
    """Claim files or glob patterns before editing; returns claim_ids or conflicts."""
    claims = [{"type": "file", "pattern": p} for p in patterns]
    for sf in shared_files or []:
        claims.append({"type": "shared_file", "pattern": sf})
    body: dict[str, Any] = {
        "engineer": engineer,
        "branch": branch,
        "description": description,
        "claims": claims,
    }
    if ttl_hours is not None:
        body["ttl_hours"] = ttl_hours
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{_base_url()}/claims", json=body, headers={**_headers(), "Content-Type": "application/json"})
        if r.status_code in (400, 409):
            return r.json()
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def release_claims(claim_ids: list[str], engineer: str | None = None) -> dict[str, Any]:
    """Release claim IDs when work is finished."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{_base_url()}/claims/release",
            json={"claim_ids": claim_ids, "engineer": engineer},
            headers={**_headers(), "Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.json()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
