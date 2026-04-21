from __future__ import annotations

import html
from collections import Counter
from datetime import UTC, datetime

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
    conflicts = await svc.db.recent_conflicts(30)
    recent = await svc.db.list_recent_claims(40)

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
    for r in recent:
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
  <p class="muted">Active claims, path-prefix heatmap, recent conflict log, and claim timeline.</p>
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
