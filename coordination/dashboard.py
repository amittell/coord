from __future__ import annotations

import html
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from coordination.deps import get_service


def _esc(s: str | None) -> str:
    return html.escape(s or "")


def _remaining(expires_at: str | None) -> str:
    if not expires_at:
        return "?"
    try:
        exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except ValueError:
        return "?"
    now = datetime.now(UTC)
    if exp <= now:
        return "expired"
    sec = int((exp - now).total_seconds())
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _ago(value: str | None, now: datetime | None = None) -> str:
    """Render an ISO timestamp as a compact 'Xs/Xm/Xh/Xd ago' string.

    The dashboard is read in a hurry; absolute UTC strings make every
    reader compute the delta in their head. Relative time + an absolute
    on hover (via <time title=...>) is the readable default. Returns
    "?" for unparseable input.
    """
    ts = _parse_iso(value)
    if ts is None:
        return "?"
    now = now or datetime.now(UTC)
    sec = int((now - ts).total_seconds())
    if sec < 0:
        return "now"
    if sec < 60:
        return f"{sec}s ago"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}m ago"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h ago"
    d, h = divmod(h, 24)
    return f"{d}d ago"


def _resolution_for_conflict(
    *,
    conflict: dict[str, Any],
    claim: dict[str, Any] | None,
    idle_timeout_sec: int,
    now: datetime,
) -> tuple[str, str]:
    """Derive a useful resolution status for a conflict at render time.

    The conflict_log.resolution column is reserved for an explicit
    resolver action that nothing in the codebase currently writes, so
    in practice it is always NULL and the dashboard column was a dead
    space. Compute a status from the linked claim's state instead.

    Returns (status_slug, human_label). Status values:

    * ``blocked`` -- holder still has the claim, requester is still
      stuck. The actionable state.
    * ``stale`` -- claim's TTL has passed but the cleanup sweep has not
      run yet, so released_at is still NULL. Effectively resolved; the
      next sweep will close it. Worth surfacing because it indicates
      cleanup lag.
    * ``ttl-expired`` -- claim was closed by the TTL sweep without the
      holder doing anything.
    * ``idle-released`` -- claim was closed by activity-based expiration
      because the holder's session went silent for longer than
      ``idle_timeout_sec``.
    * ``released`` -- claim was released voluntarily, well before TTL or
      idle thresholds. Most often: the holder saw the conflict in
      ``pending_requests`` and called ``release_session`` / explicit
      release.
    * ``missing`` -- conflict references a claim_id we don't have. The
      claim aged out of the recent-claims window we fetch, or was
      deleted manually. Surface so an operator notices schema drift.
    """
    if claim is None:
        return ("missing", "claim record not in recent window")

    released = _parse_iso(claim.get("released_at"))
    expires = _parse_iso(claim.get("expires_at"))
    last_activity = _parse_iso(claim.get("last_activity"))

    if released is None:
        if expires is not None and expires <= now:
            return ("stale", "TTL passed; cleanup sweep pending")
        return ("blocked", "holder still has the claim")

    # Released. Discriminate why.
    if expires is not None and released >= expires:
        return ("ttl-expired", "TTL sweep closed the claim")

    # Idle expiration: released_at is roughly idle_timeout_sec after
    # last_activity, well before TTL. Allow some slack for the
    # cleanup-loop interval (the sweep doesn't run continuously).
    if (
        idle_timeout_sec
        and idle_timeout_sec > 0
        and last_activity is not None
        and (released - last_activity).total_seconds() >= idle_timeout_sec - 60
    ):
        return ("idle-released", f"session idle > {idle_timeout_sec}s")

    return ("released", "holder released the claim")


def _bucket(pattern: str | None) -> str:
    p = (pattern or "").replace("\\", "/").strip("./")
    if not p:
        return "(root)"
    if p.startswith("**"):
        return "**"
    return p.split("/")[0]


def _heat_bar(count: int, max_count: int, width: int = 20) -> str:
    """Render a Unicode-block density bar.

    Eight intermediate widths thanks to ``▏▎▍▌▋▊▉█`` give a smoother
    gradient than the old ``####....`` ASCII bar -- still terminal-safe
    and copy-paste-friendly, but visually closer to a proper sparkline.
    """
    if max_count <= 0:
        return "·" * width
    fraction = count / max_count
    full = int(fraction * width)
    rem = (fraction * width) - full
    partial = ""
    if full < width:
        partial_chars = "▏▎▍▌▋▊▉"
        idx = int(rem * len(partial_chars))
        if idx > 0:
            partial = partial_chars[idx - 1]
    bar = "█" * full + partial
    return bar.ljust(width, "·")


