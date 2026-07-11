from __future__ import annotations

from pydantic import BaseModel, Field


class ClaimItem(BaseModel):
    type: str = Field(
        ...,
        description=(
            "file | shared_file. 'module' is a legacy value kept for "
            "direct HTTP API compatibility; it is not reachable via the "
            "MCP tools and behaves like a non-narrowable claim outside "
            "the shared-TTL rules."
        ),
    )
    pattern: str
    symbols: list[str] | None = Field(
        default=None,
        description=(
            "Optional symbol-level scope (v0.14+). When non-empty, the "
            "claim covers only the listed top-level symbols (functions, "
            "classes, types, etc.) within every file the pattern matches "
            "rather than the whole file. Two symbol claims on the same "
            "file with disjoint symbol sets auto-coexist (no 409). When "
            "absent or empty, the claim retains the legacy whole-file "
            "scope. See docs/design/sub-file-claims.md."
        ),
    )
    narrowable: bool | None = Field(
        default=None,
        description=(
            "Optional flag (v0.14+) controlling whether an incoming "
            "symbol-scope claim is allowed to auto-narrow this row. "
            "Defaults: file claims True, shared_file False, legacy module "
            "claims False, symbol-scope claims always non-narrowable. "
            "Explicit False on a normal file claim forces the legacy "
            "409+request flow."
        ),
    )


class ConflictingSymbol(BaseModel):
    file: str
    symbols: list[str]


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
            "(e.g. 'example-org/example-app'). Optional for backward compat; "
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
    wait_seconds: int | None = Field(
        default=None,
        ge=0,
        le=600,
        description=(
            "v0.21 FIFO queue knob. When set to a positive int and the "
            "request would 409, the service enqueues the requester behind "
            "the blocking holder and long-polls for up to ``wait_seconds`` "
            "seconds. If the holder releases within that window the "
            "service grants the next FIFO entry and returns the new claim "
            "ids; otherwise the original conflict payload is returned. "
            "wait_seconds=0 or None preserves the v0.13-v0.20 immediate-409 "
            "behaviour."
        ),
    )
    urgency: str | None = Field(
        default=None,
        description=(
            "v0.25 queue priority hint. One of 'low' | 'normal' | 'high' |"
            " 'blocking' (matches the v0.9 release-request urgency "
            "vocabulary). When set AND wait_seconds > 0 puts the queue "
            "entry the conflict path enqueues at the requested priority. "
            "Default None coerces to 'normal' on the wire so legacy "
            "behaviour (strict FIFO) is preserved. Unknown values "
            "silently coerce to 'normal' at the DB layer."
        ),
    )


class ClaimRefactorRequest(BaseModel):
    """v0.31 wave 2: ``POST /claims/refactor``.

    Asks the server to reserve a symbol's definition plus every
    callsite the language server can see, in one shot. The server
    resolves the definition span, runs ``textDocument/references``, and
    expands the result into a normal ``create_claims`` batch: a symbol
    claim on the tightest enclosing symbol of each reference, a file
    claim for references with no enclosing symbol, and always the
    definition symbol claim itself. Requires a live LSP
    (``COORD_LSP_ENABLED`` + ``COORD_REPO_ROOT``); otherwise the
    endpoint answers 503.
    """

    engineer: str
    file: str = Field(
        ...,
        description="Repo-root-relative path of the file defining the symbol.",
    )
    symbol: str = Field(
        ...,
        description=(
            "Canonical symbol path being refactored, in claim notation "
            "('handler', 'Outer::method', 'A::B::leaf')."
        ),
    )
    new_name: str | None = Field(
        default=None,
        description=(
            "Intended post-refactor name. Informational: it seeds the "
            "default description so other agents see what is coming. "
            "The server does not perform the rename."
        ),
    )
    wait_seconds: int | None = Field(
        default=None,
        ge=0,
        le=600,
        description=(
            "Forwarded to the underlying create_claims call: when the "
            "generated batch would 409, queue behind the blocking "
            "holder for up to this many seconds (v0.21 semantics)."
        ),
    )
    repo: str | None = None
    session_id: str | None = None
    branch: str | None = None
    description: str | None = Field(
        default=None,
        description=(
            "Optional override; defaults to 'refactor: rename <symbol> "
            "-> <new_name>' (or 'refactor: <symbol>' without new_name)."
        ),
    )
    ttl_hours: int | None = None
    urgency: str | None = None


