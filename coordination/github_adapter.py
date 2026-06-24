"""v0.34: GitHub PR-comment delivery adapter.

When ``COORD_GITHUB_TOKEN`` is set, ``push_bounced`` events are routed
through the webhook outbox with ``kind='github'`` and delivered here
instead of being POSTed to ``COORD_WEBHOOK_URL``. The delivery loop
hands us the event's ``detail`` dict; we resolve the open PR for the
pushing branch and post (or update) a single de-duplicated comment
that lists every claim the push bounced against.

De-duplication uses a hidden HTML marker embedded in the comment body
(:data:`MARKER`). On each delivery we list the PR's issue comments,
look for an existing comment carrying the marker, and PATCH it in place
when found; otherwise we POST a fresh comment. That keeps a noisy
branch from accumulating one bounce comment per push.

Network failures raise so the outbox retry/backoff path applies. A
branch with no open PR is not an error -- it is logged and treated as a
no-op (there is simply nowhere to comment).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from coordination.config import Settings

logger = logging.getLogger(__name__)

# Hidden marker embedded in every coord bounce comment. The adapter
# scans existing PR comments for this exact substring to find the one
# comment it owns, so it updates in place instead of posting duplicates.
MARKER = "<!-- coord-bounce -->"

# httpx timeout for every GitHub REST call. Matches the webhook
# delivery client so one slow GitHub response cannot stall the sweep.
_TIMEOUT = 10.0


def _headers(token: str) -> dict[str, str]:
    """Standard GitHub REST headers with bearer auth. ``X-GitHub-Api-Version``
    pins the schema so a future default-version bump cannot silently change
    the comment/PR payload shapes we rely on."""

    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _render_body(detail: dict[str, Any]) -> str:
    """Render the markdown comment body for a ``push_bounced`` detail.

    The first line is the hidden marker so the dedup scan can find this
    comment again. The visible body names the pushing engineer/branch
    and lists each blocking holder with its claim metadata and files.
    """

    repo = str(detail.get("repo") or "")
    pushing_engineer = str(detail.get("pushing_engineer") or "")
    pushing_branch = str(detail.get("pushing_branch") or "")
    bounced = detail.get("bounced") or []

    lines: list[str] = [MARKER]
    lines.append("## coord: push bounced")
    lines.append("")
    lines.append(
        f"`{pushing_engineer}` pushed to `{pushing_branch}`"
        + (f" in `{repo}`" if repo else "")
        + ", but the following files are claimed by other engineers:"
    )
    lines.append("")

    for entry in bounced:
        holder_engineer = str(entry.get("holder_engineer") or "")
        holder_branch = str(entry.get("holder_branch") or "")
        holder_pattern = str(entry.get("holder_pattern") or "")
        holder_description = str(entry.get("holder_description") or "")
        files = entry.get("files") or []

        header = f"### {holder_engineer}"
        if holder_branch:
            header += f" (`{holder_branch}`)"
        lines.append(header)
        if holder_pattern:
            lines.append(f"- claim: `{holder_pattern}`")
        if holder_description:
            lines.append(f"- description: {holder_description}")
        if files:
            lines.append("- files:")
            for path in files:
                lines.append(f"  - `{path}`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


async def _resolve_open_pr(
    client: httpx.AsyncClient,
    *,
    api_base: str,
    token: str,
    repo: str,
    branch: str,
) -> dict[str, Any] | None:
    """Return the first open PR whose head is ``branch``, or None.

    GitHub filters by ``head=<owner>:<branch>`` where the owner is the
    repository owner (the common same-repo branch case). Raises on any
    non-2xx response so the outbox retry path applies to transient API
    failures; an empty result list is a clean None (no PR to comment on).
    """

    owner = repo.split("/", 1)[0]
    response = await client.get(
        f"{api_base}/repos/{repo}/pulls",
        params={"head": f"{owner}:{branch}", "state": "open"},
        headers=_headers(token),
    )
    response.raise_for_status()
    prs = response.json()
    if not prs:
        return None
    return prs[0]


async def _find_marker_comment(
    client: httpx.AsyncClient,
    *,
    api_base: str,
    token: str,
    repo: str,
    pr_number: int,
) -> dict[str, Any] | None:
    """Scan the PR's issue comments for the one carrying :data:`MARKER`.

    Paginates with ``per_page=100`` until a short page is returned so a
    long comment thread cannot hide our marker comment past page one.
    Raises on any non-2xx response.
    """

    page = 1
    while True:
        response = await client.get(
            f"{api_base}/repos/{repo}/issues/{pr_number}/comments",
            params={"per_page": 100, "page": page},
            headers=_headers(token),
        )
        response.raise_for_status()
        comments = response.json()
        if not comments:
            return None
        for comment in comments:
            if MARKER in str(comment.get("body") or ""):
                return comment
        if len(comments) < 100:
            return None
        page += 1


async def post_bounce_comment(settings: Settings, detail: dict[str, Any]) -> None:
    """Post or update the de-duplicated bounce comment for one event.

    Resolves the open PR for ``(detail["repo"], detail["pushing_branch"])``,
    then PATCHes the existing marker comment if present or POSTs a new
    one. No open PR => log and return (not an error). Any HTTP failure
    raises so the webhook outbox retry/backoff applies.
    """

    token = (settings.github_token or "").strip()
    if not token:
        # Defensive: the delivery loop already gates on the token, but a
        # direct caller must not silently hit the API unauthenticated.
        logger.debug("post_bounce_comment: github_token empty, skipping")
        return

    api_base = (settings.github_api_base or "https://api.github.com").rstrip("/")
    repo = str(detail.get("repo") or "")
    branch = str(detail.get("pushing_branch") or "")
    if not repo or not branch:
        logger.warning(
            "post_bounce_comment: missing repo/branch in detail (repo=%r "
            "branch=%r), skipping",
            repo,
            branch,
        )
        return

    body = _render_body(detail)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        pr = await _resolve_open_pr(
            client,
            api_base=api_base,
            token=token,
            repo=repo,
            branch=branch,
        )
        if pr is None:
            logger.info(
                "post_bounce_comment: no open PR for %s head %s, nothing "
                "to comment on",
                repo,
                branch,
            )
            return

        pr_number = int(pr["number"])
        existing = await _find_marker_comment(
            client,
            api_base=api_base,
            token=token,
            repo=repo,
            pr_number=pr_number,
        )

        if existing is not None:
            comment_id = int(existing["id"])
            response = await client.patch(
                f"{api_base}/repos/{repo}/issues/comments/{comment_id}",
                json={"body": body},
                headers=_headers(token),
            )
            response.raise_for_status()
            logger.info(
                "post_bounce_comment: updated comment %s on %s#%s",
                comment_id,
                repo,
                pr_number,
            )
            return

        response = await client.post(
            f"{api_base}/repos/{repo}/issues/{pr_number}/comments",
            json={"body": body},
            headers=_headers(token),
        )
        response.raise_for_status()
        logger.info(
            "post_bounce_comment: posted new comment on %s#%s",
            repo,
            pr_number,
        )
