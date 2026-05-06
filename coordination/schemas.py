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
    requested_scope: str | None = Field(
        default=None,
        description=(
            "What the requester actually needs, often a sub-pattern of "
            "the holder's claim pattern. Recorded for the audit trail "
            "(v0.11+) so 'holder claimed src/api/**, requester wanted "
            "src/api/auth.py' is reconstructible without parsing the "
            "free-text reason field. Used by the holder to decide "
            "whether 'narrowed' or 'coexist' is the right response."
        ),
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

    decision: str = Field(
        ...,
        description="approved | denied | narrowed | coexist",
    )
    engineer: str | None = None
    session_id: str | None = None
    note: str | None = None
    narrowed_pattern: str | None = Field(
        default=None,
        description=(
            "Required when decision='narrowed'. The new, tighter pattern "
            "the holder will keep claimed; the original claim is closed "
            "and a new claim is opened under this pattern (inheriting "
            "the holder's engineer / branch / repo / session / TTL). "
            "Must be a subset of the holder's current pattern; the "
            "service layer rejects disjoint or broader patterns."
        ),
    )
    coexist_pattern: str | None = Field(
        default=None,
        description=(
            "Required when decision='coexist'. The pattern the requester "
            "is being granted a sibling claim on. Both holder and "
            "requester end up with active claims, mutually self-excluded "
            "via claims.coexists_with, but still adversarial to anyone "
            "outside the pair. Useful when both agents want to edit "
            "different functions in the same file."
        ),
    )
