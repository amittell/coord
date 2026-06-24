# Changelog

All notable changes to this project are documented in this file.

The format is based on Keep a Changelog and this project adheres to
Semantic Versioning.

## [Unreleased]

### Fixed

- ``sessions.live`` stale-process rows are now pruned proactively instead
  of waiting for a later graceful shutdown. ``coord-mcp`` compacts the
  marker file under a repo-local lock on startup/shutdown, preserving the
  v0.12 append-race fix while removing dead PID rows before they become
  recurring ``coord doctor`` warnings. It also avoids unlocked appends
  while another process owns the compaction lock. ``coord doctor`` now
  uses the same lock and atomic write path to prune stale runtime rows
  immediately when it can rewrite the file.
- The symbol-parser remediation hint now uses the real PyPI package name,
  ``coord-mcp-server[symbols]``. Install and upgrade docs now preserve the
  ``symbols`` extra so parser environments do not drift back to regex
  fallback after package upgrades.

## [0.35.0] - 2026-06-24

### Added

- Symbol-level coexist. The ``coexist`` decision on
  ``respond_to_request`` now accepts a ``coexist_symbols`` dict
  (file -> list of symbol paths) so a holder can grant a requester
  specific disjoint symbols within a contested file, generalizing the
  file-scope ``coexist_pattern``. Validation happens at respond time
  (the holder is the trust boundary): both claims must be symbol-scoped,
  the granted symbols must be a subset of the requester's claim and
  DISJOINT from the holder's symbols (via the existing
  ``symbol_paths_overlap`` prefix rule), and the granted files must be
  files the holder actually claims -- so a coexist can neither hide a
  real symbol conflict nor reach outside the request's subject. The
  grant mints the requester a real symbol-scoped sibling claim with
  ``claim_symbols`` rows, and the conflict engine now evaluates a later
  third claim against a symbol-coexisting partner's granted symbols
  (409 on collision, auto-coexist when disjoint) instead of
  blanket-skipping the partner. File-scope ``coexist_pattern`` is
  unchanged. Schema v18 adds the nullable ``requests.coexist_symbols``
  column.

## [0.34.0] - 2026-06-24

### Added

- GitHub PR-comment integration. When a pre-push conflict (409) bounces
  a push, coord posts or updates a single de-duplicated comment on the
  pushing branch's open GitHub PR, naming the files that bounced and
  which engineer holds them, closing the "why is my push blocked" gap.
  The pushing branch is now forwarded from the pre-push hook to
  ``/conflicts`` and threaded into ``check_conflicts``. The bounce is
  emitted as a ``push_bounced`` event through the existing v0.27 webhook
  outbox, which gained a ``kind`` column (schema v17): the delivery loop
  dispatches ``kind='github'`` rows to a new ``github_adapter`` that
  resolves the branch to its open PR and find-or-updates a comment keyed
  by a hidden ``<!-- coord-bounce -->`` marker, reusing the outbox
  retry/backoff. Disabled by default: a complete no-op (no outbox row,
  no network, byte-identical ``/conflicts`` response) unless
  ``COORD_GITHUB_TOKEN`` is set, mirroring how ``COORD_WEBHOOK_URL``
  gates webhooks. New settings ``COORD_GITHUB_TOKEN`` /
  ``COORD_GITHUB_API_BASE`` (the latter for GitHub Enterprise).

## [0.33.0] - 2026-06-20

### Added

