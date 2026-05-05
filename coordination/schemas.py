from __future__ import annotations

from pydantic import BaseModel, Field


class ClaimItem(BaseModel):
    type: str = Field(..., description="module | file | shared_file")
    pattern: str


class CreateClaimsRequest(BaseModel):
    engineer: str
    branch: str | None = None
    description: str | None = None
    claims: list[ClaimItem]
    ttl_hours: int | None = None
    repo: str | None = Field(
        default=None,
        description=(
            "Identifier for the repo this claim came from "
            "(e.g. 'amittell/bastionx'). Optional for backward compat; "
            "supplied automatically by coord-mcp when set in the repo's config."
        ),
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "Per-MCP-process session id. When set, the conflict check "
            "self-excludes any active claim with the same session_id, "
            "even if its engineer name differs. coord-mcp generates one "
            "automatically at startup so subagents within the same "
            "Codex/Claude process don't block each other."
        ),
    )


class ConflictingClaim(BaseModel):
    id: str
    engineer: str
    pattern: str
    severity: str
    description: str | None = None
    expires_at: str


class ConflictEntry(BaseModel):
    your_pattern: str
    conflicting_claim: ConflictingClaim
    overlap: list[str]


class CreateClaimsResponse(BaseModel):
    claim_ids: list[str]
    conflicts: list[ConflictEntry] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    options: list[str] = Field(default_factory=list)


class ConflictCheckResponse(BaseModel):
    has_conflicts: bool
    conflicts: list[dict] = Field(default_factory=list)
    safe_to_proceed: bool
    safe: bool = True
    suggestion: str | None = None


class ReleaseClaimsRequest(BaseModel):
    claim_ids: list[str]
    engineer: str | None = None


class ExtendClaimRequest(BaseModel):
    engineer: str
    ttl_hours: int = 2


class FileRequestRequest(BaseModel):
    """A requester asking the holder of an active claim to release it."""

    claim_id: str
    requester: str
    session_id: str | None = None
    reason: str | None = None
    urgency: str = Field(
        default="normal",
        description="low | normal | high | blocking. Recorded for the audit trail; v0.9.0 doesn't yet vary the shortened-TTL window per urgency.",
    )
    wait_seconds: int = Field(
        default=60,
        ge=0,
        le=600,
        description=(
            "How long the server should hold this connection open waiting "
            "for the holder's decision. 0 = fire-and-forget (returns "
            "immediately with decision='pending'). The default 60s "
            "matches a typical holder poll cycle; longer values are fine "
            "but cap at 600s to bound HTTP-worker hold time."
        ),
    )


class RespondToRequestRequest(BaseModel):
    """The holder's decision on an open request."""

    decision: str = Field(..., description="approved | denied")
    engineer: str | None = None
    session_id: str | None = None
    note: str | None = None
