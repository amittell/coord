"""Audit guards for the HA-cutover manifest set and its runbook.

Locks in the fixes for the 2026-07 audit findings against
``deploy/k8s/ha-cutover/`` and ``docs/runbooks/coord-ha-cutover.md``:

1. The default container image must stay SQLite-only, while
   ``requirements-postgres.txt`` remains pinned for an explicitly built
   PostgreSQL image. The cutover manifest must warn operators not to point
   the standard image at ``COORD_DATABASE_URL``.
2. ``postgres.yaml`` must NOT bundle the stub coord-pg Secret: the runbook
   ``kubectl apply``s that file verbatim, and a bundled stub would clobber
   the real credentials with replace-me placeholders. The stub lives in
   ``secret.example.yaml`` instead.
3. The runbook's pod selectors must match the labels the manifests
   actually set (``app.kubernetes.io/name=coord``, never bare
   ``app=coord``) and guard against an empty pod lookup.
4. The runbook must not exec the ``sqlite3`` CLI inside the coord
   container -- the image (python:slim + git) does not ship it; the
   stdlib module via ``python -c`` is what works.
5. The Postgres StatefulSet image is pinned minor + digest, matching the
   digest-pinning policy the coord image follows.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
HA_DIR = REPO_ROOT / "deploy" / "k8s" / "ha-cutover"
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "coord-ha-cutover.md"
DOCKERFILE = REPO_ROOT / "Dockerfile"
REQ_POSTGRES = REPO_ROOT / "requirements-postgres.txt"


# ---------------------------------------------------------------------------
# 1. PostgreSQL is opt-in: the pin exists, but the default image stays lean.
# ---------------------------------------------------------------------------


def test_requirements_postgres_pins_asyncpg() -> None:
    text = REQ_POSTGRES.read_text(encoding="utf-8")
    pins = [
        line.strip().removesuffix("\\").strip()
        for line in text.splitlines()
        if line.strip()
        and not line.strip().startswith(("#", "--hash="))
    ]
    assert any(re.fullmatch(r"asyncpg==\d+\.\d+(\.\d+)?", p) for p in pins), (
        "requirements-postgres.txt must pin asyncpg exactly (asyncpg==X.Y.Z); "
        f"got {pins!r}. Explicit PostgreSQL builds need a reproducible driver "
        "layer even though the default image does not install it."
    )
    assert "--hash=sha256:" in text, (
        "requirements-postgres.txt must hash-lock every asyncpg distribution"
    )


def test_default_dockerfile_excludes_postgres_requirements() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    copy_lines = [
        line for line in text.splitlines() if line.startswith("COPY")
    ]
    assert all("requirements-postgres.txt" not in line for line in copy_lines), (
        "The default Dockerfile must not COPY requirements-postgres.txt; "
        "PostgreSQL is an explicit image/package extra."
    )
    assert not re.search(
        r"pip install[^&]*-r /build/requirements-postgres\.txt", text
    ), (
        "The standard release image must remain SQLite-only; build a separate "
        "PostgreSQL image when COORD_DATABASE_URL will be set."
    )


def test_cutover_deployment_warns_about_asyncpg_repin() -> None:
    """The standard release image intentionally excludes asyncpg; the
    manifest must require an explicit PostgreSQL-enabled image."""
    text = (HA_DIR / "deployment.yaml").read_text(encoding="utf-8")
    assert "asyncpg" in text and "PostgreSQL-enabled" in text, (
        "ha-cutover deployment.yaml must warn that the standard image omits "
        "asyncpg and require an explicitly PostgreSQL-enabled image."
    )
    docs = yaml.safe_load_all(text)
    deployment = next(d for d in docs if d and d.get("kind") == "Deployment")
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    assert "REPLACE_WITH_REVIEWED_IMAGE" in image
    assert not image.startswith("ghcr.io/amittell/coord:"), (
        "The unapplied HA template must fail closed, not carry a real standard "
        "SQLite image that will CrashLoop when COORD_DATABASE_URL is set."
    )


def test_cutover_deployment_sets_stable_postgres_schema() -> None:
    docs = yaml.safe_load_all(
        (HA_DIR / "deployment.yaml").read_text(encoding="utf-8")
    )
    deployment = next(d for d in docs if d and d.get("kind") == "Deployment")
    env = deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    schema = next(
        (item.get("value") for item in env if item.get("name") == "COORD_POSTGRES_SCHEMA"),
        None,
    )
    assert schema == "coord", (
        "The HA deployment must pin COORD_POSTGRES_SCHEMA=coord so every "
        "replica and the migration runbook address the same durable namespace."
    )


# ---------------------------------------------------------------------------
# 2. Secret handling: the apply-able manifest carries no credentials stub.
# ---------------------------------------------------------------------------


def test_postgres_manifest_contains_no_secret() -> None:
    text = (HA_DIR / "postgres.yaml").read_text(encoding="utf-8")
    docs = [d for d in yaml.safe_load_all(text) if d]
    kinds = [d.get("kind") for d in docs]
    assert "Secret" not in kinds, (
        "postgres.yaml is kubectl-applied verbatim by the cutover runbook; a "
        "bundled coord-pg Secret would clobber the real credentials with "
        "placeholders. Keep the stub in secret.example.yaml only."
    )
    assert "replace-me" not in text, (
        "postgres.yaml must not carry placeholder credential values."
    )
    # The rest of the manifest set is still intact.
    assert {"Service", "StatefulSet", "NetworkPolicy"} <= set(kinds)


def test_secret_example_is_a_placeholder_stub() -> None:
    path = HA_DIR / "secret.example.yaml"
    text = path.read_text(encoding="utf-8")
    docs = [d for d in yaml.safe_load_all(text) if d]
    assert len(docs) == 1 and docs[0]["kind"] == "Secret"
    assert docs[0]["metadata"]["name"] == "coord-pg"
    string_data = docs[0]["stringData"]
    assert set(string_data) == {
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "COORD_DATABASE_URL",
    }
    for key, value in string_data.items():
        assert "replace-me" in value, (
            f"secret.example.yaml {key} must stay a replace-me placeholder; "
            "never commit real credentials."
        )


def test_runbook_never_applies_the_example_secret() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert not re.search(r"apply\s+-f\s+\S*secret\.example\.yaml", text), (
        "The runbook must never `kubectl apply` secret.example.yaml; its "
        "placeholders would clobber the real coord-pg Secret."
    )
    assert "secret.example.yaml" in text, (
        "The runbook preconditions should point operators at "
        "secret.example.yaml for the required coord-pg keys."
    )


def test_runbook_bootstraps_and_migrates_one_explicit_schema() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    bootstrap = 'asyncio.run(build_service().db.init())'
    first_import = "python3 scripts/migrate_tokens_ownership.py"
    assert bootstrap in text
    assert text.index(bootstrap) < text.index(first_import), (
        "The target PostgreSQL schema/tables must be initialized before the "
        "durable-state importer addresses them."
    )
    assert "COORD_POSTGRES_SCHEMA=\"$PG_SCHEMA\"" in text
    assert text.count('--postgres-schema "$PG_SCHEMA"') >= 3
    assert "${PG_SCHEMA}.engineer_tokens" in text
    assert '"<PG_SCHEMA>"."engineer_tokens"' in text
    assert "Unqualified target tables are a failure" in text


def test_runbook_uses_a_local_port_forward_for_operator_postgres_commands() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    bootstrap = text.index('asyncio.run(build_service().db.init())')
    forward = text.index("port-forward pod/coord-postgres-0")
    assert "@127.0.0.1:15432/coord" in text
    assert "PG_FORWARD_PID=$!" in text
    assert forward < bootstrap


def test_runbook_imports_a_fresh_post_drain_snapshot() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "same Bash session" in text
    assert "COORD_DEPLOYMENT_ARGO_APP=cluster-coord-deployment" in text
    hard_cutover = text.index("## 4. Hard cutover")
    verify = text.index("## 5. Verify")
    cutover = text[hard_cutover:verify]
    disable_argocd = cutover.index(
        'patch application "$COORD_DEPLOYMENT_ARGO_APP"'
    )
    scale_down = cutover.index("scale deploy/coord --replicas=0")
    pods_deleted = cutover.index("wait_for_no_coord_pods", scale_down)
    final_snapshot = cutover.index("coordination.final-cutover.db")
    final_import = cutover.index("--sqlite ./coordination.final-cutover.db")
    assert disable_argocd < scale_down < pods_deleted < final_snapshot < final_import
    assert "persistentVolumeClaim:" in cutover
    assert "claimName: coord-data" in cutover
    assert "--sqlite ./coordination.pre-cutover.db" not in cutover


def test_runbook_waits_for_postgres_pods_to_delete_before_rollback() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    rollback = text[text.index("## 6. Rollback") :]
    disable_argocd = rollback.index(
        'patch application "$COORD_DEPLOYMENT_ARGO_APP"'
    )
    scale_down = rollback.index("scale deploy/coord --replicas=0")
    pods_deleted = rollback.index("wait_for_no_coord_pods", scale_down)
    sqlite_apply = rollback.index('apply -f "$SQLITE_ROLLBACK_MANIFEST"')
    assert disable_argocd < scale_down < pods_deleted < sqlite_apply


def test_runbook_freezes_durable_mutations_for_executable_rollback() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "Token create/rotate/revoke and ownership-config changes are frozen" in text
    assert 'test -s "$SQLITE_ROLLBACK_MANIFEST"' in text
    assert "post-cutover-tokens.txt" not in text
    rollback = text[text.index("## 6. Rollback") :]
    assert "partial token dump" in rollback
    assert "rollback guarantee is no longer valid" in rollback


# ---------------------------------------------------------------------------
# 3. Runbook selectors match the labels the manifests actually set.
# ---------------------------------------------------------------------------


def test_runbook_uses_real_pod_labels() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "-l app=coord" not in text, (
        "No coord pod carries a bare app=coord label; selectors must use "
        "app.kubernetes.io/name=coord or every kubectl step resolves empty."
    )
    assert "-l app.kubernetes.io/name=coord" in text


def test_manifest_pod_labels_back_the_runbook_selector() -> None:
    for manifest in ("deployment.yaml",):
        docs = yaml.safe_load_all(
            (HA_DIR / manifest).read_text(encoding="utf-8")
        )
        for doc in docs:
            if doc and doc.get("kind") == "Deployment":
                labels = doc["spec"]["template"]["metadata"]["labels"]
                assert labels.get("app.kubernetes.io/name") == "coord"


def test_runbook_guards_empty_pod_lookup() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert 'test -n "$SQLITE_POD"' in text, (
        "The runbook must fail fast when the pod lookup resolves empty "
        "instead of exec-ing into nothing mid-maintenance-window."
    )


# ---------------------------------------------------------------------------
# 4. Runbook does not exec a CLI the image does not ship.
# ---------------------------------------------------------------------------


def test_runbook_does_not_exec_sqlite3_cli_in_pod() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert 'sqlite3 "$SQLITE_PATH"' not in text, (
        "The coord image (python:slim + git) does not ship the sqlite3 CLI; "
        "in-pod steps must go through `python -c` and the stdlib module."
    )
    # The replacement path is present: stdlib backup API and the read-only
    # outbox query, both driven via python -c.
    assert "src.backup(dst)" in text
    assert "FROM webhook_outbox GROUP BY status" in text
    assert text.count("python -c") >= 2


# ---------------------------------------------------------------------------
# 5. Postgres image pinning.
# ---------------------------------------------------------------------------


def test_postgres_image_pinned_minor_and_digest() -> None:
    docs = yaml.safe_load_all(
        (HA_DIR / "postgres.yaml").read_text(encoding="utf-8")
    )
    images = [
        c["image"]
        for d in docs
        if d and d.get("kind") == "StatefulSet"
        for c in d["spec"]["template"]["spec"]["containers"]
    ]
    assert images, "postgres.yaml StatefulSet must define a container image"
    for image in images:
        assert re.fullmatch(r"postgres:16\.\d+@sha256:[0-9a-f]{64}", image), (
            f"Postgres image {image!r} must be pinned minor + digest "
            "(postgres:16.N@sha256:...), matching the coord image's "
            "digest-pinning policy; a floating tag plus IfNotPresent lets "
            "rescheduled pods silently change Postgres versions."
        )


# ---------------------------------------------------------------------------
# Leader-lease deploy guidance.
# ---------------------------------------------------------------------------


def test_runbook_notes_scale_to_zero_for_leader_lease_fix() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "leader-lease" in text and "scale-to-zero" in text, (
        "The runbook must note that the first deploy of the leader-lease "
        "fix should be a scale-to-zero bounce, not a RollingUpdate, when "
        "background-loop continuity matters."
    )