- Function-level (symbol) claims now cover 11 more languages beyond
  TypeScript/Python/Go: JavaScript (``.js``/``.jsx``), Rust (``.rs``),
  Java (``.java``), C (``.c``/``.h``), C++
  (``.cc``/``.cpp``/``.cxx``/``.hpp``/``.hh``), C# (``.cs``), Ruby
  (``.rb``), PHP (``.php``), Kotlin (``.kt``/``.kts``), Swift
  (``.swift``), and Scala (``.scala``/``.sc``). Each language ships a
  tree-sitter backend (gated on its optional grammar wheel via
  ``GRAMMAR_MODULE``, so it degrades to regex when the wheel is absent)
  plus a regex fallback, so symbol claims work with or without the
  ``symbols`` extra. Nested types and their methods use the canonical
  ``Outer::Inner::method`` parent path uniformly across languages, and
  LSP ``_command_for`` is wired for the languages with a standard
  language server. The new grammars are added to the ``symbols`` and
  ``dev`` extras only; the pinned production image is unaffected. (#29)

### Fixed

- The pre-push git hook no longer hard-fails when run from a linked git
  worktree. It resolved ``.coordination`` via ``git rev-parse
  --show-toplevel`` (the linked worktree root, which has no
  ``.coordination/``), so the push exec'd a nonexistent helper and was
  blocked. Both the ``.git/hooks/pre-push`` shim and the managed helper
  now resolve the MAIN worktree root via ``git rev-parse
  --git-common-dir`` and its parent, and the shim fails OPEN (exit 0)
  when the helper is missing rather than blocking a push. The helper's
  own conflict check stays fail-closed. Already-deployed shims self-heal
  on ``coord upgrade``. (#28)
- ``_repo_root_for_marker`` (which locates ``.coordination/`` for the
  ``sessions.live`` session marker) had the same linked-worktree blind
  spot and silently returned ``None`` from a linked worktree. It now
  resolves the main worktree root via ``--git-common-dir`` too, so
  session markers register correctly from any worktree.

## [0.32.5] - 2026-06-20

### Fixed

- Symbol (sub-file) claims no longer crash with ``No module named
  'tree_sitter_<lang>'`` when the optional native grammar wheel is absent.
  The tree-sitter backends import their grammar lazily (inside the parser
  getter), so ``auto`` mode committed to the tree-sitter backend -- its
  module imports fine -- and only hit the missing wheel at ``extract()``
  time, escaping the regex fallback and crashing the caller. The dispatcher
  now probes the grammar at selection time (``find_spec`` via each backend's
  new ``GRAMMAR_MODULE``), so ``auto`` correctly degrades to the regex
  backend and ``coord doctor`` (``probe_backend``) reports it accurately.
  ``COORD_SYMBOL_PARSER=treesitter`` still raises (loud-by-design). Tests:
  ``tests/test_symbols_fallback.py``.

## [0.32.4] - 2026-06-19

### Changed

- Dependency bumps in the pinned ``requirements.txt`` (Dependabot PR
  #25): ``anyio`` 4.13.0 -> 4.14.0, ``certifi`` 2026.5.20 ->
  2026.6.17, ``fastapi`` 0.137.0 -> 0.137.2, ``mcp`` 1.27.2 -> 1.28.0.
  Full suite green on the new versions across the matrix.

## [0.32.3] - 2026-06-19

### Changed

- Dependency bumps in the pinned ``requirements.txt`` (Dependabot PRs
  #23, #24), so the container image ships current runtime deps:
  ``cryptography`` 48.0.0 -> 49.0.0, ``fastapi`` 0.136.3 -> 0.137.0,
  ``starlette`` 1.2.1 -> 1.3.1. Full suite green on the new versions
  across the matrix.

## [0.32.2] - 2026-06-17

### Changed

- ``coord doctor`` no longer hard-fails on conditions that are not
  coordination breaks, so the fleet reads cleanly under the v0.32
  user-scoped MCP model. Root-cause fixes:
  - **Per-repo MCP config is optional when a user-scoped coord MCP
    server is registered.** doctor now detects a user-scoped server
    (any ``~/.claude.json`` ``mcpServers`` entry whose command runs
    ``coord-mcp``) and passes the MCP-config check ("via user-scoped
    coord MCP server") instead of FAILing on a missing local
    ``.mcp.json`` -- which v0.32 intentionally made untracked/optional.
    With neither a local config nor a user-scoped server the check is a
    WARN (the repo still works via the CLI/HTTP), not a hard FAIL.
  - **The coordination protocol block is found in CLAUDE.md *or*
    AGENTS.md** (a repo may use either regardless of ``config.tool``),
    and a missing block is a WARN, not a FAIL -- on a feature branch it
    arrives with the next merge from the default branch.
  - **Stale ``sessions.live`` dead-PID entries are a WARN**, matching
    their own long-standing "self-healing, harmless" description; they
    are pruned on read and never a coordination break.
- Net effect: ``coord doctor`` exits non-zero only when coordination is
  genuinely broken (uninitialized repo, server unreachable, auth
  failure, version skew, or a missing pre-push hook target). 4 new
  tests.

## [0.32.1] - 2026-06-16

### Fixed

- Windows: LSP callsite / refactor paths are now rendered repo-root
  relative on Windows. ``Path.relative_to`` compares path components
  case-sensitively, so on Windows it wrongly rejected paths that sit
  under the repo root but differ only in drive-letter or component
  case, falling back to an absolute path. That broke callsite
  enrichment, the callsite-overlap advisory, and ``claim_refactor``
  claim de-duplication on Windows (the ``ci`` workflow's
  ``windows-latest`` job had been red since v0.31.0). Replaced the
  three ``relative_to`` under-root checks (LSP reference normalization,
  symbol-claim validation, rename-sweep) with a shared
  ``relpath_under_root`` helper built on ``os.path.relpath`` over
  realpath'd operands (case-insensitive, cross-drive safe, and stable
  across the macOS ``/var`` symlink). No behavior change on Linux /
  macOS, where these paths already resolved correctly; prod is
  unaffected (it runs Linux with ``COORD_LSP_ENABLED`` off).
- Test harness: the fake LSP server accepts large fixtures via a
  ``FAKE_LSP_*_FILE`` env var (a JSON file path) in addition to the
  inline ``FAKE_LSP_*_JSON`` var, so the 200+ callsite cap test no
  longer overflows Windows' 32767-char environment-variable limit.

## [0.32.0] - 2026-06-14

### Changed

- coord's generated MACHINE config is no longer tracked in git. ``coord
  init`` / ``coord upgrade`` now add ``.mcp.json``, ``.cursor/mcp.json``
  and ``.codex/config.toml`` to the managed ``.gitignore`` block (next
  to ``/.coordination/``), and ``coord upgrade`` untracks any that an
  older coord version committed (``git rm --cached``, file kept on
  disk). Only the protocol docs (the ``CLAUDE.md`` / ``AGENTS.md``
  managed block, cursor rules) and the ``.gitignore`` block remain
  tracked. This removes the last way coord wiring could be swept into a
  contributor PR by a careless ``git add -A``. Real per-repo config
  continues to live only in the gitignored ``.coordination/local.env``.
- The commit-risk warning added in v0.31.2 now skips gitignored paths
  (via ``git check-ignore``), so the now-ignored ``.mcp.json`` is no
  longer flagged -- only genuinely committable wiring is named.

### Recommended setup

- Register the coord MCP server once, user-scoped, instead of relying
  on a per-repo ``.mcp.json``::

      claude mcp add --scope user coord coord-mcp

  ``coord-mcp`` resolves each repo's URL / token / repo id from that
  repo's ``.coordination/local.env`` at startup, so one user-scoped
  server works across every repo and no tracked MCP config file exists
  to leak into a PR. See ``docs/integrations/claude-code.md``.

## [0.31.2] - 2026-06-14

### Added

- ``coord init`` / ``coord upgrade`` now warn when they write coord's
  TRACKED wiring (``.mcp.json``, the CLAUDE.md / AGENTS.md managed
  block, ``.gitignore``, etc.) on a non-default branch or with staged
  changes already in the index -- the exact conditions where a later
  ``git add -A && commit`` sweeps the wiring into an unrelated pull
  request (the failure mode behind earlier polluted PRs). The warning
  names only the committable files (gitignored ``.coordination/`` and
  ``.git/`` entries are excluded) and prints the safe ``git add`` for
  staging coord's wiring by itself. It is advisory, never blocking, so
  legitimately onboarding a repo via a PR still works. New
  ``cli_init`` helpers ``_current_branch`` / ``_default_branch`` /
  ``_has_staged_changes`` / ``_warn_tracked_wiring_commit_risk``.

## [0.31.1] - 2026-06-14

### Fixed

- ``coord init`` / ``coord upgrade`` now exempt coord's generated
  machine config from a repo's Prettier format check. The generated
  ``.mcp.json`` (and ``.cursor/mcp.json``) use 2-space JSON with
  placeholder values; a repo whose CI runs ``prettier --check`` would
  fail on it (and every later ``coord upgrade`` would re-break a
  previously green build). Onboarding now detects Prettier usage (a
  ``.prettierrc*`` / ``prettier.config.*`` file, an existing
  ``.prettierignore``, or a ``prettier`` key/dependency in
  ``package.json``) and adds the generated config to a managed block in
  ``.prettierignore`` -- the same treatment ``package-lock.json``
  already gets. No-op for repos that do not use Prettier, so onboarding
  never drops a stray ``.prettierignore`` into an unrelated repo. New
  ``cli_shared`` helpers ``repo_uses_prettier`` and
  ``ensure_prettierignore_entries``; 12 new tests.

## [0.31.0] - 2026-06-12

### Added

- LSP-aware symbol claims, flag-gated behind ``COORD_LSP_ENABLED``
  (default off, tree-sitter behavior unchanged when off). Coord
  spawns language servers as child processes (``pylsp``,
  ``typescript-language-server``, ``gopls``; commands overridable
  via ``COORD_LSP_COMMAND_*``), speaks JSON-RPC over stdio, reaps
  idle servers, and trips a per-server circuit breaker on failure
  -- LSP can upgrade symbol resolution but can never make claim
  creation fail or deny a symbol the parser accepted.
- Schema migration v16: ``claim_symbols`` gains definition spans
  (``start_line``/``start_col``/``end_line``/``end_col``, lines
  1-based, columns 0-based) plus ``resolved_by``
  (``parser``/``lsp``); new ``claim_symbol_callsites`` and
  ``claim_symbol_renames`` tables. Parser spans persist on every
  symbol claim when ``COORD_REPO_ROOT`` is set, LSP refines them
  when enabled, and the dashboard renders symbol ranges
  (``file.py::sym (lines 10-42, lsp)``).
- Callsite-aware overlap (advisory): granted symbol claims record
  their callsites via ``textDocument/references`` in a background
  enrichment pass (capped at 200 per claim). When a later claim by
  a different engineer covers recorded callsites of an active
  holder, the grant still succeeds and carries an advisory warning
  naming the holder and the overlap -- semantic conflicts surface
  without hard-blocking, because callsite data goes stale.
- Symbol rename auto-follow: a bounded background sweep re-checks
  active symbol claims; when a claimed symbol vanished and exactly
  one same-kind same-parent symbol overlaps its stored span, the
  claim follows the rename atomically (symbol row, spans, pattern,
  audit row in ``claim_symbol_renames``, ``symbol_renamed`` webhook
  event, dashboard note). Ambiguity means no action.
- ``templates/skills/coordinating-file-claims/``: an Agent Skill
  (SKILL.md, agentskills.io format, portable across Claude Code,
  Codex CLI, Cursor and other skill-capable agents) that teaches an
  agent to install, configure, and use coord end to end -- the
  claim/release protocol, symbol claims, queueing, conflict
  negotiation, and error recovery.
- ``POST /claims/refactor`` + MCP tool ``claim_refactor``: expands
  a (file, symbol) refactor intent into one normal claims batch --
  the definition symbol plus the enclosing symbol of every callsite
  (file claim for module-level references), deduplicated and
  capped. Conflicts, queueing (``wait_seconds``) and v0.30 rate
  limits apply unchanged; 503 when no language server can answer,
  because refactor claims are meaningless without references.

## [0.30.0] - 2026-06-12

### Added

- Per-engineer rate limiting, all disabled by default (0):
  ``COORD_MAX_CLAIMS_PER_ENGINEER`` caps an engineer's
  simultaneously active claims; a request that would push past the
  cap gets HTTP 429 with a ``Retry-After`` header computed from the
  engineer's soonest claim expiry (clamped 5s-1h).
  ``COORD_MAX_QUEUED_PER_ENGINEER`` caps an engineer's live
  (waiting or in-progress) queue entries at enqueue time.
  ``COORD_MAX_QUEUE_DEPTH_PER_REPO`` refuses new ``wait_seconds``
  requests against a repo whose waiting queue is at capacity, with
  a service-degraded hint -- pushback surfaces at the API instead
  of letting waiters pile up.
- The 429 body is structured (``detail``, ``scope``,
  ``retry_after``) and the MCP ``claim_files`` tool surfaces it as
  data the agent can reason about, mirroring how 409 conflicts are
  surfaced.
- An at-cap engineer can still QUEUE work: the active-claim cap is
  enforced where claims are inserted, not where requests arrive,
  so queueing for future capacity keeps working. When a queue
  grant would blast through the cap, the drain loop expires that
  entry (logged with the reason) and continues to the next waiter
  -- a rate-limited waiter can never wedge the queue.

### Notes

- Limits key on the request-body engineer (the worker identity
  claims are stored under). A malicious holder of a valid token
  can spread load across invented engineer names; closing that
  requires a future migration that records the authenticated token
  identity on claim rows. The audit trail makes invented-name
  abuse visible in the meantime.
- The ``X-Coord-Queue-Depth`` backpressure header is unchanged
  (waiting-only count). Quota checks use separate counters.

## [0.29.6] - 2026-06-12

### Added

- OIDC SSO for the dashboard. Configure ``COORD_OIDC_ISSUER`` /
  ``COORD_OIDC_CLIENT_ID`` / ``COORD_OIDC_CLIENT_SECRET`` /
  ``COORD_OIDC_REDIRECT_URI`` and the login page grows a "Sign in
  with SSO" link. The flow is a standard authorization code +
  PKCE exchange: discovery document and JWKS are fetched from the
  issuer (cached, kid-rotation aware), the ID token is validated
  (RS256/ES256 allowlist, issuer, audience, azp, expiry with 60s
  leeway, nonce), and the identity claim
  (``COORD_OIDC_ENGINEER_CLAIM``, default ``email``) maps to a
  coord engineer name, optionally prefixed via
  ``COORD_OIDC_ENGINEER_PREFIX``.
- A successful SSO login mints a real per-engineer token
  (description ``oidc sso login``) expiring with the dashboard
  session lifetime, and continues through the existing cookie
  machinery: the entire v0.29.4/v0.29.5 surface (expiry
  enforcement, activity tracking, the dashboard token panel,
  self-service revoke) applies to SSO sessions with no second
  auth path. Operators see SSO logins as ordinary token rows.
- Login state (state, nonce, PKCE verifier) travels in a
  short-lived HMAC-signed cookie keyed on the client secret, so
  the flow is stateless and works across replicas without sticky
  sessions.

### Security

- Fail-closed principal policy: a known-public issuer
  (accounts.google.com) with no ``COORD_OIDC_ALLOWED_PRINCIPALS``
  allowlist refuses SSO logins unless
  ``COORD_OIDC_ALLOW_ANY_PRINCIPAL=true`` is set explicitly --
  "any Google account may administer my coordination server" is
  never an accident. ``email_verified: false`` identities are
  rejected when mapping by email; ``alg=none`` and non-allowlisted
  algorithms are rejected before signature work; issuers must be
  HTTPS (localhost excepted for development).

## [0.29.5] - 2026-06-12

### Added

- In-dashboard token management. The dashboard grows an "engineer
  tokens" panel: a per-engineer session (logged in with a
  per-engineer token) sees and manages its own tokens; a shared-token
  session acts as operator and sees every engineer's tokens. Rows
  show short id, status (``active`` / ``rotating`` /
  ``grace-elapsed`` / ``expired`` / ``revoked`` -- the same
  vocabulary as ``coord tokens list``), creation/last-use/expiry
  timestamps, request count, and last source IP. Every non-revoked
  row carries an inline revoke action; a create form mints new
  tokens with optional ``expires-in`` (v0.29.4 duration grammar).
- ``POST /dashboard/tokens/create`` returns a one-time page showing
  the raw token exactly once (``Cache-Control: no-store``); the raw
  value is never logged or re-renderable. ``POST
  /dashboard/tokens/revoke`` is PRG (303 back to the dashboard) and
  idempotent.
- Self-service guardrails: a per-engineer session can only create
  tokens for itself (the submitted engineer field is ignored) and
  only revoke its own tokens (atomically scoped in SQL). If the
  session's own token has an expiry, self-minted tokens must expire
  no later -- a holder of an expiring credential cannot mint
  themselves an immortal one. Operator sessions are uncapped.
  Insecure no-auth sessions cannot manage tokens at all.
- New ``coordination/tokens.py`` pure helper module (token
  generation, hashing, status derivation) shared by the CLI and the
  dashboard; ``coordination/db.py`` gains
  ``get_engineer_token_by_id`` and an optional atomic ``engineer=``
  scope on ``revoke_engineer_token``.

### Security

- CSRF protection for state-changing dashboard operations. A
  ``coord_csrf`` double-submit cookie (HttpOnly, SameSite=Lax,
  Secure behind TLS/proxies, rotated on login, cleared on logout)
  must match the hidden ``csrf_token`` form field on ``POST
  /dashboard/tokens/create``, ``POST /dashboard/tokens/revoke`` and
  ``POST /dashboard/logout``; mismatches get a 403 with no state
  change. ``POST /dashboard/login`` is deliberately exempt so the
  documented curl login probe keeps working; it instead gets a soft
  Origin guard (a present-but-cross-site ``Origin`` header is
  rejected; absent ``Origin`` -- curl -- passes), which closes
  browser-based login CSRF without breaking scripts.

## [0.29.4] - 2026-06-12

### Added

- Schema migration v15: token lifecycle columns on
  ``engineer_tokens``. ``expires_at`` makes a token
  self-terminating (NULL keeps legacy never-expires semantics);
  ``rotated_from`` links a successor token to the token it
  replaced; ``rotation_grace_until`` on the old token marks it as
  rotated while keeping it valid until the window closes;
  ``request_count`` / ``last_source_ip`` / ``last_user_agent``
  give operators last-state activity per token without a
  per-request history table.
- ``coord tokens create --expires-in 30d``: tokens can now be
  minted with an expiry. Durations accept ``m``/``h``/``d``/``w``
  units. The auth path rejects expired tokens with a 401 that
  names the expiry timestamp and points at ``coord tokens
  rotate`` / ``coord tokens create``.
- ``coord tokens rotate <token-id> [--grace 24h] [--expires-in
  30d]``: zero-downtime rotation. Mints a successor token for the
  same engineer and keeps the predecessor valid through the grace
  window so cached copies survive the swap; after the window the
  old token gets a 401 explaining it was rotated. Rotation
  refuses revoked, expired, and already-rotated tokens -- a
  rotation can never revive a dead credential. Insert-successor
  and set-grace happen in one transaction.
- Per-token activity tracking: every successful per-engineer auth
  bumps ``request_count`` and records the last source IP
  (``CF-Connecting-IP``, else first hop of ``X-Forwarded-For``,
  else the socket peer) and user agent, best-effort and truncated.
  ``coord tokens list`` surfaces a derived status word
  (``active`` / ``rotating`` / ``grace-elapsed`` / ``expired`` /
  ``revoked``) plus expiry, request count, last-used and last-IP
  columns; ``--json`` carries the raw fields.
- New ``Database.resolve_engineer_token`` diagnostic resolver
  returns ``ok`` / ``expired`` / ``rotation_grace_elapsed`` so
  the auth layer can emit actionable 401 hints.
  ``lookup_engineer_token`` keeps its valid-only contract.

### Changed

- The per-engineer -> shared -> require-flag auth pipeline was
  triplicated across ``require_auth``, ``GET /dashboard``, and
  ``POST /dashboard/login``; it now lives in a single
  ``_authenticate_bearer`` helper, so the expiry/rotation/activity
  logic exists exactly once. The dashboard login form now shows
  the specific failure hint (expired vs rotated vs invalid)
  instead of a generic invalid-token banner.
- Per-engineer-only deployments are now legal: with
  ``COORD_REQUIRE_PER_ENGINEER_TOKEN=true`` the service boots and
  serves without any ``COORD_AUTH_TOKEN`` set. ``auth_mode`` (in
  ``/readyz`` and ``/meta``) reports ``per_engineer`` in that
  configuration. Previously the server refused to start and
  ``require_auth`` answered 500 until a shared token was
  configured even when nothing was meant to use it.

## [0.29.3] - 2026-06-09

### Security

- v0.29.2 added ``X-Forwarded-Proto`` awareness to the cookie
  Secure flag, but real-world testing against the production
  Cloudflare Tunnel + Traefik stack showed the header gets
  rewritten to ``http`` at the Traefik hop (Traefik's default
  ``forwardedHeaders.trustedIPs`` does not include the
  cloudflared pod IP, so the proxy-injected header is stripped
  for safety). The cookie was still shipping without ``Secure``.

  Two new signals added to ``_request_uses_https`` so the Secure
  flag fires in real proxy chains:

  1. ``CF-Visitor: {"scheme":"https"}`` -- Cloudflare adds this
     at the edge; cloudflared and Traefik pass arbitrary headers
     through untouched, so this signal survives the chain
     intact. Cloudflare guarantees its presence on every proxied
     request.
  2. ``COORD_DASHBOARD_COOKIE_FORCE_SECURE=true`` -- operator
     escape hatch for stacks that strip both proxy headers, or
     for any future proxy chain where the auto-detection still
     misses.

  Two new regression tests pin the CF-Visitor path (JSON happy
  case + mangled-JSON soft fail) and the force-secure override.
  Plus the v0.29.2 ``X-Forwarded-Proto`` test still passes.

## [0.29.2] - 2026-06-09

### Security

- ``coord_session`` cookie set by ``POST /dashboard/login`` now
  honours the ``X-Forwarded-Proto`` header when deciding the
  ``Secure`` attribute. Pre-fix the code only checked
  ``request.url.scheme``, which always reads ``http`` behind a
  TLS-terminating proxy (Cloudflare's edge, Traefik's edge, an
  AWS ALB, etc.). The cookie was being set without ``Secure`` in
  production, which meant the browser was technically willing to
  send it over plain HTTP. No exploit path today because
  ``coord.mittell.ai`` is HTTPS-only at the Cloudflare edge, but
  the missing flag was a defense-in-depth miss that this release
  closes.

  A new helper ``_request_uses_https`` checks both the immediate
  transport and the first hop of ``X-Forwarded-Proto``. Three new
  regression tests cover the contract: header present sets
  Secure, header absent keeps it off (so local dev over
  ``http://127.0.0.1`` stays functional), and comma-separated
  proxy chains use the first hop.

## [0.29.1] - 2026-06-08

### Fixed

- ``packaging`` is declared as a runtime dep in ``pyproject.toml``
  (since v0.27.2) but was never added to ``requirements.txt``.
  The Dockerfile builds the image by ``pip install -r
  requirements.txt`` and then ``pip install --no-deps .``, so the
  pyproject runtime deps are deliberately ignored -- the image
  was relying on ``packaging`` being pulled in transitively by
  some other runtime dep. PR #17's bumps (v0.28.4) broke that
  transitive chain, but the consequence only surfaced when the
  v0.29.0 image actually tried to import ``coordination.cli_doctor``
  (which imports ``packaging.version``) and crashed at module
  load. ``coord --version``, ``coord tokens ...``, ``coord
  doctor`` all failed in the production pod with
  ``ModuleNotFoundError: No module named 'packaging'``.

  Fix: pin ``packaging==26.2`` in ``requirements.txt``. The
  runtime server (``coord-api``) was unaffected (it never
  imports ``packaging``), which is why ``/readyz`` kept returning
  HTTP 200 the whole time; the bug only blocked operator-facing
  CLI work inside the pod.

## [0.29.0] - 2026-06-06

First minor version bump since v0.28.0. Brings per-engineer
bearer tokens to retire the single shared ``COORD_AUTH_TOKEN``,
plus a real login form on ``/dashboard`` that replaces the JSON
401 browsers used to see. The shared token still works by
default; flip ``COORD_REQUIRE_PER_ENGINEER_TOKEN=true`` to reject
it cluster-wide once every engineer has been migrated.

### Added

- Schema migration v14: ``engineer_tokens`` table. Tokens are
  stored as ``sha256(raw_token)`` only; the raw value is returned
  exactly once at creation time. Columns: ``id`` (UUID),
  ``engineer``, ``token_sha256`` (unique index), optional
  ``description``, ``created_at``, ``revoked_at``,
  ``last_used_at``. Unique index on the hash gives the auth path
  O(1) lookups.
- Five new ``Database`` methods covering the lifecycle:
  ``create_engineer_token``, ``lookup_engineer_token``,
  ``touch_engineer_token``, ``list_engineer_tokens``,
  ``revoke_engineer_token``.
- ``coord tokens create / list / revoke`` CLI. Tokens are
  prefixed ``coordt_`` for grep-ability in CI logs and clipboard
  scanners. ``list`` is metadata-only; the raw token is never
  recoverable after creation. ``revoke`` is idempotent.
- ``Settings.require_per_engineer_token`` (env:
  ``COORD_REQUIRE_PER_ENGINEER_TOKEN``). False by default;
  switching to True is the migration kill switch -- after that
  point only rows in ``engineer_tokens`` authenticate.
- ``Settings.dashboard_session_lifetime_sec`` (env:
  ``COORD_DASHBOARD_SESSION_LIFETIME_SEC``). Default 28800 (8h).
- Dashboard login UI: ``GET /dashboard`` renders an HTML login
  form when no auth is present (instead of returning JSON 401).
  ``POST /dashboard/login`` validates the token and sets a
  ``coord_session`` cookie (HTTPOnly, SameSite=Lax, Secure when
  the request itself is over HTTPS, ``max_age =
  COORD_DASHBOARD_SESSION_LIFETIME_SEC``). ``POST
  /dashboard/logout`` clears the cookie and 303s back to the
  login form.
- 32 new tests across four files cover the contracts:
  ``test_engineer_tokens.py`` (db layer), ``test_cli_tokens.py``
  (CLI), ``test_auth_per_engineer.py`` (HTTP auth),
  ``test_dashboard_login.py`` (browser login flow).

### Changed

- ``require_auth`` middleware now resolves the bearer from
  either the ``Authorization`` header or the ``coord_session``
  cookie; the header always wins so an operator debugging with
  curl can override a stale browser cookie. The lookup tries
  per-engineer tokens first, then falls back to the shared
  ``COORD_AUTH_TOKEN`` unless
  ``COORD_REQUIRE_PER_ENGINEER_TOKEN`` is set.
- Successful per-engineer auth attaches ``request.state.engineer``
  to the request so downstream handlers can read the
  authenticated engineer without re-deriving it. ``auth_kind`` is
  also attached (``per_engineer`` or ``shared``).

### Security

- ``last_used_at`` is bumped opportunistically on successful
  per-engineer auth so operators can spot stale tokens via
  ``coord tokens list`` and revoke them. The touch path swallows
  exceptions to keep transient lock contention from blocking
  request auth.
- A revoked token's row stays in the table so ``coord tokens
  list --include-revoked`` answers ``which tokens did this
  engineer ever hold, when were they revoked?``. The matching
  ``lookup_engineer_token`` query filters out revoked rows so
  the bearer stops authenticating on the next request.

## [0.28.4] - 2026-06-06

Rollup release of post-v0.28.3 dependency updates and a CI flake
fix. No code-level behaviour change; the runtime version moves so
the production image picks up the new transitive deps and the
manifest auto-bumper deploys them.

### Fixed

- Three FIFO queue ordering tests in ``tests/test_api.py``
  (``test_queue_grants_in_fifo_order_on_release``,
  ``test_queue_priority_blocking_jumps_ahead``,
  ``test_queue_priority_default_normal_preserves_fifo``) used
  ``await asyncio.sleep(0.05)`` to enforce enqueue ordering
  before the holder release fired. The 50ms delay was reliable on
  Linux/macOS but race-flaky on Windows's coarser scheduler --
  CI on main had been red since v0.28.1. Switched to the existing
  ``_wait_for_queue_id`` helper that polls
  ``GET /requests?queued=true`` until each row is observable,
  which removes the timing dependency.
- ``requirements.txt`` had inconsistent ``pydantic==2.13.4`` and
  ``pydantic_core==2.47.0`` pins after PR #17's group bump.
  pydantic 2.13.4 strictly requires pydantic_core==2.46.4;
  reverting the core pin makes the docker build smoke step
  resolve cleanly again.

### Changed (dependency bumps, runtime)

- cryptography 47.0.0 -> 48.0.0 (PR #20)
- rpds-py 0.30.0 -> 2026.5.1 (PR #19)
- fastapi 0.136.0 -> 0.136.3
- uvicorn 0.45.0 -> 0.49.0
- pydantic 2.13.3 -> 2.13.4
- pydantic-settings 2.14.0 -> 2.14.1
- mcp 1.27.0 -> 1.27.2
- pathspec 1.0.4 -> 1.1.1
- certifi 2026.2.25 -> 2026.5.20
- click 8.3.2 -> 8.4.1
- httptools 0.7.1 -> 0.8.0
- idna 3.11 -> 3.18
- pyjwt 2.12.1 -> 2.13.0
- python-multipart 0.0.26 -> 0.0.32
- sse-starlette 3.3.4 -> 3.4.4
- starlette 1.0.0 -> 1.2.1
- watchfiles 1.1.1 -> 1.2.0

### Changed (dev / CI)

- tree-sitter dev extra: ``>=0.24.0`` -> ``>=0.25.2`` (PR #21)
- Two GitHub Actions SHAs bumped via the actions-minor-and-patch
  group (PR #16): ``actions/checkout`` v6.0.2 -> v6.0.3, etc.

## [0.28.3] - 2026-06-05

### Security

- ``coord init`` and ``coord upgrade`` no longer write the real service
  URL, bearer token, or repo identifier into the tracked MCP
  templates (``.mcp.json``, ``.codex/config.toml``, ``.cursor/mcp.json``).
  Pre-fix, an ``upgrade`` against a remote-mode config would leak the
  64-hex-char ``COORD_AUTH_TOKEN`` from ``.coordination/local.env``
  back into the public-safe template, which would have committed a
  real credential the next time someone ran ``git add .``. The leak
  was caught at working-tree time by ``tests/test_deploy_overlay.py``
  and never landed on main, but the regression existed since
  before v0.14.
  Tracked templates now always carry the documented placeholders
  (``http://127.0.0.1:8080``, ``set-me``, ``example-org/example-repo``);
  the MCP wrapper's ``_load_local_env`` resolves them at startup
  against the gitignored ``.coordination/local.env``, which continues
  to hold the real values. Two new regression tests pin the contract:
  ``test_upgrade_never_writes_real_token_into_tracked_mcp_json`` and
  ``test_upgrade_never_writes_real_token_into_codex_config``.

### Fixed

- ``coord doctor``'s ``.mcp.json / .codex/config.toml / .cursor/mcp.json
  token matches local.env`` check now treats the documented
  ``set-me`` placeholder as a match. The check was reporting FAIL
  whenever the tracked template held the correct placeholder
  (because the MCP wrapper resolves it from local.env at startup --
  the wrapper does this on every spawn). False alarm removed.

### Added

- ``docs/deployment.md`` now has a "Transport security (TLS)" section
  covering five operator-pickable patterns: plaintext (default),
  Cloudflare Tunnel + Universal SSL, Let's Encrypt + cert-manager
  with DNS-01 challenge, self-signed CA with cert distribution, and
  the dual-access hybrid (Cloudflare for off-LAN access plus LAN-direct
  HTTP for local agents). Each option carries threat-model coverage,
  step-by-step setup, pros/cons, and a decision tree at the bottom.

### Changed

- ``coordination.cli_init._update_mcp_json`` and
  ``coordination.cli_init._update_codex_config`` are now zero-argument
  helpers (except ``path``); the previous ``service_url``, ``token``,
  and ``repo_id`` parameters were exactly the leak vector. Internal-only
  signature change; no public API affected.
- New module constants ``PLACEHOLDER_API_URL``,
  ``PLACEHOLDER_AUTH_TOKEN``, ``PLACEHOLDER_REPO_ID`` in
  ``coordination.cli_init`` make the placeholder values discoverable
  to test code and future surfaces.

## [0.28.2] - 2026-06-05

### Fixed

- ``coordination.__version__`` is now sourced from
  ``importlib.metadata.version("coord-mcp-server")`` instead of being
  hand-maintained alongside ``pyproject.toml``. v0.28.1 shipped with
  the two out of sync (``__version__ = "0.28.0"`` baked into the
  v0.28.1 container image) so ``/readyz`` reported the wrong version
  after the bump-manifest job rolled out. With this change there is
  one source of truth at runtime, and the v0.28.2 image reports
  ``0.28.2`` correctly through ``/readyz``, the ``coord --version``
  banner, the dashboard footer, ``coord doctor``, and the update
  notice banner.

### Added

- ``tests/test_version_consistency.py`` pins the three-way contract:
  ``coordination.__version__`` == ``importlib.metadata`` metadata ==
  ``pyproject.toml`` ``[project].version``. CI fails fast on the next
  release pipeline if any pair drifts again. The third assertion
  guards that the value is a clean PEP 440 string so the workflow's
  ``Version()`` parse cannot break either.

## [0.28.1] - 2026-06-05

### Added

- ``bump-manifest`` job in ``.github/workflows/release.yml``. Runs
  after ``publish-image`` succeeds on real tag pushes and rewrites
  ``deploy/k8s/prod/deployment.yaml`` so the kebabrack live overlay
  picks up the image digest just published. Skipped on
  ``workflow_dispatch`` (manual rebuilds + release candidates
  should not silently flip production). Commit message includes
  ``[skip ci]`` so the manifest bump does not retrigger the full
  CI matrix; ArgoCD watches git directly and reconciles regardless.

### Fixed

- ``deploy/k8s/prod/deployment.yaml`` had been pinned at v0.13.0
  since that release. The kebabrack live overlay drifted 14
  versions behind because the release workflow built and pushed
  the GHCR image but never updated the manifest. The companion
  ops commit catches the live cluster up to v0.28.0; the new
  bump-manifest job prevents the drift from recurring.

### Changed

- ``.gitignore`` excludes ``uv.lock`` (development artifact when
  contributors run ``uv venv`` instead of ``python -m venv``).

## [0.28.0] - 2026-06-05

### Added

- Backpressure response header. Every authenticated response
  includes ``X-Coord-Queue-Depth: N`` when the request carries an
  engineer signal (X-Coord-Engineer header or ``engineer`` query
  param). N counts that engineer's currently-queued waiting
  claims. Toggle via COORD_BACKPRESSURE_HEADER.
- Queue fairness pass. Every COORD_QUEUE_FAIRNESS_INTERVAL-th
  call (default 10) to ``db.pop_next_waiting_queue_entry``
  bypasses the priority CASE and pops by raw FIFO position.
  Anti-starvation guarantee for low/normal-priority waiters.
  Set to 0 to disable.
- Priority decay. Counterpart to the v0.26 age boost. A waiting
  entry's effective priority drops one level per
  COORD_QUEUE_PRIORITY_DECAY_SEC seconds (blocking -> high ->
  normal -> low, floor at low). Prevents misclassified urgent
  requests from monopolising the queue head. Default 300;
  set to 0 to disable.
- Stale engineer housekeeping. New ``coord engineers stale
  [--release]`` subcommand surfaces engineers whose most-recent
  activity is older than COORD_STALE_ENGINEER_DAYS (default 7).
  ``--release`` drops their lingering active claims. Dashboard
  panel shows the same data. db.list_stale_engineers helper.
- ~15 new tests covering the four features.

### Changed

- v0.28 originally targeted multi-namespace coordination
  (per-repo ownership rules, cross-repo blocks, per-team views,
  multi-tenant tokens; see docs/design/multi-namespace.md). That
  work is deferred to a later release pending actual multi-tenant
  demand. v0.28.0 pulls in four low-hanging queue QoS +
  housekeeping items from v0.29 + the future bucket instead.

## [0.27.2] - 2026-06-04

### Fixed

- Missing runtime dependency on ``packaging``. ``cli_doctor`` and
  ``cli_update_notice`` import ``from packaging.version`` for SemVer
  parsing, but the package was not declared in
  ``[project.dependencies]``. Fresh ``pip install coord-mcp-server``
  installs hit ``ModuleNotFoundError: No module named 'packaging'``
  on the first ``coord`` invocation. The dev venv masked this because
  ``pip`` itself pulls in ``packaging`` transitively, but a clean
  user install does not. Added ``packaging>=23.0`` to the runtime
  dependency list. Surfaced by the first successful v0.27.1 PyPI
  publish and a clean-venv install smoke test.

## [0.27.1] - 2026-06-03

### Added

- ``coord outbox`` CLI for v0.27 webhook outbox management.
  - ``coord outbox stats`` shows per-event-type counts
    (delivered / failed / pending / exhausted) over a rolling window
    (default 24h, ``--hours N`` to change). ``--json`` emits a
    machine-readable payload mirroring the dashboard panel.
  - ``coord outbox tail -n N`` shows the N most recent rows
    (default 20), oldest-first within the slice, with status,
    event_type, event_age, and a truncated last_error. ``--json``
    emits the row list verbatim.
  - ``coord outbox retry [--exhausted | --failed | --all]`` resets
    ``retry_count`` to 0 and ``next_attempt_at`` to now on selected
    rows, clearing ``last_error`` and flipping status back to
    ``pending`` so the delivery loop picks them up.
  - ``coord outbox purge [--delivered | --exhausted | --all-terminal]``
    DELETEs rows in terminal states. ``--dry-run`` is available on
    both mutators and reports the row count without writing.

## [0.27.0] - 2026-06-03

### Added

- Webhook notification primitive. Set COORD_WEBHOOK_URL to a
  receiver endpoint and the conflict pipeline writes every emitted
  event (auto-coexist, auto-narrow, auto-promote,
  auto-promote-subtree, auto-demote, claim_granted, queue_grant,
  queue_cancel) to a new webhook_outbox table (schema v13). A
  background delivery loop POSTs each row with an HMAC-SHA256
  signature header (X-Coord-Signature) computed using
  COORD_WEBHOOK_SECRET. Retries with exponential backoff capped
  at COORD_WEBHOOK_MAX_RETRIES (default 5), then marks the row
  exhausted. Filter the event stream with COORD_WEBHOOK_EVENTS.
- New settings: webhook_url, webhook_secret, webhook_events,
  webhook_max_retries, webhook_retry_backoff_sec,
  webhook_delivery_interval_sec.
- Service.fire_webhook(event_type, detail) helper called at every
  event-emission site. Service.deliver_pending_webhooks runs the
  delivery loop. Five db helpers: enqueue_webhook,
  list_pending_webhooks, mark_webhook_delivered,
  mark_webhook_failed, webhook_delivery_stats.
- Dashboard "webhook delivery (24h)" panel showing per-event-type
  delivery counts (delivered / failed / pending / exhausted).
- 10+ new tests covering signature correctness, retry behaviour,
  exhaustion, filter, event-site emission, and dashboard render.

### Roadmap

- Slack adapter, GitHub PR comments, and an outbox retry CLI are
  queued as v0.27.x follow-ups.

## [0.26.0] - 2026-06-03

### Added

- Pattern-class granularity in hard auto-promote. When
  COORD_AUTO_PROMOTE_SUBTREE_MIN_FILES (default 3) or more
  auto-promoted files share a directory ancestor, coord writes
  the subtree glob (e.g. ``src/auth/**``) once instead of N
  individual entries. Subtree audit events record the source
  patterns + source_count for traceability. Set to 0 to disable.
- Priority age boost. db.pop_next_waiting_queue_entry now factors
  age into the priority ordering: an entry waiting longer than
  COORD_QUEUE_AGE_BOOST_SECONDS (default 60s) is treated as one
  priority level higher. Recomputed per pop -- no extra writes,
  no separate sweep. Set to 0 to disable.
- Queue cancellation API. DELETE /requests/{queue_id} marks a
  waiting queue entry cancelled and wakes its in-process long-poll
  immediately. Optional ?engineer= query scopes the cancellation
  to that engineer (prevents cross-engineer interference).
- New service.cancel_queue_request method + db.cancel_queue_entry
  helper.
- New coord-mcp cancel_queue_request(queue_id, engineer=) tool.

## [0.25.0] - 2026-06-02

### Added

- Permanent shared-file pin. Operators append
  ``# coord-managed=permanent`` to a shared_files line in
  owners.yaml; the v0.23 auto-demote sweep skips any entry carrying
  this marker even when the rolling hotspot count drops to zero.
  Entries can carry BOTH the auto-promoted=DATE and the
  coord-managed=permanent markers (operator intent wins).
- ownership.list_permanent_shared_files helper.
- Schema v12: claim_queue.priority TEXT NOT NULL DEFAULT 'normal'.
- Queue priority hints. CreateClaimsRequest gains urgency
  (low|normal|high|blocking, matches v0.9 release-request urgency
  vocabulary). When combined with wait_seconds > 0, the FIFO queue
  orders by priority DESC then position ASC so urgent work jumps
  ahead of normal traffic. Default 'normal' preserves strict FIFO
  for legacy callers.
- db.enqueue_claim_request gains priority kwarg.
  db.pop_next_waiting_queue_entry orders by priority via a CASE
  expression (SQLite has no ENUM).
- coord-mcp claim_files exposes urgency as an optional kwarg
  (omitted from body when None; byte-identical to v0.24 shape).

## [0.24.0] - 2026-06-02

### Added

- Cross-process FIFO queue backend. service._enqueue_and_wait
  refactored from pure asyncio.Event wait to a hybrid loop: short
  event-wait (same-process fast path) plus a DB state poll every
  ~0.5s (catches cross-process grants made by another replica).
- db.get_queue_entry(queue_id) helper used by the poll path.
- 3 new tests covering same-process and cross-process grant paths.

### Changed

- Coord can now be deployed multi-replica without losing queued-
  waiter notifications. No config knob to flip; the hybrid wait is
  the default and is byte-compatible with the v0.21 in-process
  fast path.

## [0.23.0] - 2026-06-02

### Added

- Auto-demote. Closes the v0.22 one-way ratchet. A coord-managed
  shared_files entry (marked with the ``# auto-promoted=YYYY-MM-DD``
  comment suffix in owners.yaml) is removed by a background sweep
  when its rolling hotspot count stays below
  COORD_AUTO_PROMOTE_THRESHOLD for COORD_AUTO_DEMOTE_WINDOW_DAYS
  days. Sweep cadence: COORD_AUTO_DEMOTE_INTERVAL_SEC (default
  3600). Operator-added entries (no marker) are left alone.
- ownership.py extended: patch_owners_yaml_with_shared_file gains
  a ``managed=True`` kwarg that adds the marker, plus new helpers
  list_coord_managed_shared_files and patch_owners_yaml_remove_shared_file.
- Service.promote_hotspot signature gains ``managed=False`` kwarg
  and v0.22's _maybe_auto_promote now passes managed=True so demote
  can distinguish coord-owned entries.
- New auto-demote request_event type recorded per removal.
- Settings.auto_demote_interval_sec (default 3600) and
  auto_demote_window_days (default 14).
- 4 new tests.

## [0.22.0] - 2026-06-02

### Added

- Hard auto-promote. Settings.auto_promote_threshold and
  auto_promote_window_days (envs COORD_AUTO_PROMOTE_THRESHOLD,
  COORD_AUTO_PROMOTE_WINDOW_DAYS, both default 0/7). When set,
  the conflict pipeline auto-writes a shared_file rule into
  owners.yaml when a file's blocked-claim attempts cross the
  threshold within the rolling window. Each promotion records an
  auto-promote request_event for audit. Idempotent.
- Queue visibility. GET /requests?queued=true returns live FIFO
  queue rows joined with the blocking holder's engineer + pattern
  (new QueuedRequestEntry schema; v0.22).
- coord-mcp my_requests tool gains a queued kwarg passing the
  filter through.
- db.list_queued_with_holder helper (joined query, used by the
  endpoint and the dashboard panel).
- Dashboard "pending queue" panel per-repo showing depth + head-
  of-queue waiter.

## [0.21.0] - 2026-06-02

### Added

- Soft auto-promote: POST /metrics/hotspots/promote accepts
  {action: "shared_file" | "split", pattern, repo?, note?} and writes
  the corresponding rule into the active owners.yaml. Idempotent.
  Dashboard hotspot rows render an apply link for actionable suggestions.
  Operator still in the loop -- v0.21 stays read-only by default and
  only writes when actively poked.
- FIFO queue for blocked claim_files requests. Schema v11 adds the
  claim_queue table. When the caller passes wait_seconds > 0 and the
  request would 409, the service enqueues the requester behind the
  blocking holder and long-polls for up to wait_seconds seconds. On
  release (manual release_claims, TTL expiry, request approval,
  narrowed/coexist decisions) the service drains the FIFO and
  auto-grants the next entry. wait_seconds=0 or omitted preserves the
  v0.13-v0.20 immediate-409 behaviour.
- CreateClaimsRequest.wait_seconds field (0..600).
- coord-mcp claim_files exposes wait_seconds as an optional kwarg.

## [0.20.0] - 2026-06-02

### Added

- Hotspot file detection. New `db.hotspot_files(days=30, min_attempts=5)`
  helper groups conflict_log entries by (repo, attempted_pattern) and
  returns the files agents keep bouncing off of.
- `GET /metrics/hotspots?days=&min_attempts=&limit=&repo=` exposes the
  same series for external monitoring.
- Dashboard "Hotspot files (30d)" panel showing the top-N files per
  repo with attempt counts, distinct attempters, and a suggested
  action chip ("split into modules" / "promote to shared_file" /
  "monitor") based on count thresholds. Read-only signal for v0.20;
  auto-promote is queued for v0.21.

## [0.19.0] - 2026-06-02

### Added

- TypeScript parser walks RECURSIVELY into nested class_declaration
  nodes (deferred from v0.17). Both tree-sitter and regex backends
  emit inner classes plus their methods with the full ancestor path
  in `parent`. Closes the v0.17 carry-over flagged in [0.18.0].
- Go parser documents the nesting boundary explicitly: Go has no
  nested class model so the v0.16 receiver-as-parent extraction is
  the deepest nesting that applies. New tests pin the behavior for
  embedded types, function-local types, and generic receivers.

## [0.18.0] - 2026-06-02

### Added

- Dashboard "auto-resolution heatmap (30d)" panel: per-repo strip of
  30 day cells, intensity scaled by combined auto-coexist + auto-narrow
  count. Renders a "no auto-resolutions in the last 30 days"
  placeholder when the series is empty.
- `GET /metrics/auto-resolutions` endpoint returning the daily per-repo
  series. Accepts `?days=1..90` and `?repo=` filters; authenticated.
- `db.daily_auto_resolutions(days=30, repo=None)` helper. Groups
  `request_events.auto-coexist` / `auto-narrow` by `(repo, date)` so
  the dashboard, the new endpoint, and external monitoring share one
  query.

## [0.17.0] - 2026-06-02

### Added

- Recursive nested-namespace symbol claims. `"Outer::Inner::method"`
  notation works to any depth. `parse_symbol_path` uses `rpartition`
  so `parent_symbol` carries the full ancestor chain joined by `"::"`;
  `symbol_paths_overlap` prefix-matches on the canonical full path.
  A claim on `"Outer"` covers every `"Outer::*"` descendant, a claim
  on `"Outer::Inner"` covers `"Outer::Inner::*"` but not
  `"Outer::Other::*"`, sibling methods of the same class continue to
  auto-coexist.
- Python parser walks RECURSIVELY into nested class definitions and
  emits inner classes plus their methods with the full ancestor path
  in `parent`. Both tree-sitter and regex backends updated; regex
  backend uses an indentation stack to track ancestor chains in
  source order. TypeScript and Go nesting follow in a subsequent
  release; `Outer::Inner::method` claims work today via the API
  notation regardless of parser support because the conflict engine
  is the source of truth.
- Server-side symbol-claim validation in `POST /claims`. When
  `COORD_REPO_ROOT` is set, the service reads each claimed file and
  rejects unknown symbols with a hint listing the file's actual
  symbol set (up to 20 hints). Missing files skip validation rather
  than blocking the claim. When `COORD_REPO_ROOT` is unset the call
  is a silent no-op so legacy deployments keep working.
- Client-side pre-validation in `coord-mcp`. `claim_files` reads
  files locally and short-circuits with a warning + `client_validated`
  flag before round-tripping when symbols don't exist. Disable with
  `COORD_DISABLE_CLIENT_VALIDATION=1`. The server-side check remains
  the source of truth.
- 8 new e2e tests: 3 recursive-nesting overlap (`Outer::Inner::method`
  auto-coexist with siblings, outer-class blocks nested method, inner
  class blocks its descendants but not siblings), 5 server-side
  validation (skipped without repo root, accepts known symbol,
  rejects unknown with hint, accepts method notation, skips missing
  file), 5 client-side validation (analogous, plus disable-flag and
  namespace path resolution).

### Changed

- `Database.insert_claim_symbols` 6-tuple shape from v0.16 unchanged;
  the new `parent_symbol` column simply gains nesting depth at the
  string level.

## [0.16.0] - 2026-06-02

### Added

- Method-level (namespaced) symbol claims. Schema v10 adds
  `claim_symbols.parent_symbol`. Clients send `"Parent::child"`
  notation in the `symbols` list; the service splits at insert time.
  Two-level prefix-matching overlap: a method claim and its parent
  class auto-block; two sibling methods auto-coexist.
- TS/Python/Go parsers extract methods with the enclosing class /
  receiver type recorded as `parent`. The Symbol dataclass gained a
  `parent: str | None` field.
- 4 new e2e API tests for the namespace overlap matrix
  (auto-coexist between methods, class blocks method, different
  classes coexist, same method conflicts).

### Changed

- `Database.insert_claim_symbols` signature is now 6-tuples
  `(id, claim_id, file_path, symbol_name, symbol_kind, parent_symbol)`
  instead of 5-tuples. Callers that built the tuple manually must add
  the `parent_symbol` slot (pass `None` for top-level symbols).

## [0.15.0] - 2026-06-02

### Added

- **Python symbol parser.** `.py` files now participate in sub-file claims. Tree-sitter backend via `tree-sitter-python` with a regex fallback for environments where the native wheel is unavailable; selection follows the existing `COORD_SYMBOL_PARSER` contract introduced in v0.14.
- **Go symbol parser.** `.go` files now participate in sub-file claims. Tree-sitter backend via `tree-sitter-go` with a regex fallback; same selection rules as the other backends.
- **`coord doctor` symbol-parser-backend check.** A new `symbol parser backend` diagnostic probes every registered extension (`.ts`, `.tsx`, `.py`, `.go`) and reports which backend would resolve. OK when every extension lands on tree-sitter; WARN when one or more fall back to regex (the parser still works, but native wheels give stricter parsing); FAIL only when `COORD_SYMBOL_PARSER=treesitter` is forced and a native grammar is missing -- which would otherwise crash `extract_symbols` at call time. The check uses a new `coordination.symbols.probe_backend(extension)` helper that classifies the resolved backend without raising. WARN-level results do not flip the doctor exit code.
- **Sub-file (symbol-level) claims promoted to stable.** The opt-in `symbols` field on `POST /claims` / `claim_files`, the `AUTO_COEXIST` / `AUTO_NARROW` decisions, the `narrowable` flag, and the `claim_symbols` join table (schema v8) are all considered stable as of v0.15. Schema v8 and v9 are unchanged; the only behavioural difference is documentation posture and the addition of Python and Go to the parser dispatcher.

### Changed

- Sub-file claims promoted from experimental to stable. Schema v8 + v9 unchanged; the only behavioural difference is documentation posture and the doctor surfacing parser-backend status.
- `CheckResult` in `coordination/cli_doctor.py` gains an optional `level: str` field (default `"fail"`) so a check can opt into WARN semantics without breaking the existing boolean `ok` contract. WARN results are surfaced in the doctor output but do not fail the overall exit code. Only the new symbol-parser-backend check uses this today.

## [0.14.1] - 2026-06-02

### Added

- **Dashboard surfaces symbol claims.** Active claims table gains a `Scope` column (`file` / `symbol` / `shared_file` / `module`) and a `Symbols` column that lists the bound symbol names from the `claim_symbols` join table. Symbol-scope rows are visually distinct so an operator can see at a glance which claims are sub-file.
- **`Auto-resolutions (24h)` stat panel** showing rolling counts of `AUTO_COEXIST` and `AUTO_NARROW` events. Computed from `request_events`; no schema change.
- **`/repos` response includes `auto_resolutions_24h` per repo**, so the dashboard's repos panel can show the same headline figure broken down by repository without an additional round trip.

## [0.14.0] - 2026-06-02

### Added

- **Sub-file (symbol-level) claims (experimental).** `POST /claims` and the `claim_files` MCP tool accept an optional `symbols` field that flips a claim from whole-file to symbol-scope: the claim covers only the named top-level declarations, not imports or other module-level code. Two automatic decisions resolve overlaps without filing a request: `AUTO_COEXIST` grants two symbol claims on the same file when their symbol sets are disjoint, and `AUTO_NARROW` grants a symbol claim against an existing narrowable file claim, marking the two as cooperative partners. Both are logged to `request_events` as new event types but skip the `requests` table. A new `narrowable` flag controls auto-narrow eligibility (`file` defaults `true`; `shared_file` / `module` / `symbol` default `false`). Schema v8 adds `claims.scope_type`, `claims.narrowable`, and a `claim_symbols` join table; pre-v0.14 rows backfill as `scope_type='file'` so existing behaviour is unchanged. TypeScript is the only parser backend in v0.14 (tree-sitter with a regex fallback, selected via `COORD_SYMBOL_PARSER`); Python and Go follow in v0.15. Opt-in: any caller that doesn't pass `symbols` sees identical behaviour to v0.13. Full spec in `docs/design/sub-file-claims.md`.
- `coord-mcp` auto-loads `<repo-root>/.coordination/local.env` at startup, walking up from cwd like git looking for `.git/`. Restricted to a `COORD_*` allowlist so an unrelated env line in the file can't mutate the wrapper. Explicit env (shell exports, `.mcp.json` env block, codex `[mcp_servers.coord.env]`) still wins -- the file only fills in unset variables and overrides documented placeholders (`set-me`, `example-org/example-repo`, `http://127.0.0.1:8080`). This makes the committed-template + gitignored-secret pattern work hands-off: a tracked `.mcp.json` can ship placeholder values to a public repo and the wrapper recovers the real values from `.coordination/local.env` (which `coord init` already gitignores).
- `_headers()` skips the `Authorization` header entirely when the resolved token is a documented placeholder, so a misconfigured setup yields a clean `401 Authorization required` instead of a `Bearer set-me` request that looks malicious in server logs.
- `tests/test_deploy_overlay.py` guards against two sanitisation hazards: `deploy/k8s/prod/` (the live Argo overlay) must NOT carry `YOUR_CLUSTER` / `coord.internal.example` / `set-me` placeholders, and the tracked `.mcp.json` must NOT carry a real-looking 40+ char hex token. Parametrised over every yaml in the overlay; 10 cases total.
- Documentation sweep: README has a new `Configuration & secrets` section with a tracked-vs-gitignored table and the runtime resolution model; `docs/getting-started.md` annotates Step 4 file list and adds a `Configuration & secrets` subsection; `docs/architecture.md` has a new `Env resolution` subsection under MCP bridge; `docs/troubleshooting.md` has new `MCP tools return 401 from a known-good service` and `MCP wrapper picks up the wrong service URL` entries with a copy-pasteable diagnostic command; `docs/integrations/{claude-code,codex-cli}.md` document the resolution order in tool-specific terms; `templates/README.md` calls out that the `.example` MCP wirings are placeholder templates and points at `local.env` for real credentials.

### Fixed

- **Deploy overlay placeholder regression.** A prior "public readiness" sanitisation sweep had replaced `secret/apps/k8s/coord` with `apps/YOUR_CLUSTER/coord` in `deploy/k8s/prod/vaultstaticsecret-{auth,ghcr}.yaml`, leaving the live `coord-auth` and `ghcr-pull` Secrets unable to refresh against Vault (`VaultStaticSecret` status: `empty response from Vault, path="secret/data/apps/YOUR_CLUSTER/coord"`). The kebabrack `coord` pod kept serving on stale cached Secret data, but the placeholder also propagated into `.mcp.json` env blocks across repos, leading to `401`s from MCP clients once their wrappers were spawned with the sanitised values. Restored the real Vault path in both manifests; the new `tests/test_deploy_overlay.py` guard prevents the same sweep from breaking prod again. The companion ingress-host restore landed earlier in `f7851d1`.

## [0.13.0] - 2026-05-06

### Fixed

- **CRITICAL: idle expiration silently disabled in background sweep.** The background `cleanup_loop` called `expire_stale_claims()` with no arguments, so `idle_timeout_sec` defaulted to 0 and the idle path never fired between API requests. Dead-agent claims were not reaped after 30 minutes of inactivity; they sat until the full hard TTL expired. Fix: pass `settings.idle_timeout_sec` to the background call.
- **HIGH: `narrowed` / `coexist` decisions inherited a possibly-shortened TTL.** When a holder responded to a release request with `narrowed` or `coexist`, the new claim's `expires_at` was copied from the original row, which may have already been shortened by `request_release`. The new claim could expire within minutes of creation. Fix: the service layer now passes `min_expires_at = now + default_ttl_hours` to `db.respond_to_request`, which floors the new claim's TTL at that value. The DB layer accepts a `min_expires_at: str | None` parameter on `_apply_narrowed` and `_apply_coexist`.
- **HIGH: `_register_session_marker` non-atomic read-modify-write race.** Two `coord-mcp` processes starting simultaneously both read sessions.live before either writes; the second writer's atomic replace silently dropped the first session's entry. That session's claims were then not self-excluded at push time, causing false-positive conflict blocks on the agent's own push. Fix: registration is now append-only (one `open(marker, "a")` write, no read). Stale entries are swept lazily by `_remove_session_marker` on graceful shutdown, which rewrites the file with only live entries.
- **MEDIUM: `release_for_session` TOCTOU.** SELECT and UPDATE ran in separate connections; a concurrent `release_claims` between them could close some IDs before the UPDATE, causing spurious cascade-resolve calls. Fix: SELECT and UPDATE now share a single `BEGIN IMMEDIATE` transaction.
- **MEDIUM: `ttl_shortened` audit label used wrong heuristic.** `expire_stale_claims` labelled expiring claims `"ttl-shortened"` based on whether they had pending requests at expiry time, producing false positives (claim expired with a pending request that arrived just before the natural deadline) and false negatives (TTL shortened but requester withdrew). Fix: schema v7 adds `claims.ttl_shortened BOOLEAN DEFAULT 0`; `create_request` stamps it `1` when shortening; the `denied` decision resets it `0`; `expire_stale_claims` reads it directly from the claims row rather than joining the requests table.
- **LOW: non-constant-time bearer token comparison.** `token != settings.auth_token` used Python's built-in string equality. Fix: replaced with `hmac.compare_digest`.
- **LOW: redundant in-function imports in `service.py`.** `file_request` re-imported `datetime`/`uuid4` inside its body; both are already at module scope. Removed.

### Added

- `claims.ttl_shortened BOOLEAN DEFAULT 0` column (schema v7). Tracks whether a claim's TTL was explicitly shortened by a `request_release` call. Used by `expire_stale_claims` for accurate audit labelling and reset by `denied` decisions when the original TTL is restored.
- `db.respond_to_request` accepts `min_expires_at: str | None` parameter, forwarded to `_apply_narrowed` and `_apply_coexist`. Callers can supply a floor timestamp; new claims get `max(inherited_ttl, min_expires_at)`.
- Tests for `narrowed` and `coexist` decision paths: TTL floor behavior, `ttl_shortened` stamping and reset, and both "healthy TTL unchanged" and "shortened TTL floored" cases (7 new tests in `test_requests_v11.py`).
- `_remove_session_marker` now sweeps dead-PID and legacy-format entries when rewriting the file, completing the lazy-cleanup contract introduced by the append-only registration change.

## [0.12.0] - 2026-05-06

### Added

- **PID-tracked `sessions.live` format (v0.12)**. Every `coord-mcp` instance now writes `<session_id> <pid> <start_time_ns>` per line in `.coordination/sessions.live` instead of the bare session-id from v0.10. The PID enables liveness probing without any process-supervision infrastructure.
- **Self-healing startup sweep**. `_register_session_marker` runs `_sweep_stale_entries` before appending its own line. Any entry whose PID fails a `kill -0` probe (or whose start time mismatches on Linux, preventing PID-reuse false positives) is silently pruned. A single startup on a host that had SIGKILL-killed coord-mcp processes clears all stale entries.
- **`_is_live_pid` / `_process_start_time_ns`** helper functions in `coordination/mcp_server.py`. `_process_start_time_ns` reads `/proc/<pid>/stat` on Linux for PID-reuse defense; returns 0 on other platforms where the probe is not available.
- **Hook-side PID liveness pruning**. The `SESSION_QS` block in the pre-push script now parses the three-field v0.12 format and uses `kill -0 <pid>` to skip dead entries before forwarding session IDs to `/conflicts`. Legacy entries (no PID field) are also skipped so old repos migrate safely on first contact.
- **`coord doctor` sessions.live check**. A new `_check_sessions_live` diagnostic in `coordination/cli_doctor.py` surfaces the total / live / stale entry counts. Reports `WARNING` when stale entries are present (they will be pruned on next `coord-mcp` startup), `OK` when all entries are live, and `INFO` when the file is absent.

### Changed

- **Backward compat**: pre-v0.12 `sessions.live` entries (bare session_id, no PID) are treated as stale on first contact and pruned automatically on the next `coord-mcp` startup. No manual migration needed.

### Fixed

- Stale `sessions.live` entries from SIGKILL-killed `coord-mcp` processes no longer accumulate indefinitely. Previously, `atexit` cleanup does not fire for SIGKILL or OOM kills, leaving entries forever. The PID-tracked format with startup-time sweep eliminates this buildup without requiring a supervisor.

## [0.11.0] - 2026-05-05

### Added

- **`narrowed` decision** on `respond_to_request`. The holder can close their original claim and atomically open a new claim with a tighter pattern. Inherits the original's engineer / repo / session / TTL. The server validates `narrowed_pattern` is a strict subset of the holder's current pattern via the same heuristic-overlap synthesizer used by `compute_overlap`; disjoint or broader patterns are 400'd. Cascade-resolves any open requests against the closed claim.
- **`coexist` decision** on `respond_to_request`. The holder grants the requester a sibling claim on the same scope. Both claims live, mutually self-excluded via a new `claims.coexists_with` JSON-array column, and they remain adversarial to anyone outside the pair. Useful when two agents want to edit different functions in the same file. Cooperative not enforced -- imports and shared module-level state are still on the agents to handle.
- **`requested_scope`** on `POST /requests` and the `request_release` MCP tool. The requester says what they actually need (often a sub-pattern of the holder's claim); the holder uses it to decide between approve / deny / narrow / coexist. Recorded in the audit trail.
- **Schema migration v6** adds nullable `requests.requested_scope` (TEXT) and `claims.coexists_with` (TEXT, JSON-encoded array of partner claim IDs, NULL for none). Forwards-only.
- Dashboard `release requests` panel gains a `scope` column and the `narrowed` (dashed phosphor) and `coexist` (cyan) decision pills.
- `db._detach_coexist_partners` cleanup hook wired into `release_claims`, `release_for_session`, and `expire_stale_claims` so a coexist partner's `coexists_with` array is cleaned up when its sibling claim ends.

### Fixed

- coord-mcp no longer installs custom SIGTERM/SIGINT handlers. The v0.10 handlers re-raised the signal under `SIG_DFL` after cleanup, which fought with FastMCP's own signal handling and caused the MCP child to die abruptly during operations -- agents would then see "Transport closed" on their next tool call and have to fall back to the coord CLI to release claims. Marker cleanup now runs purely from `atexit`, which fires for both clean exits and signal-driven shutdowns through the interpreter's normal path. FastMCP keeps full control over the signal disposition.

### Built by

A 3-phase, 3-agent build: phase 1 (schema + DB methods) ran sequentially because phases 2/3 depend on its API; phase 2a (service + API + conflict-check coexist semantics) and phase 2b (MCP tools + dashboard + snippets) were dispatched in parallel against the merged phase 1. Phase 2b's agent hit a usage cap mid-task; its tests landed but the implementation had to be completed by the dispatcher.

## [0.10.0] - 2026-05-05

### Fixed

- Pre-push hook now self-excludes every live `coord-mcp` session in the repo, not just claims matching `git config user.name`. Pre-v0.10.0 the hook passed only the git user as the engineer, so an agent's own subagent claims (under names like `codex-server-review` or `claude-l26-fix` that don't match git's user) showed up as adversarial conflicts on the agent's own push, forcing the agent to pre-release before pushing as a defensive workaround. Three coordinated changes close this:
  - `coord-mcp` writes its `session_id` to `<repo_root>/.coordination/sessions.live` on startup (atomic temp+rename, idempotent for parallel sessions in the same repo) and removes it on graceful shutdown via `atexit` + SIGTERM/SIGINT handlers. If the file would become empty, it's unlinked. All filesystem operations are wrapped so a hostile or read-only `.coordination/` cannot break MCP startup or shutdown.
  - The pre-push hook reads `.coordination/sessions.live` and forwards every non-empty, non-comment line as a `&session_id=<encoded>` query param on the `/conflicts` URL. Skips silently when the file is absent (legacy engineer-name self-exclusion still applies).
  - `GET /conflicts` accepts repeated `session_id=` query params; `service.check_conflicts` now takes `session_ids: list[str] | None` and excludes any active claim whose `session_id` matches any of the supplied ids. Single-value behaviour is preserved through the same code path.

### Added

- `_register_session_marker` / `_remove_session_marker` helpers in `coordination/mcp_server.py` plus an `_atomic_write_lines` primitive used for the marker file.

### Changed

- `CoordinationService.check_conflicts` keyword renamed from `session_id` (singular) to `session_ids: list[str] | None`. Callers passing a single id now pass `[id]`. The API layer accepts both single and repeated query params and forwards either as a list, so external callers see no breaking change.

## [0.9.0] - 2026-05-05

### Added

- **Release-request system.** A requester whose `claim_files` was blocked by an active claim can file an explicit release request. Filing shortens the holder's claim TTL to `min(remaining, COORD_REQUEST_TTL_SHORT_SEC)` (default 300s), surfaces in the holder's next `pending_requests` poll, and the holder responds with `respond_to_request` (decision: `approved` releases the claim now; `denied` restores the original TTL). If the shortened TTL fires before the holder responds, the request transitions to `expired` and the requester is unblocked. Releases for unrelated reasons (`release_session`, voluntary release, idle expiration) cascade open requests to `resolved`.
- **Immutable audit log.** Every state transition is appended to `request_events` with actor, session_id, timestamp, and a JSON detail blob: `filed`, `notified` (first time per holder session), `responded`, `expired`, `resolved`, plus `responded-late` when a holder tries to decide after the request has already terminalised. Operators can replay the full lifecycle of any request via `GET /requests/{id}/events`.
- **MCP tools:** `request_release(claim_id, reason, urgency, wait_seconds=60)` (long-polls the decision by default), `respond_to_request(request_id, decision, note)`, `wait_for_request(request_id, timeout)` (block on a previously fire-and-forget request), `my_requests(decision='pending')`. Existing `pending_requests` now returns first-class requests merged with the read-only auto-conflict-log entries, distinguished by a `kind` discriminator.
- **HTTP endpoints:** `POST /requests`, `POST /requests/{id}/respond`, `GET /requests` (filterable by requester / claim_id / decision), `GET /requests/{id}`, `GET /requests/{id}/events`.
- **Dashboard panel.** New "release requests" panel between recent conflicts and claim timeline. Columns: when, requester, holder, urgency pill, decision pill, time-to-decision latency. Same v0.8 phosphor aesthetic; pills use phosphor green for `approved`, red for `denied`/`urgency-blocking`, amber for `pending`/`urgency-high`, muted for `urgency-low`.
- **Schema migration v5** adds the `requests` table (current state per request) and `request_events` table (append-only audit log). Forwards-only; pre-v5 databases migrate cleanly with no row-level data changes.
- **Settings:** `COORD_REQUEST_TTL_SHORT_SEC` (default 300) controls the shortened-TTL window applied when a request is filed.

### Changed

- The managed CLAUDE.md / AGENTS.md / cursor coordination snippets now mention `request_release` and the v0.9+ flow as a graceful enhancement on top of the v0.6+ tools, with the unconditional three-call workflow (`list_claims` / `claim_files` / `release_claims`) still treated as the baseline.

## [0.8.1] - 2026-05-03

### Fixed

- Widened the managed `.gitignore` entry from `.coordination/local.env` to the entire `/.coordination/` directory. Hard-won lesson from a requesthub incident: `git stash -u` (`--include-untracked`) sweeps up untracked files but skips ignored ones, so the narrow rule left `config.toml` / `owners.yaml` / `hooks/` exposed to stash-pop conflicts that silently dropped them, leaving a partial-install state where `.git/hooks/pre-push` exec'd a missing target. Every push then exit-coded silently. Widening to the whole directory makes that failure mode impossible by design; everything under `.coordination/` is per-developer state generated by `coord init` and should never be committed. The in-place block-rewrite in `ensure_gitignore_entry` migrates existing repos automatically on the next `coord upgrade`.

## [0.8.0] - 2026-05-03

### Added

- Dashboard's conflict log now derives a useful resolution status per row instead of leaving the column empty. The `conflict_log.resolution` field has never been populated by anything in the codebase; v0.8 ignores it at render time and computes the status from the linked claim's current state: `blocked` (holder still has it), `released` (voluntary), `ttl-expired` (sweep closed it), `idle-released` (activity-based auto-release fired), `stale` (TTL passed but cleanup hasn't run), or `missing` (claim aged out). Each status has a distinct accent color.
- Conflict log gains `holder` and `holder pattern` columns so a glance tells you who was holding the claim and on what scope, not just who tried and was blocked.
- Active claims gain `repo` and `session` columns. The session column shows the first 8 chars of `session_id` with the full id on hover.
- Claim timeline gains a state pill (same vocabulary as conflict resolution) and a relative-time `updated` column.
- Top-of-page stats block: 4 big-number cards (repos, active claims, conflicts 24h, idle-timeout) with delta lines. Closes the gap where the headline figures were buried in a low-visibility activity table.

### Changed (visual redesign)

- Dashboard rewritten from scratch in a brutalist phosphor-terminal aesthetic. Type pairing: Major Mono Display for structural ALL-CAPS headings, JetBrains Mono for everything else (both Google Fonts, both deliberately distinct from the Inter/Space-Grotesk defaults). Color reserved for signal: phosphor green for activity, amber for warnings/numbers, red for blocked conflicts. Sharp hairlines instead of borders, no rounded corners except 1px on status pills, subtle SVG noise overlay for tactile depth, staggered panel reveal on load. Section headers anchored with a phosphor `▌` glyph.
- Module heatmap density bar uses Unicode block characters (`▏▎▍▌▋▊▉█`) instead of `####....` ASCII, giving a smoother gradient at the same width.
- Timestamps render relative ("2m ago", "0s ago") with the absolute UTC value on hover, so a glance at the page doesn't require subtracting timestamps in your head.

### Changed (snippets in CLAUDE.md / AGENTS.md / cursor)

- The managed coordination snippet now lists the basic three-call workflow (`list_claims` / `claim_files` / `release_claims`) as the unconditional protocol, and mentions `pending_requests` / `release_session` as enhancements to prefer when the local `coord-mcp` exposes them (v0.6.0+). The previous snippet prescribed the v0.6+ tools as the sole protocol, which an agent in astrowars correctly flagged as misleading when its MCP child happened to be running an older coord-mcp build.

## [0.7.2] - 2026-05-02

### Fixed

- Pre-push hook now refuses loudly when stdin is redirected but empty (an outer wrapper hook backgrounded the coord call or otherwise dropped git's ref-update stream). The pre-v0.7.2 hook silently fell through to a HEAD-vs-origin/HEAD diff in this case, which misses non-HEAD pushes, multi-ref pushes, new-branch pushes, and deletions -- exactly the failure mode that surfaced in astrowars where a project-level `run_child` wrapper was backgrounding coord with `"$@" &`. The hand-run fallback (TTY stdin) is preserved for testing, but now prints a noisy heads-up that it's the test path. The refusal message includes a worked example of the right outer-hook wiring (cache stdin once into a tempfile and redirect the coord child from it).

### Wrapping coord's hook from another pre-push hook

If your repo already has a tracked pre-push hook and you want to chain coord's check into it:

    # near the top, cache git's ref-update stream once
    PUSH_REFS="$(mktemp)"
    trap 'rm -f "$PUSH_REFS"' EXIT
    [ ! -t 0 ] && cat > "$PUSH_REFS"

    # at the call site, redirect stdin from the cache
    bash "$REPO_ROOT/.coordination/hooks/pre-push" "$@" < "$PUSH_REFS"

The hook reads `<local_ref> <local_sha> <remote_ref> <remote_sha>` lines off stdin to compute a per-ref diff. Without that input it can't tell what's actually being pushed.

## [0.7.1] - 2026-05-02

### Fixed

- `coord init --force` no longer silently destroys a tracked pre-push hook when `.git/hooks/pre-push` is a symlink to a repo file. `pathlib.Path.write_text` follows symlinks, so the previous code wrote the coord shim *through* the symlink and clobbered the target -- typically `scripts/git-hooks/pre-push` carrying real CI / lint / deploy logic. Init now detects symlinks before any write and refuses to follow them, printing actionable guidance for chaining coord's check into the user's existing hook. The non-symlink overwrite path (force=True over an existing non-coord hook) now writes a `.bak` of the previous content first.
- `coord doctor` adds a check that `.coordination/hooks/pre-push` exists. The shim in `.git/hooks/pre-push` exec's that target; if the target is missing every push silently exits zero, so deploy commits stay local without surfacing -- which is exactly how requesthub's deploys broke. The new check fails loud with a `coord upgrade` hint when the target is missing.

### Migration

Repos that have a tracked pre-push hook should chain coord's check into it with these two lines (no auto-magic; explicit beats clobbering):

    COORD_HOOK="$(git rev-parse --show-toplevel)/.coordination/hooks/pre-push"
    [ -x "$COORD_HOOK" ] && "$COORD_HOOK" "$@"

## [0.7.0] - 2026-05-02

### Changed (behaviour-affecting)

- Pre-push hook now fails closed instead of silently skipping when prerequisites are missing or transport fails. Three previously-silent bypass paths are now hard refusals:
  - `jq` not installed: was `exit 0` with a "skipping" message; is now `exit 1` with a hint to install jq or pass `--no-verify`.
  - `curl` error talking to the service: was wrapped in `|| true` so a transient network glitch produced an empty response and the check passed by default. The curl exit code is now checked explicitly; any error refuses the push.
  - Unparseable response from `/conflicts`: was treated as "no conflict"; is now refused with the raw body printed for diagnosis.
- Pre-push hook now consumes the ref-update stream that `git push` hands the hook on stdin (`<local_ref> <local_sha> <remote_ref> <remote_sha>`) and computes the diff per ref. Pre-v0.7 always diffed `HEAD...origin/HEAD` regardless of what was actually being pushed, which silently missed multi-ref pushes, non-HEAD pushes, and deleted-branch pushes. Falls back to the old HEAD-based path when run interactively without stdin.
- First-push scenarios (no remote tracking branch yet) now diff against `git hash-object -t tree /dev/null` (the empty tree). Triple-dot diff fails with the empty tree, which is why the pre-v0.7 hook punted with "could not determine diff base; skipping" in that case -- yet another silent bypass.
- Conflict-check response parsing is stricter: `.has_conflicts` must be `true` or `false`. An empty / null / unexpected value is treated as a server bug and refuses the push.

### Migration

The behaviour change only matters for environments where the hook was previously hitting a silent-skip path. If you've been relying on `jq`-missing as a tacit bypass, install jq or use `--no-verify` deliberately. Existing repos pick up the new hook on their next `coord upgrade`.

### Credits

The hook redesign was prompted by an agent in astrowars rewriting the hook on its own to close these holes. The diff was reviewed and ported upstream verbatim, with comments expanded.

## [0.6.2] - 2026-05-02

### Fixed

- `coord upgrade` now refreshes `.gitignore` too. v0.6.1 fixed the marker style for fresh repos, but the upgrade path didn't touch `.gitignore`, so existing repos couldn't migrate to the new `# coord:` markers without re-running `coord init`. Upgrade now calls `ensure_gitignore_entry` alongside the rest of the asset refresh; the in-place detection accepts either marker style, so the migration is idempotent and never duplicates the entry.

## [0.6.1] - 2026-05-02

### Fixed

- `.gitignore` managed-block markers now use shell-comment syntax (`# coord:begin` / `# coord:end`) instead of HTML-comment syntax. The HTML markers are not valid gitignore comments and were silently parsed as never-matching path patterns; an agent in astrowars saw them as broken syntax and "fixed" them, drifting the file off coord's detection contract. Detection now accepts either marker style on read, so existing repos migrate cleanly on their next `coord upgrade` without losing the entry.
- Managed blocks now embed an `AUTO-GENERATED by 'coord upgrade'. Do not hand-edit; next upgrade will overwrite.` warning as their first content line. Future agents inspecting CLAUDE.md / AGENTS.md / `.cursor/rules/coordination.mdc` see the contract immediately rather than treating the cryptic marker line as a hint to ignore. Doctor's drift comparison strips the warning before matching against the packaged snippet so the new line doesn't itself look like drift.
- `ensure_managed_block` now recognises a block whose markers were swapped to hash style (the astrowars vandalism scenario), so the next upgrade replaces it in place with proper HTML markers rather than appending a duplicate.

### Changed

- Tightened the CLAUDE.md / AGENTS.md / cursor coordination snippets: same protocol, fewer words, harder to want to "simplify."

## [0.6.0] - 2026-05-02

### Added

- Pending-requests inbox. New `GET /sessions/{session_id}/pending_requests` returns recent conflict-log entries logged against active claims a session currently holds, so an active holder can poll "has anyone been blocked on my scope?" between operations and release voluntarily. coord-mcp exposes this as a `pending_requests` tool whose default form takes no arguments and uses the current process's session id. The CLAUDE.md / AGENTS.md / cursor managed snippets have been updated to recommend polling between operations.
- Activity-based auto-expiration. Session-tagged claims now carry a `last_activity` timestamp that gets bumped on every coord call from the holder's session (`claim_files`, `check_conflicts`, `list_claims`). The cleanup sweep auto-releases any session-tagged claim that has been silent for longer than `COORD_IDLE_TIMEOUT_SEC` (default 1800 seconds / 30 minutes), catching agents that walked away without releasing. Legacy NULL-session claims keep `last_activity = NULL` and are unaffected -- they continue to use TTL only. Set `COORD_IDLE_TIMEOUT_SEC=0` to disable idle expiration cluster-wide.
- Conflict log records the requester's `session_id` (`conflict_log.attempted_session_id`), so the holder can distinguish foreign sessions from its own subagents in the pending-requests inbox.

### Changed

- coord-mcp's `list_claims` and `check_conflicts` tools now include `session_id` on every call. The conflict check itself was already session-aware in v0.5; the new wiring lets these calls also act as activity pings on the server side, keeping the holder's claims warm while it's actively reasoning rather than only when it's creating new claims.
- Schema bumped to v4 via a forwards-only migration adding nullable `claims.last_activity` and `conflict_log.attempted_session_id` columns. Pre-v4 data is preserved with NULLs.

## [0.5.0] - 2026-05-02

### Added

- Per-MCP-process session id. The conflict check now self-excludes any active claim whose `session_id` matches the caller's, so subagents spawned by a single Codex/Claude Code/Cursor process never block each other when they pick distinct engineer names. Different sessions remain adversarial. coord-mcp generates a 16-char hex id at module load and sends it on every `claim_files` call; operators can pin a stable value with `COORD_SESSION_ID`. Schema bumped to v3 via a forwards-only migration adding a nullable `claims.session_id` column; pre-v3 claims keep `session_id=NULL` and behave like the legacy engineer-only self-exclusion path. Closes the orphaned-claim trap where a parent agent left claims under engineer `codex` and its subagents (using names like `codex-server-review`) were locked out of overlapping scope until TTL expiry.
- `GET /conflicts` accepts a new `session_id=` query parameter mirroring the field on `POST /claims`.
- `POST /sessions/{session_id}/release` releases every active claim with the given session_id in one call. coord-mcp exposes this as a `release_session` tool whose default form takes no arguments and uses the current process's session id, so end-of-work cleanup is one MCP call regardless of how many engineer names the agent used.

## [0.4.2] - 2026-05-02

### Fixed

- `coord upgrade` now refreshes every tool config that exists on disk in the repo, not just the one named in `.coordination/config.toml`. A repo that wired both Claude (`.mcp.json`) and Codex (`.codex/config.toml`) by running `coord init` twice with different `--tool` values used to have only the most-recent tool's config refreshed by upgrade; the other silently kept its stale URL/token/repo id. Cursor configs are handled the same way. Upgrade still falls back to creating the tool named in `config.toml` when its file is missing, so deleting a config and re-running upgrade restores it.
- `coord doctor` now flags managed-block drift in CLAUDE.md, AGENTS.md, and the cursor rules file independently, so multi-tool repos see a drift warning for whichever doc has gone stale (previously only the primary tool from `config.toml` was checked).
- `coord doctor` now compares the embedded `COORD_AUTH_TOKEN` in each tool's MCP config against `.coordination/local.env` and reports drift. Pre-fix, rotating the token in `local.env` without running `coord upgrade` left the MCP child authenticating with the old key with no warning.

## [0.4.1] - 2026-05-02

### Fixed

- Codex MCP setup now writes an explicit `[mcp_servers.coord.env]` block in `.codex/config.toml` carrying `COORD_API_URL`, `COORD_AUTH_TOKEN`, and `COORD_REPO_ID`. The previous codex template embedded only a comment hint pointing at `.coordination/local.env`, which Codex never sources, so the MCP child silently fell back to `http://127.0.0.1:8080` and surfaced "All connection attempts failed" to operators on remote-mode repos. `coord init` and `coord upgrade` both populate the env block; existing codex repos pick up the fix on their next `coord upgrade`.

## [0.4.0] - 2026-05-01

### Changed

- Conflict detection is now repo-scoped. A claim with `repo=X` is only checked against other claims with `repo=X`; a claim with `repo=NULL` (legacy / un-tagged client) is only checked against other `repo=NULL` claims. Closes the cross-repo false-positive where, for example, a `client/js/**` claim from `example-org/astrowars` would block any push touching `client/js/**` in unrelated services on the same coord instance. Both `POST /claims` and `GET /conflicts` apply the partition.
- `GET /conflicts` accepts a new `repo=` query parameter so the pre-push hook can scope its check.
- Pre-push hook reads `COORD_REPO_ID` from `.coordination/local.env` and forwards it as `&repo=` on every `/conflicts` call. Existing repos pick up the behaviour on their next `coord upgrade`.

## [0.3.0] - 2026-05-01

### Added

- Per-repo claim tracking. Each claim now carries a repo identifier ("amittell/coord"-style slug). The MCP bridge reads `COORD_REPO_ID` and includes it on every `claim_files` call; `coord init` and `coord upgrade` derive the value from `git remote get-url origin` (HTTPS or SSH) and persist it in `.coordination/config.toml`, `.coordination/local.env`, and the tool-specific MCP config. Existing repos pick up the value on their next `coord upgrade`.
- New GET `/repos` endpoint returns one row per repo with active claim count, claims and engineers in the rolling 24h window, and last-activity timestamp.
- GET `/claims` now accepts `?repo=` to filter to a single repo's claims.
- Dashboard adds a "Repositories" panel listing every repo using the service so operators can see at a glance who's coordinating where.

### Changed

- Database schema bumped to v2 via a forwards-only migration that adds a nullable `claims.repo` column. Pre-v2 claims keep `repo=NULL` and are excluded from the per-repo aggregations.

## [0.2.1] - 2026-04-28

### Added

- Dashboard now opens with a "Recent activity (last 24h)" panel that summarises claims created, conflicts logged, distinct engineers active, and the top modules touched in the rolling 24h window. Computed from the existing claim and conflict tables, no schema changes. Closes the gap where the headline "Active claims" and "Module heatmap" sections both rendered empty between active sessions and made the page look dead.

## [0.2.0] - 2026-04-27

### Added

- `coord upgrade` command refreshes the pre-push hook, MCP config, and managed CLAUDE.md / AGENTS.md / cursor block from the latest packaged assets while preserving `.coordination/config.toml`, `.coordination/owners.yaml`, and the existing `COORD_AUTH_TOKEN`.
- `coord doctor` now flags managed asset drift (in-repo hook or managed block content does not match the packaged snippet) and points at `coord upgrade`.
- `coord doctor` now compares the locally installed CLI version against the running service's `/meta` and reports skew in either direction with an actionable hint (update local install, or bump the cluster image).
- Proactive once-per-24h update notice on every CLI command. When the configured service reports a newer version than the local install, `coord` prints a single stderr line pointing at `coord upgrade`. Throttled via a timestamp file, silent on failure, opt-out with `COORD_NO_UPDATE_CHECK=1`, skipped for `init` / `start` / `_serve` / `doctor` and outside coord-initialised repos.
- `deploy/k8s/prod/` overlay: namespace, Traefik ingress, local-path PVC, two `VaultStaticSecret` resources (auth token + GHCR pull credentials rendered as `kubernetes.io/dockerconfigjson`), and a pinned image digest.

### Fixed

- Pre-push hook silently skipped the conflict check when `COORD_AUTH_TOKEN` was empty, disabling protection for any service running in `COORD_ALLOW_INSECURE_NO_AUTH` mode. Now omits the Authorization header instead of skipping, so the check still runs and a 401 is the only failure path.
- Pre-push hook ignored the repo's `.coordination/local.env` and silently fell back to `http://127.0.0.1:8080` whenever the pushing shell had no `COORD_SERVICE_URL` exported. The hook now sources `local.env` first and `coord init` writes `COORD_API_URL` and `COORD_SERVICE_URL` into it. URL fallback chain becomes: `COORD_API_URL` -> `COORD_SERVICE_URL` -> `COORD_URL` -> `http://127.0.0.1:8080`.
- Pre-push hook crashed under bash 3.2 (the macOS system bash) with `CURL_AUTH[@]: unbound variable` when the auth token was empty. Switched the auth-header expansion site to the portable `${var[@]+"${var[@]}"}` form so empty arrays no longer trip `set -u`.
- `coord doctor`'s auth probe sent `Authorization: Bearer ` (trailing space) when the token was empty, which httpx rejects as an illegal header value. Doctor now sends no Authorization header in that case and renames the check to `unauthenticated access works` with a hint pointing at `COORD_ALLOW_INSECURE_NO_AUTH`.

## [0.1.0] - 2026-04-21

### Added

- Core HTTP API for claims, conflicts, ownership configuration, and a bundled dashboard.
- MCP stdio bridge (`coord-mcp`) so Claude Code, Codex CLI, and Cursor can talk to the service as a native tool.
- `coord` CLI with `start`, `init`, `doctor`, `stop`, `status`, `claims`, and `release` subcommands.
- `coord --version` flag that prints the installed package version.
- Shell completion scripts for bash and zsh under `scripts/completions/`.
- Container image with a non-root runtime user, multi-stage build, and pinned Python dependencies.
- GitHub Actions CI matrix covering Ubuntu, macOS, and Windows.
- Release workflow with `workflow_dispatch`, SHA-pinned third-party actions, and build provenance attestations.
- Dependabot configuration for GitHub Actions and Python dependency updates.
- Windows-friendly path handling, GitHub Enterprise support, monorepo layouts, and Scalar clone support in the repo scanning helpers.
- `COORD_REPO_SCOPE` environment variable and a 10-second `git ls-files` cache to bound repo scanning cost.
- Cross-process migration safety via `BEGIN IMMEDIATE` and `busy_timeout` on the SQLite writer.
- PID marker verification for `coord stop` so we never SIGTERM an unrelated process.
- `tests/test_mcp_server.py` regression guards around the MCP stdio bridge surface.

### Changed

- Pattern negation (leading `!`) is now rejected at the API boundary with a clear 400 response instead of being silently partially supported.
- Zero-match claim scopes now emit a warning with a case-insensitive hint when a near-match exists.
- Dependabot grouping tightened: dev tools (pytest, pytest-asyncio, ruff, mypy) bundle into one PR across all version types; production deps keep minor+patch grouped with majors separate; docker base image groups all updates. Schedule moved from weekly to monthly (security advisories still fire immediately). Cuts GitHub Actions consumption on routine dependency sweeps.
- `make check` now runs ruff + mypy + pytest; new `make verify` adds a container smoke. Opt-in pre-push hook at `scripts/git-hooks/pre-push` runs local checks before `git push`.

### Fixed

- (none recorded yet)

## [0.1.0] - TBD

### Added

- Initial release.