def _recent_activity(
    *,
    claims: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    now: datetime,
    window_hours: int = 24,
) -> dict[str, Any]:
    """Summarise the last `window_hours` of claim and conflict activity.

    Pure function over already-fetched rows so the dashboard renderer can stay
    composed of small testable pieces. Rows with missing or malformed
    timestamps are silently dropped rather than crashing the whole panel.
    """
    cutoff = now - timedelta(hours=window_hours)

    fresh_claims = [
        c for c in claims if (ts := _parse_iso(c.get("created_at"))) and ts >= cutoff
    ]
    fresh_conflicts = [
        c for c in conflicts if (ts := _parse_iso(c.get("created_at"))) and ts >= cutoff
    ]

    engineers = {c.get("engineer") for c in fresh_claims if c.get("engineer")}

    by_module: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in fresh_claims:
        by_module[_bucket(c.get("pattern"))].append(c)

    top_modules: list[dict[str, Any]] = []
    for prefix, items in by_module.items():
        eng_set: set[str] = {str(name) for i in items if (name := i.get("engineer"))}
        top_modules.append(
            {"prefix": prefix, "count": len(items), "engineers": sorted(eng_set)}
        )
    top_modules.sort(key=lambda m: (-m["count"], m["prefix"]))

    return {
        "claims": len(fresh_claims),
        "conflicts": len(fresh_conflicts),
        "engineers": len(engineers),
        "top_modules": top_modules[:5],
    }


# ---------------------------------------------------------------------------
# CSS / HTML scaffolding
# ---------------------------------------------------------------------------
#
# Aesthetic: phosphor terminal × Bloomberg ops console × Edward Tufte.
# Dense, monospace, sharp edges, color reserved for signal not decoration.
# Type pairing: Major Mono Display for ALL-CAPS structural headings,
# JetBrains Mono for everything else. Both are free Google Fonts and
# distinct from the usual Inter/Space-Grotesk/Roboto defaults.

_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Major+Mono+Display&family=JetBrains+Mono:wght@300;400;500;700&display=swap');

:root {
  --bg: #0a0a08;
  --surface: #15140f;
  --surface-2: #1c1b15;
  --hairline: #2a2820;
  --hairline-bright: #3a382f;
  --fg: #dcd6c1;
  --fg-bright: #f4eed8;
  --muted: #7a7560;
  --muted-2: #5a5a52;
  --phosphor: #7fffa1;
  --amber: #ffba4d;
  --red: #ff5e5e;
  --cyan: #6cf0ff;
  --grid: 8px;
}

* { box-sizing: border-box; }

html, body {
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--fg);
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 13px;
  line-height: 1.55;
  font-feature-settings: 'liga' 0, 'calt' 0;
  -webkit-font-smoothing: antialiased;
}

/* Subtle film-grain noise overlay -- adds tactile depth without dominating.
   The data URI is a 200x200 SVG of fractal noise at low opacity. */
body::before {
  content: '';
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 1000;
  opacity: 0.05;
  mix-blend-mode: overlay;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='200' height='200'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/></filter><rect width='100%25' height='100%25' filter='url(%23n)' opacity='0.6'/></svg>");
}

main {
  max-width: 1400px;
  margin: 0 auto;
  padding: calc(var(--grid) * 4) calc(var(--grid) * 3);
}

/* ----- Console-line status bar ------------------------------------------ */

.statusbar {
  display: flex;
  align-items: center;
  gap: calc(var(--grid) * 2);
  padding: calc(var(--grid) * 1.5) calc(var(--grid) * 2);
  border: 1px solid var(--hairline-bright);
  background: var(--surface);
  margin-bottom: calc(var(--grid) * 4);
  font-size: 12px;
  color: var(--muted);
  letter-spacing: 0.04em;
  animation: panel-in 600ms cubic-bezier(0.2, 0.7, 0.2, 1) both;
}

.statusbar .seg { white-space: nowrap; }
.statusbar .seg strong { color: var(--fg-bright); font-weight: 500; }
.statusbar .sep { color: var(--hairline-bright); }
.statusbar .live::before {
  content: '●';
  color: var(--phosphor);
  margin-right: 6px;
  animation: pulse 2.4s ease-in-out infinite;
  text-shadow: 0 0 8px rgba(127, 255, 161, 0.6);
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}

/* ----- Title ------------------------------------------------------------ */

.title {
  font-family: 'Major Mono Display', monospace;
  font-size: 28px;
  font-weight: 400;
  letter-spacing: 0.02em;
  color: var(--fg-bright);
  margin: 0 0 calc(var(--grid) * 0.5);
  text-transform: lowercase;
}

.subtitle {
  color: var(--muted);
  font-size: 12px;
  letter-spacing: 0.06em;
  margin: 0 0 calc(var(--grid) * 4);
  max-width: 680px;
}

/* ----- Big-number stat blocks ------------------------------------------- */

.stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 1px;
  background: var(--hairline);
  border: 1px solid var(--hairline-bright);
  margin-bottom: calc(var(--grid) * 4);
}

.stats .block {
  background: var(--surface);
  padding: calc(var(--grid) * 2.5) calc(var(--grid) * 2);
  display: flex;
  flex-direction: column;
  gap: calc(var(--grid) * 0.5);
  animation: panel-in 700ms cubic-bezier(0.2, 0.7, 0.2, 1) both;
}
.stats .block:nth-child(1) { animation-delay: 60ms; }
.stats .block:nth-child(2) { animation-delay: 120ms; }
.stats .block:nth-child(3) { animation-delay: 180ms; }
.stats .block:nth-child(4) { animation-delay: 240ms; }

.stats .block .label {
  font-family: 'Major Mono Display', monospace;
  font-size: 11px;
  letter-spacing: 0.08em;
  color: var(--muted);
  text-transform: lowercase;
}
.stats .block .num {
  font-family: 'JetBrains Mono', monospace;
  font-size: 36px;
  font-weight: 300;
  color: var(--fg-bright);
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  line-height: 1;
}
.stats .block .num.phosphor { color: var(--phosphor); text-shadow: 0 0 16px rgba(127, 255, 161, 0.25); }
.stats .block .num.amber { color: var(--amber); }
.stats .block .num.red { color: var(--red); }
.stats .block .delta {
  font-size: 11px;
  color: var(--muted);
  letter-spacing: 0.04em;
}

