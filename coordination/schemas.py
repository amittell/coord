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
