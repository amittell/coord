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
    # Sort by claim count desc, then prefix asc for stable ordering.
    top_modules.sort(key=lambda m: (-m["count"], m["prefix"]))

    return {
        "claims": len(fresh_claims),
        "conflicts": len(fresh_conflicts),
        "engineers": len(engineers),
        "top_modules": top_modules[:5],
    }


def _bucket(pattern: str | None) -> str:
    p = (pattern or "").replace("\\", "/").strip("./")
    if not p:
        return "(root)"
    if p.startswith("**"):
        return "**"
    return p.split("/")[0]


async def render_dashboard() -> str:
    svc = get_service()
    rows = await svc.list_claims(active_only=True)
    # Pull a wider window than what the timeline displays so the 24h activity
    # panel can summarise busier days without missing rows. The timeline
    # itself still renders only the most recent slice below.
    conflicts = await svc.db.recent_conflicts(500)
    recent = await svc.db.list_recent_claims(500)
    activity = _recent_activity(claims=recent, conflicts=conflicts, now=datetime.now(UTC))

    rows_html = ""
    for r in rows:
        rows_html += (
            "<tr>"
            f"<td>{_esc(r.get('engineer'))}</td>"
            f"<td><code>{_esc(r.get('pattern'))}</code></td>"
            f"<td>{_esc(r.get('description'))}</td>"
            f"<td>{_esc(r.get('expires_at'))}</td>"
            f"<td><strong>{_remaining(r.get('expires_at'))}</strong></td>"
            f"<td>{_esc(r.get('severity'))}</td>"
            "</tr>"
        )
    if not rows_html:
        rows_html = "<tr><td colspan='6'>No active claims</td></tr>"

    counts = Counter(_bucket(r.get("pattern")) for r in rows)
    max_c = max(counts.values(), default=1)
    heat_rows = ""
    for name, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        bar_w = max(1, int(12 * n / max_c))
        bar = "#" * bar_w + "." * (12 - bar_w)
        heat_rows += (
            "<tr>"
            f"<td><code>{_esc(name)}</code></td>"
            f"<td>{n}</td>"
            f"<td><code>{_esc(bar)}</code></td>"
            "</tr>"
        )
    if not heat_rows:
        heat_rows = "<tr><td colspan='3'>No active claims</td></tr>"

    conf_html = ""
    for c in conflicts:
        conf_html += (
            "<tr>"
            f"<td>{_esc(c.get('created_at'))}</td>"
            f"<td>{_esc(c.get('attempted_by'))}</td>"
            f"<td><code>{_esc(c.get('attempted_pattern'))}</code></td>"
            f"<td>{_esc(c.get('resolution'))}</td>"
            "</tr>"
        )
    if not conf_html:
        conf_html = "<tr><td colspan='4'>No recent conflict attempts logged</td></tr>"

    timeline_html = ""
    for r in recent[:40]:
        timeline_html += (
            "<tr>"
            f"<td>{_esc(r.get('created_at'))}</td>"
            f"<td>{_esc(r.get('engineer'))}</td>"
            f"<td><code>{_esc(r.get('pattern'))}</code></td>"
            f"<td>{_esc(r.get('released_at') or '')}</td>"
            f"<td>{_esc(r.get('expires_at'))}</td>"
            "</tr>"
        )
    if not timeline_html:
        timeline_html = "<tr><td colspan='5'>No claim history yet</td></tr>"

    activity_modules_html = ""
    for m in activity["top_modules"]:
        engineers_label = ", ".join(_esc(e) for e in m["engineers"]) or ""
        activity_modules_html += (
            "<tr>"
            f"<td><code>{_esc(m['prefix'])}</code></td>"
            f"<td>{m['count']}</td>"
            f"<td>{engineers_label}</td>"
            "</tr>"
        )
    if not activity_modules_html:
        activity_modules_html = (
            "<tr><td colspan='3'>No activity in the last 24h</td></tr>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Coordination Dashboard</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #0b0f14; color: #e6edf3; }}
    h1 {{ font-size: 1.5rem; }}
    h2 {{ font-size: 1.1rem; margin-top: 2rem; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
    th, td {{ border: 1px solid #30363d; padding: 0.5rem 0.6rem; text-align: left; vertical-align: top; }}
    th {{ background: #161b22; }}
    code {{ font-size: 0.85rem; }}
    .muted {{ color: #8b949e; margin-top: 0.5rem; }}
  </style>
</head>
<body>
  <h1>Coordination Dashboard</h1>
  <p class="muted">Recent activity, active claims, path-prefix heatmap, recent conflict log, and claim timeline.</p>
  <h2>Recent activity (last 24h)</h2>
  <table>
    <thead><tr><th>Claims created</th><th>Conflicts logged</th><th>Engineers active</th></tr></thead>
    <tbody><tr><td>{activity["claims"]}</td><td>{activity["conflicts"]}</td><td>{activity["engineers"]}</td></tr></tbody>
  </table>
  <table>
    <thead><tr><th>Top module (last 24h)</th><th>Claims</th><th>Engineers</th></tr></thead>
    <tbody>{activity_modules_html}</tbody>
  </table>
  <h2>Active claims</h2>
  <table>
    <thead><tr><th>Engineer</th><th>Pattern</th><th>Description</th><th>Expires (UTC)</th><th>Time left</th><th>Severity</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  <h2>Module heatmap (by first path segment)</h2>
  <table>
    <thead><tr><th>Prefix</th><th>Active claims</th><th>Bar</th></tr></thead>
    <tbody>{heat_rows}</tbody>
  </table>
  <h2>Recent conflicts (log)</h2>
  <table>
    <thead><tr><th>When</th><th>Attempted by</th><th>Pattern</th><th>Resolution</th></tr></thead>
    <tbody>{conf_html}</tbody>
  </table>
  <h2>Claim timeline (recent)</h2>
  <table>
    <thead><tr><th>Created</th><th>Engineer</th><th>Pattern</th><th>Released</th><th>Expires</th></tr></thead>
    <tbody>{timeline_html}</tbody>
  </table>
</body>
</html>"""