/* ----- Panels (each section) -------------------------------------------- */

.panel {
  border: 1px solid var(--hairline-bright);
  background: var(--surface);
  margin-bottom: calc(var(--grid) * 4);
  animation: panel-in 700ms cubic-bezier(0.2, 0.7, 0.2, 1) both;
}
.panel:nth-of-type(1) { animation-delay: 100ms; }
.panel:nth-of-type(2) { animation-delay: 160ms; }
.panel:nth-of-type(3) { animation-delay: 220ms; }
.panel:nth-of-type(4) { animation-delay: 280ms; }
.panel:nth-of-type(5) { animation-delay: 340ms; }
.panel:nth-of-type(6) { animation-delay: 400ms; }

.panel header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: calc(var(--grid) * 1.5) calc(var(--grid) * 2);
  border-bottom: 1px solid var(--hairline);
  background: var(--surface-2);
}
.panel header h2 {
  font-family: 'Major Mono Display', monospace;
  font-size: 13px;
  font-weight: 400;
  letter-spacing: 0.1em;
  color: var(--fg-bright);
  margin: 0;
  text-transform: lowercase;
}
.panel header h2::before {
  content: '▌ ';
  color: var(--phosphor);
  text-shadow: 0 0 8px rgba(127, 255, 161, 0.4);
}
.panel header .meta {
  font-size: 11px;
  color: var(--muted);
  letter-spacing: 0.04em;
}