class ConflictingClaim(BaseModel):
    id: str
    engineer: str
    pattern: str
    severity: str
    description: str | None = None
    expires_at: str
    scope_type: str | None = Field(
        default=None,
        description="'file' | 'symbol' (v0.14+). Absent for legacy responses.",
    )
    symbols: list[str] | None = Field(
        default=None,
        description="Symbol set the conflicting claim covers, when scope_type='symbol'.",
    )


class ConflictEntry(BaseModel):
    your_pattern: str
    conflicting_claim: ConflictingClaim
    overlap: list[str]
    your_symbols: list[str] | None = Field(
        default=None,
        description="Symbols you tried to claim on the overlapping file (v0.14+).",
    )
    symbol_overlap: list[ConflictingSymbol] | None = Field(
        default=None,
        description=(
            "Set when both sides are symbol-scope on the overlapping file. "
            "Empty list means symbol-disjoint (would have auto-coexisted)."
        ),
    )


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


class ConflictBatchRequest(BaseModel):
    """One-request conflict check used by the managed pre-push hook.

    The legacy ``GET /conflicts`` endpoint accepts repeated ``pattern`` query
    parameters, but a large push can exceed proxy/request-line limits.  This
    JSON form keeps the same service semantics while allowing thousands of
    paths in a bounded request body.
    """

    patterns: list[str] = Field(min_length=1, max_length=5000)
    engineer: str = Field(min_length=1)
    repo: str | None = None
    all_repos: bool = False
    session_ids: list[str] = Field(default_factory=list, max_length=1000)
    branch: str | None = None


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
    coexist_symbols: dict[str, list[str]] | None = Field(
        default=None,
        description=(
            "v0.35 symbol-scoped alternative to coexist_pattern. A dict "
            "mapping file_path -> list of symbol-path strings "
            "(e.g. {'src/auth.py': ['Login::handle']}) the requester is "
            "being granted a sibling claim on. The requester's new claim "
            "is created scope_type='symbol' with exactly these symbols, "
            "so a later third claim collides only on the granted symbols "
            "and auto-coexists elsewhere. Valid only when BOTH the "
            "holder's claim and the requester's original claim are "
            "symbol-scoped; the granted symbols must be a subset of the "
            "requester's claimed symbols and disjoint from the holder's "
            "(else 400). decision='coexist' requires coexist_pattern OR "
            "coexist_symbols."
        ),
    )


class PromoteHotspotRequest(BaseModel):
    """v0.21: write a hotspot pattern into the active owners.yaml.

    The dashboard surfaces hotspot files (v0.20) with a suggested
    action chip; v0.21 makes the suggestion actionable via POST
    /metrics/hotspots/promote. Idempotent: promoting an already-
    present pattern is a no-op.
    """

    action: str = Field(..., description="'shared_file' or 'split'")
    pattern: str = Field(..., min_length=1)
    # No ``repo`` field: ownership rules are global per coord instance
    # (the route is operator-only for exactly that reason), so accepting
    # and echoing a repo would misrepresent the write as repo-scoped.
    # Clients still sending the retired field are ignored (pydantic's
    # default extra-field handling).
    note: str | None = Field(
        default=None,
        description=(
            "Free-text note attached to a 'split' suggestion so future "
            "reviewers can see why the operator flagged this pattern."
        ),
    )


class QueuedRequestEntry(BaseModel):
    """v0.22: a row from the FIFO queue (claim_queue), joined with the
    blocking holder claim's engineer and pattern so the response can
    show ``who am I waiting on?`` without a second query.

    Surfaced via ``GET /requests?queued=true`` and the MCP
    ``my_requests(queued=True)`` wrapper. ``kind`` distinguishes
    queued rows from the existing request_events / requests rows the
    same endpoints serve when the filter is not set.
    """

    kind: str = Field(default="queued", description="Always 'queued'.")
    queue_id: str
    blocking_claim_id: str
    blocking_engineer: str | None = None
    blocking_pattern: str | None = None
    requester_engineer: str
    requester_pattern: str
    claim_type: str
    symbols: list[str] | None = None
    position: int
    state: str
    enqueued_at: str
    expires_at: str
    granted_claim_id: str | None = None