@keyframes panel-in {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

/* ----- Grid (two-up layout) --------------------------------------------- */

.row {
  display: grid;
  gap: calc(var(--grid) * 4);
  margin-bottom: calc(var(--grid) * 4);
}
.row.split-7-5 { grid-template-columns: 7fr 5fr; }
.row.split-1-1 { grid-template-columns: 1fr 1fr; }
.row > .panel { margin-bottom: 0; }

@media (max-width: 1100px) {
  .row.split-7-5, .row.split-1-1 { grid-template-columns: 1fr; }
  .stats { grid-template-columns: repeat(2, 1fr); }
}

/* ----- Tables ----------------------------------------------------------- */

table {
  border-collapse: collapse;
  width: 100%;
  font-size: 12px;
}
thead th {
  font-family: 'Major Mono Display', monospace;
  font-weight: 400;
  font-size: 10px;
  letter-spacing: 0.12em;
  color: var(--muted);
  text-transform: lowercase;
  text-align: left;
  padding: calc(var(--grid) * 1.25) calc(var(--grid) * 2);
  background: var(--surface-2);
  border-bottom: 1px solid var(--hairline);
}
thead th.num-col { text-align: right; }

tbody td {
  padding: calc(var(--grid) * 1.25) calc(var(--grid) * 2);
  border-bottom: 1px solid var(--hairline);
  vertical-align: top;
  color: var(--fg);
}
tbody td.num-col {
  text-align: right;
  font-variant-numeric: tabular-nums;
  color: var(--fg-bright);
}
tbody td.muted { color: var(--muted); }
tbody td code {
  font-family: 'JetBrains Mono', monospace;
  color: var(--cyan);
  background: rgba(108, 240, 255, 0.06);
  padding: 1px 6px;
  font-size: 11.5px;
}
tbody td .heat {
  color: var(--phosphor);
  letter-spacing: 0;
  white-space: pre;
  font-feature-settings: 'tnum';
}
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover td { background: rgba(127, 255, 161, 0.04); }
tbody tr td.empty {
  color: var(--muted);
  font-style: normal;
  text-align: center;
  padding: calc(var(--grid) * 3);
}

/* Status pill -- sharp rectangle with accent border, no rounded fills */
.pill {
  display: inline-block;
  padding: 1px 8px;
  border: 1px solid currentColor;
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: lowercase;
  font-family: 'Major Mono Display', monospace;
  white-space: nowrap;
}
.pill.blocked { color: var(--red); }
.pill.stale { color: var(--amber); }
.pill.ttl-expired { color: var(--muted); }
.pill.idle-released { color: var(--amber); }
.pill.released { color: var(--phosphor); }
.pill.missing { color: var(--muted-2); }
.pill.severity-soft { color: var(--cyan); }
.pill.severity-hard { color: var(--red); }
.pill.severity-shared { color: var(--amber); }
.pill.pending { color: var(--amber); }
.pill.approved { color: var(--phosphor); }
.pill.denied { color: var(--red); }
.pill.narrowed { color: var(--phosphor); border-style: dashed; }
.pill.coexist { color: var(--cyan); }
.pill.urgency-low { color: var(--muted-2); }
.pill.urgency-normal { color: var(--cyan); }
.pill.urgency-high { color: var(--amber); }
.pill.urgency-blocking { color: var(--red); }

/* Pattern code -- give patterns a subtle distinct look from inline code */
.pattern {
  font-family: 'JetBrains Mono', monospace;
  color: var(--fg-bright);
  font-size: 11.5px;
}

/* Section footer: tiny relative-time timestamp */
.panel footer {
  padding: calc(var(--grid) * 1) calc(var(--grid) * 2);
  border-top: 1px solid var(--hairline);
  font-size: 10px;
  color: var(--muted-2);
  letter-spacing: 0.06em;
  display: flex;
  justify-content: flex-end;
  gap: calc(var(--grid) * 2);
}

/* Top-modules inline list (compact alt to a table) */
.top-modules {
  display: flex;
  flex-direction: column;
  padding: calc(var(--grid) * 1.5) 0;
}
.top-modules li {
  display: grid;
  grid-template-columns: 4ch 1fr auto;
  gap: calc(var(--grid) * 2);
  padding: calc(var(--grid) * 1) calc(var(--grid) * 2);
  font-size: 12px;
  list-style: none;
  border-bottom: 1px solid var(--hairline);
}
.top-modules li:last-child { border-bottom: 0; }
.top-modules .count {
  color: var(--phosphor);
  font-variant-numeric: tabular-nums;
  text-align: right;
  font-weight: 500;
}
.top-modules .prefix { color: var(--cyan); }
.top-modules .engineers {
  color: var(--muted);
  font-size: 11px;
  text-align: right;
}

/* Footer credit line */
.foot {
  margin-top: calc(var(--grid) * 6);
  padding-top: calc(var(--grid) * 2);
  border-top: 1px solid var(--hairline);
  font-size: 10px;
  color: var(--muted-2);
  letter-spacing: 0.08em;
  text-transform: lowercase;
  display: flex;
  justify-content: space-between;
}

/* v0.18 auto-resolution heatmap */
.heatmap .hrow {
  display: grid;
  grid-template-columns: 24ch 1fr 10ch;
  gap: calc(var(--grid) * 2);
  align-items: center;
  padding: calc(var(--grid)) 0;
  border-bottom: 1px dashed var(--hairline);
  font-family: var(--mono);
  font-size: 11px;
}
.heatmap .hrow:last-child { border-bottom: none; }
.heatmap .hrepo {
  color: var(--phosphor);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.heatmap .hcells {
  display: grid;
  grid-template-columns: repeat(30, 1fr);
  gap: 2px;
}
.heatmap .hcell {
  display: block;
  width: 100%;
  height: 12px;
  border-radius: 2px;
  background: #1a1a1a;
}
.heatmap .hcell.h0 { background: #1a1a1a; }
.heatmap .hcell.h1 { background: #1f3a2a; }
.heatmap .hcell.h2 { background: #2f6440; }
.heatmap .hcell.h3 { background: #4ea870; }
.heatmap .hcell.h4 { background: #7fffa1; }
.heatmap .htotal {
  text-align: right;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}

/* v0.20 hotspot files panel */
.hotspots .hsrow {
  display: grid;
  grid-template-columns: 18ch minmax(0, 1fr) 14ch 16ch;
  gap: calc(var(--grid) * 2);
  align-items: center;
  padding: calc(var(--grid)) 0;
  border-bottom: 1px dashed var(--hairline);
  font-family: var(--mono);
  font-size: 11px;
}
.hotspots .hsrow:last-child { border-bottom: none; }
.hotspots .hsrepo {
  color: var(--phosphor);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.hotspots .hspattern {
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.hotspots .hscount {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.hotspots .hssuggest {
  text-align: center;
  font-size: 10px;
  letter-spacing: 0.04em;
  padding: 2px 6px;
  border-radius: 3px;
  border: 1px solid var(--hairline);
}
.hotspots .hssuggest.sg-split { color: #ff8a7f; border-color: #6b3a36; }
.hotspots .hssuggest.sg-shared { color: var(--cyan); border-color: #2f5466; }
.hotspots .hssuggest.sg-monitor { color: var(--muted-2); }
"""


def _pill(status: str, label: str) -> str:
    return (
        f'<span class="pill {html.escape(status)}" title="{html.escape(label)}">'
        f"{html.escape(status)}</span>"
    )


# ---------------------------------------------------------------------------
# Page render
# ---------------------------------------------------------------------------


async def render_dashboard() -> str:
    svc = get_service()
    now = datetime.now(UTC)

    rows = await svc.list_claims(active_only=True)
    conflicts = await svc.db.recent_conflicts(500)
    recent = await svc.db.list_recent_claims(500)
    activity = _recent_activity(claims=recent, conflicts=conflicts, now=now)
    repos = await svc.db.list_repos()
    idle_timeout_sec = svc.settings.idle_timeout_sec
    requests = await svc.list_requests(limit=200)
    auto_resolutions = await svc.db.count_auto_resolutions_since(window_hours=24)
    auto_resolution_series = await svc.db.daily_auto_resolutions(days=30)
    hotspot_rows = await svc.db.hotspot_files(days=30, min_attempts=5, limit=10)

    claims_by_id: dict[str, dict[str, Any]] = {
        str(c["id"]): c for c in recent if c.get("id")
    }

    # ---- big-number stats ------------------------------------------------
    open_conflicts = sum(
        1
        for c in conflicts
        if _resolution_for_conflict(
            conflict=c,
            claim=claims_by_id.get(str(c.get("claim_id"))),
            idle_timeout_sec=idle_timeout_sec,
            now=now,
        )[0]
        == "blocked"
    )
    stats_html = (
        f'<div class="block"><span class="label">repos</span>'
        f'<span class="num phosphor">{len(repos)}</span>'
        f'<span class="delta">{activity["engineers"]} engineers active 24h</span></div>'
        f'<div class="block"><span class="label">active claims</span>'
        f'<span class="num">{len(rows)}</span>'
        f'<span class="delta">{activity["claims"]} created 24h</span></div>'
        f'<div class="block"><span class="label">conflicts 24h</span>'
        f'<span class="num amber">{activity["conflicts"]}</span>'
        f'<span class="delta">{open_conflicts} still blocked</span></div>'
        f'<div class="block"><span class="label">idle-timeout</span>'
        f'<span class="num">{idle_timeout_sec // 60}m</span>'
        f'<span class="delta">session auto-release</span></div>'
    )

    # ---- repos table ------------------------------------------------------
    if repos:
        repos_html = "".join(
            "<tr>"
            f"<td><span class='pattern'>{_esc(r['repo'])}</span></td>"
            f"<td class='num-col'>{r['active_claims']}</td>"
            f"<td class='num-col'>{r['claims_24h']}</td>"
            f"<td class='num-col'>{r['engineers_24h']}</td>"
            f"<td class='muted'><time datetime='{_esc(r['last_activity'])}' "
            f"title='{_esc(r['last_activity'])}'>{_esc(_ago(r['last_activity'], now))}</time></td>"
            "</tr>"
            for r in repos
        )
    else:
        repos_html = (
            "<tr><td class='empty' colspan='5'>"
            "no repos using this service yet</td></tr>"
        )

    # ---- top modules (compact list, replaces the second 24h table) ------
    if activity["top_modules"]:
        top_modules_html = "".join(
            f"<li><span class='count'>{m['count']}</span>"
            f"<span class='prefix'>{_esc(m['prefix'])}</span>"
            f"<span class='engineers'>{_esc(', '.join(m['engineers']))}</span></li>"
            for m in activity["top_modules"]
        )
    else:
        top_modules_html = "<li class='empty'>no activity in the last 24h</li>"

    # ---- active claims ---------------------------------------------------
    # Note: v0.14.1 added a per-row Database.get_claim_symbols call so
    # symbol-scope claims can render their symbol list inline. This is
    # an N+1 query against claim_symbols; acceptable on the dashboard
    # because the active-claims set is small (typically << 100 rows) and
    # the page is an operator surface, not a hot path. If the row count
    # ever explodes we'll batch this into a single IN-clause fetch.
    if rows:
        rows_html = ""
        for r in rows:
            sev = (r.get("severity") or "soft").lower()
            sev_html = (
                f'<span class="pill severity-{html.escape(sev)}">{html.escape(sev)}</span>'
            )
            sess = r.get("session_id") or ""
            sess_short = sess[:8] if sess else ""
            sess_cell = (
                f'<td class="muted" title="{_esc(sess)}">{_esc(sess_short)}</td>'
                if sess_short
                else "<td class='muted'>—</td>"
            )
            rem = _remaining(r.get("expires_at"))
            rem_class = "muted" if rem == "expired" else ""

            scope_type = (r.get("scope_type") or "file").lower()
            if scope_type == "symbol":
                claim_id = r.get("id")
                symbol_names: list[str] = []
                if claim_id:
                    sym_rows = await svc.db.get_claim_symbols(str(claim_id))
                    symbol_names = [
                        str(s.get("symbol_name") or "")
                        for s in sym_rows
                        if s.get("symbol_name")
                    ]
                if symbol_names:
                    symbols_inline = ", ".join(_esc(n) for n in symbol_names)
                    scope_cell = (
                        "<td>symbol"
                        f"<br><em class='symbols' style='font-size:11px;color:var(--muted)'>"
                        f"{symbols_inline}</em></td>"
                    )
                else:
                    scope_cell = "<td>symbol</td>"
            else:
                scope_cell = "<td class='muted'>file</td>"

            rows_html += (
                "<tr>"
                f"<td>{_esc(r.get('engineer'))}</td>"
                f"<td>{_esc(r.get('repo')) or '<span class=muted>—</span>'}</td>"
                f"<td><span class='pattern'>{_esc(r.get('pattern'))}</span></td>"
                f"{scope_cell}"
                f"<td class='muted'>{_esc(r.get('description'))}</td>"
                f"<td class='{rem_class}'>{_esc(rem)}</td>"
                f"<td>{sev_html}</td>"
                f"{sess_cell}"
                "</tr>"
            )
    else:
        rows_html = (
            "<tr><td class='empty' colspan='8'>no active claims</td></tr>"
        )

    # ---- module heatmap (full unicode block bar) -------------------------
    counts = Counter(_bucket(r.get("pattern")) for r in rows)
    max_c = max(counts.values(), default=1)
    if counts:
        heat_rows = "".join(
            "<tr>"
            f"<td><span class='pattern'>{_esc(name)}</span></td>"
            f"<td class='num-col'>{n}</td>"
            f"<td><span class='heat'>{_heat_bar(n, max_c)}</span></td>"
            "</tr>"
            for name, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))
        )
    else:
        heat_rows = (
            "<tr><td class='empty' colspan='3'>no active claims</td></tr>"
        )

    # ---- conflict log with derived resolution ---------------------------
    if conflicts:
        conf_html = ""
        for c in conflicts:
            claim = claims_by_id.get(str(c.get("claim_id")))
            status, label = _resolution_for_conflict(
                conflict=c,
                claim=claim,
                idle_timeout_sec=idle_timeout_sec,
                now=now,
            )
            holder_engineer = (claim or {}).get("engineer") or "?"
            holder_pattern = (claim or {}).get("pattern") or "?"
            attempted_sess = c.get("attempted_session_id") or ""
            sess_short = attempted_sess[:8] if attempted_sess else ""
            conf_html += (
                "<tr>"
                f"<td class='muted'><time datetime='{_esc(c.get('created_at'))}' "
                f"title='{_esc(c.get('created_at'))}'>{_esc(_ago(c.get('created_at'), now))}</time></td>"
                f"<td>{_esc(c.get('attempted_by'))}</td>"
                f"<td><span class='pattern'>{_esc(c.get('attempted_pattern'))}</span></td>"
                f"<td class='muted'>vs {_esc(holder_engineer)}</td>"
                f"<td><span class='pattern'>{_esc(holder_pattern)}</span></td>"
                f"<td>{_pill(status, label)}</td>"
                f"<td class='muted' title='{_esc(attempted_sess)}'>{_esc(sess_short) or '—'}</td>"
                "</tr>"
            )
    else:
        conf_html = (
            "<tr><td class='empty' colspan='7'>"
            "no recent conflict attempts logged</td></tr>"
        )

    # ---- release requests (v0.9.0) --------------------------------------
    # Surface filed release-requests with their decision pill and the
    # time-to-decision latency. Dashboard is read-only: the underlying
    # state machine lives in /requests/{id}/respond and the audit
    # timeline at /requests/{id}/events.
    if requests:
        req_html = ""
        pending_count = 0
        for r in requests:
            decision = str(r.get("decision") or "pending")
            urgency = str(r.get("urgency") or "normal")
            decided_at = _parse_iso(r.get("decided_at"))
            created_at = _parse_iso(r.get("created_at"))
            if decision == "pending":
                pending_count += 1
                latency_label = "—"
            elif created_at and decided_at:
                secs = int((decided_at - created_at).total_seconds())
                if secs < 60:
                    latency_label = f"{secs}s"
                elif secs < 3600:
                    latency_label = f"{secs // 60}m {secs % 60}s"
                else:
                    latency_label = f"{secs // 3600}h {(secs % 3600) // 60}m"
            else:
                latency_label = "—"
            scope = r.get("requested_scope") or "—"
            scope_cell = (
                f"<td><span class='pattern'>{_esc(scope)}</span></td>"
                if scope != "—"
                else "<td class='muted'>—</td>"
            )
            req_html += (
                "<tr>"
                f"<td class='muted'><time datetime='{_esc(r.get('created_at'))}' "
                f"title='{_esc(r.get('created_at'))}'>{_esc(_ago(r.get('created_at'), now))}</time></td>"
                f"<td>{_esc(r.get('requester_engineer'))}</td>"
                f"<td><span class='pattern'>{_esc(r.get('requested_pattern'))}</span></td>"
                f"{scope_cell}"
                f"<td class='muted'>vs {_esc(r.get('holder_engineer') or '?')}</td>"
                f'<td><span class="pill urgency-{html.escape(urgency)}">{html.escape(urgency)}</span></td>'
                f"<td>{_pill(decision, decision)}</td>"
                f"<td class='muted'>{html.escape(latency_label)}</td>"
                "</tr>"
            )
        requests_meta = f"{len(requests)} total · {pending_count} pending"
    else:
        req_html = (
            "<tr><td class='empty' colspan='8'>"
            "no release requests filed yet</td></tr>"
        )
        requests_meta = "0 total"

    # ---- claim timeline --------------------------------------------------
    if recent:
        timeline_html = ""
        for r in recent[:50]:
            released = r.get("released_at")
            expires = r.get("expires_at")
            if released:
                rel_dt = _parse_iso(released)
                exp_dt = _parse_iso(expires)
                last_dt = _parse_iso(r.get("last_activity"))
                if exp_dt and rel_dt and rel_dt >= exp_dt:
                    end_status = ("ttl-expired", "TTL")
                elif (
                    idle_timeout_sec
                    and last_dt
                    and rel_dt
                    and (rel_dt - last_dt).total_seconds() >= idle_timeout_sec - 60
                ):
                    end_status = ("idle-released", "idle")
                else:
                    end_status = ("released", "released")
                end_html = _pill(*end_status)
            else:
                end_html = _pill("blocked", "still active") if (
                    expires
                    and (exp := _parse_iso(expires))
                    and exp > now
                ) else _pill("stale", "TTL passed; cleanup pending")
            timeline_html += (
                "<tr>"
                f"<td class='muted'><time datetime='{_esc(r.get('created_at'))}' "
                f"title='{_esc(r.get('created_at'))}'>{_esc(_ago(r.get('created_at'), now))}</time></td>"
                f"<td>{_esc(r.get('engineer'))}</td>"
                f"<td>{_esc(r.get('repo')) or '<span class=muted>—</span>'}</td>"
                f"<td><span class='pattern'>{_esc(r.get('pattern'))}</span></td>"
                f"<td>{end_html}</td>"
                f"<td class='muted'>{_esc(_ago(r.get('released_at') or r.get('expires_at'), now))}</td>"
                "</tr>"
            )
    else:
        timeline_html = (
            "<tr><td class='empty' colspan='6'>no claim history yet</td></tr>"
        )

    # ---- auto-resolutions panel (v0.14.1) -------------------------------
    # Surface the count of server-side auto-resolutions (auto-coexist and
    # auto-narrow) over the last 24 hours. These are decisions the
    # conflict engine took on its own when symbol sets were disjoint or
    # when a symbol requester landed on a narrowable file claim -- the
    # holder doesn't get blocked and no request is filed. Operators want
    # visibility into the volume so they can spot churn or
    # mis-configurations. See docs/design/sub-file-claims.md, "State
    # machine deltas".
    ac = int(auto_resolutions.get("auto_coexist", 0))
    an = int(auto_resolutions.get("auto_narrow", 0))
    auto_total = ac + an
    # v0.18: build the 30-day per-repo heatmap. Group the series by
    # repo, fill the 30 day-slots from (today - 29) .. today, and
    # render each cell as a coloured span keyed on total count.

    today = now.date()
    day_keys = [
        (today - timedelta(days=29 - i)).strftime("%Y-%m-%d")
        for i in range(30)
    ]
    series_by_repo: dict[str, dict[str, dict[str, int]]] = {}
    for row in auto_resolution_series:
        repo_key = str(row.get("repo") or "(unattributed)")
        series_by_repo.setdefault(repo_key, {})[row["date"]] = {
            "auto_coexist": int(row.get("auto_coexist") or 0),
            "auto_narrow": int(row.get("auto_narrow") or 0),
        }

    def _cell_class(count: int) -> str:
        if count <= 0:
            return "h0"
        if count <= 2:
            return "h1"
        if count <= 9:
            return "h2"
        if count <= 49:
            return "h3"
        return "h4"

    heatmap_rows: list[str] = []
    for repo_key in sorted(series_by_repo):
        days_data = series_by_repo[repo_key]
        total_coexist = sum(d.get("auto_coexist", 0) for d in days_data.values())
        total_narrow = sum(d.get("auto_narrow", 0) for d in days_data.values())
        cells = []
        for k in day_keys:
            d = days_data.get(k, {"auto_coexist": 0, "auto_narrow": 0})
            count = int(d.get("auto_coexist", 0)) + int(d.get("auto_narrow", 0))
            cls = _cell_class(count)
            title = (
                f"{k}: {count} "
                f"({d.get('auto_coexist', 0)} coexist, "
                f"{d.get('auto_narrow', 0)} narrow)"
            )
            cells.append(
                f'<span class="hcell {cls}" title="{_esc(title)}"></span>'
            )
        cells_html = "".join(cells)
        repo_label = _esc(repo_key)
        heatmap_rows.append(
            '<div class="hrow">'
            f'<div class="hrepo">{repo_label}</div>'
            f'<div class="hcells">{cells_html}</div>'
            f'<div class="htotal">{total_coexist + total_narrow} '
            f'<span class="muted">({total_coexist}c·{total_narrow}n)</span></div>'
            '</div>'
        )
    if heatmap_rows:
        heatmap_body = "".join(heatmap_rows)
    else:
        heatmap_body = (
            '<div class="empty" style="padding:calc(var(--grid) * 2)">'
            'no auto-resolutions in the last 30 days</div>'
        )
    heatmap_html = (
        '<section class="panel">'
        '<header><h2>auto-resolution heatmap (30d)</h2>'
        f'<span class="meta">last 30 days · {len(series_by_repo)} repos</span></header>'
        '<div class="heatmap" style="padding:calc(var(--grid) * 2)">'
        + heatmap_body +
        '</div>'
        '</section>'
    )

    # v0.20: hotspot panel. Files agents repeatedly 409 on are
    # candidates for shared_file rules (declared hotspots) or module
    # splits (the boundary is wrong). Suggested-action chip is purely
    # advisory; v0.20 ships read-only signal, v0.21 plans auto-promote.
    def _hotspot_suggestion(attempts: int) -> tuple[str, str]:
        if attempts >= 50:
            return ("split", "split into modules")
        if attempts >= 20:
            return ("shared", "promote to shared_file")
        return ("monitor", "monitor")

    if hotspot_rows:
        hotspot_lines: list[str] = []
        for row in hotspot_rows:
            attempts = int(row.get("attempts") or 0)
            distinct = int(row.get("distinct_attempters") or 0)
            tag, label = _hotspot_suggestion(attempts)
            repo_label = _esc(str(row.get("repo") or "(unattributed)"))
            pattern_label = _esc(str(row.get("pattern") or ""))
            hotspot_lines.append(
                '<div class="hsrow">'
                f'<div class="hsrepo">{repo_label}</div>'
                f'<div class="hspattern">{pattern_label}</div>'
                f'<div class="hscount">{attempts} '
                f'<span class="muted">({distinct} engineers)</span></div>'
                f'<div class="hssuggest sg-{tag}">{label}</div>'
                '</div>'
            )
        hotspot_body = "".join(hotspot_lines)
    else:
        hotspot_body = (
            '<div class="empty" style="padding:calc(var(--grid) * 2)">'
            'no hotspot files in the last 30 days (good!)</div>'
        )
    hotspots_html = (
        '<section class="panel">'
        '<header><h2>hotspot files (30d)</h2>'
        f'<span class="meta">{len(hotspot_rows)} files · min 5 attempts</span></header>'
        '<div class="hotspots" style="padding:calc(var(--grid) * 2)">'
        + hotspot_body +
        '</div>'
        '</section>'
    )

    auto_resolutions_html = (
        '<section class="panel">'
        '<header><h2>auto-resolutions (24h)</h2>'
        f'<span class="meta">{auto_total} total</span></header>'
        '<div style="padding:calc(var(--grid) * 2);'
        'display:flex;gap:calc(var(--grid) * 4);align-items:baseline;flex-wrap:wrap">'
        f'<span style="font-size:32px;color:var(--phosphor);'
        f'font-variant-numeric:tabular-nums">{auto_total}</span>'
        f'<span class="muted" style="font-size:12px">'
        f'<strong style="color:var(--cyan)">{ac}</strong> coexist · '
        f'<strong style="color:var(--phosphor)">{an}</strong> narrow'
        '</span>'
        '<span class="muted" style="font-size:11px;letter-spacing:0.04em;'
        'margin-left:auto;max-width:60ch;line-height:1.5">'
        '<strong>auto-coexist</strong>: server granted both symbol claims '
        'because their symbol sets did not intersect. '
        '<strong>auto-narrow</strong>: symbol requester was granted alongside '
        'an existing narrowable file claim. '
        '<a href="https://github.com/amittell/coord/blob/main/docs/design/'
        'sub-file-claims.md#state-machine-deltas" '
        'style="color:var(--cyan)">design notes</a>.'
        '</span>'
        '</div>'
        '</section>'
    )

    # ---- compose page ----------------------------------------------------
    from coordination import __version__

    now_label = now.strftime("%Y-%m-%d %H:%M:%SZ")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Coordination Dashboard</title>
  <style>{_CSS}</style>
</head>
<body>
  <main>
    <div class="statusbar">
      <span class="seg live"><strong>coord</strong>/{_esc(__version__)}</span>
      <span class="sep">│</span>
      <span class="seg">{_esc(now_label)}</span>
      <span class="sep">│</span>
      <span class="seg">{len(repos)} repos</span>
      <span class="sep">│</span>
      <span class="seg">{len(rows)} active claims</span>
      <span class="sep">│</span>
      <span class="seg">{open_conflicts} blocked</span>
    </div>

    <h1 class="title">multi-agent coordination</h1>
    <p class="subtitle">who is touching what, right now and over the last 24 hours</p>

    <div class="stats">
      {stats_html}
    </div>

    {auto_resolutions_html}

    {heatmap_html}

    {hotspots_html}

    <div class="row split-7-5">
      <section class="panel">
        <header><h2>repositories</h2><span class="meta">{len(repos)} total</span></header>
        <table>
          <thead>
            <tr>
              <th>repo</th>
              <th class="num-col">active</th>
              <th class="num-col">24h claims</th>
              <th class="num-col">24h engineers</th>
              <th>last activity</th>
            </tr>
          </thead>
          <tbody>{repos_html}</tbody>
        </table>
      </section>

      <section class="panel">
        <header><h2>top modules · 24h</h2><span class="meta">by claim count</span></header>
        <ul class="top-modules">{top_modules_html}</ul>
      </section>
    </div>

    <section class="panel">
      <header><h2>active claims</h2><span class="meta">{len(rows)} held</span></header>
      <table>
        <thead>
          <tr>
            <th>engineer</th>
            <th>repo</th>
            <th>pattern</th>
            <th>scope</th>
            <th>description</th>
            <th>time left</th>
            <th>severity</th>
            <th>session</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    </section>

    <div class="row split-1-1">
      <section class="panel">
        <header><h2>module heatmap</h2><span class="meta">first path segment</span></header>
        <table>
          <thead><tr><th>prefix</th><th class="num-col">claims</th><th>density</th></tr></thead>
          <tbody>{heat_rows}</tbody>
        </table>
      </section>

      <section class="panel">
        <header><h2>recent conflicts</h2><span class="meta">last 500 attempts</span></header>
        <table>
          <thead>
            <tr>
              <th>when</th>
              <th>attempted by</th>
              <th>their pattern</th>
              <th>holder</th>
              <th>holder pattern</th>
              <th>status</th>
              <th>session</th>
            </tr>
          </thead>
          <tbody>{conf_html}</tbody>
        </table>
      </section>
    </div>

    <section class="panel">
      <header><h2>release requests</h2><span class="meta">{requests_meta}</span></header>
      <table>
        <thead>
          <tr>
            <th>when</th>
            <th>requester</th>
            <th>their pattern</th>
            <th>scope</th>
            <th>holder</th>
            <th>urgency</th>
            <th>decision</th>
            <th>latency</th>
          </tr>
        </thead>
        <tbody>{req_html}</tbody>
      </table>
    </section>

    <section class="panel">
      <header><h2>claim timeline</h2><span class="meta">most recent 50</span></header>
      <table>
        <thead>
          <tr>
            <th>created</th>
            <th>engineer</th>
            <th>repo</th>
            <th>pattern</th>
            <th>state</th>
            <th>updated</th>
          </tr>
        </thead>
        <tbody>{timeline_html}</tbody>
      </table>
    </section>

    <footer class="foot">
      <span>coord · multi-agent coordination · {_esc(__version__)}</span>
      <span>rendered {_esc(now_label)}</span>
    </footer>
  </main>
</body>
</html>"""
