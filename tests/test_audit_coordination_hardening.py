"""Audit follow-up tests: JSON log validity, PR-comment markdown
sanitization, migration-script schema lockstep, and release/CI workflow
hygiene.

Covers:

* :class:`coordination.logging.JsonFormatter` must emit strictly valid
  JSON even when an extra carries a non-finite float (NaN/Infinity),
  which Python's default ``json.dumps`` renders as invalid-JSON tokens.
* :func:`coordination.github_adapter._render_body` must neutralize
  engineer-controlled claim fields (descriptions, paths, names) so a
  bounce comment cannot inject @mentions, links, or markdown structure
  into a PR thread.
* ``scripts/migrate_tokens_ownership.py`` must stay in lockstep with
  ``coordination.db``: its known schema version equals
  CURRENT_SCHEMA_VERSION and its expected token column list matches the
  real SQLite schema, so the cutover runbook sees no false warnings.
* ``.github/workflows/release.yml`` must guard manual dispatches against
  silently republishing an existing image version tag.
* Lean PR CI and weekly compatibility coverage together must exercise every
  Python version advertised in the pyproject classifiers without duplicating
  expensive full suites on every change.
"""

from __future__ import annotations

import importlib.util
import json
import logging as pylogging
import math
import os
import re
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from coordination.github_adapter import MARKER, _render_body, _sanitize_inline
from coordination.logging import JsonFormatter

REPO_ROOT = Path(__file__).resolve().parent.parent

_POSTGRES_SELECTED = os.environ.get("COORD_DATABASE_URL", "").startswith(
    ("postgres://", "postgresql://", "postgresql+asyncpg://")
)


def _load_migrate_script():
    """Import scripts/migrate_tokens_ownership.py as a module (scripts/ is
    not a package, so load it straight from its file path)."""
    path = REPO_ROOT / "scripts" / "migrate_tokens_ownership.py"
    spec = importlib.util.spec_from_file_location(
        "migrate_tokens_ownership", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _format_record(**extra: object) -> str:
    record = pylogging.LogRecord(
        name="coordination.test",
        level=pylogging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return JsonFormatter().format(record)


def _strict_json_loads(line: str) -> dict:
    """Parse with a strict-JSON stance: reject the NaN/Infinity tokens the
    way Loki's JSON stage or jq would."""

    def reject(token: str) -> None:
        raise AssertionError(f"non-strict JSON constant emitted: {token}")

    return json.loads(line, parse_constant=reject)


class TestJsonFormatterNonFiniteExtras:
    def test_nan_extra_emits_valid_json(self) -> None:
        line = _format_record(duration_ms=float("nan"))
        payload = _strict_json_loads(line)
        assert payload["msg"] == "hello"
        assert payload["duration_ms"] == "nan"

    def test_infinity_extras_emit_valid_json(self) -> None:
        line = _format_record(pos=float("inf"), neg=float("-inf"))
        payload = _strict_json_loads(line)
        assert payload["pos"] == "inf"
        assert payload["neg"] == "-inf"

    def test_nested_nan_extra_emits_valid_json(self) -> None:
        line = _format_record(timings={"p99": float("nan")})
        payload = _strict_json_loads(line)
        # The whole nested structure falls back to repr, keeping the
        # line strictly parseable.
        assert "nan" in payload["timings"]

    def test_finite_float_extra_survives_as_number(self) -> None:
        line = _format_record(duration_ms=12.5)
        payload = _strict_json_loads(line)
        assert payload["duration_ms"] == 12.5
        assert math.isclose(payload["duration_ms"], 12.5)


class TestRenderBodySanitization:
    def _detail(self, **entry_overrides: object) -> dict:
        entry: dict[str, object] = {
            "files": ["src/auth/login.ts"],
            "holder_engineer": "bob",
            "holder_branch": "feature/y",
            "holder_pattern": "src/auth/**",
            "holder_description": "auth refactor",
        }
        entry.update(entry_overrides)
        return {
            "repo": "octo/widgets",
            "pushing_engineer": "alice",
            "pushing_branch": "feature/x",
            "bounced": [entry],
        }

    def test_description_is_rendered_as_code_span(self) -> None:
        body = _render_body(
            self._detail(holder_description="ping @org/everyone now")
        )
        # Inside backticks GitHub does not resolve mentions.
        assert "- description: `ping @org/everyone now`" in body

    def test_description_backticks_and_newlines_are_stripped(self) -> None:
        body = _render_body(
            self._detail(
                holder_description="x` ![img](https://evil/a.png)\n# heading"
            )
        )
        # The backtick cannot terminate the code span and the newline
        # cannot open a fresh markdown block.
        assert "![img]" in body  # still present but inside the code span
        assert "- description: `x ![img](https://evil/a.png) # heading`" in body
        assert "\n# heading" not in body

    def test_path_backticks_are_stripped(self) -> None:
        body = _render_body(
            self._detail(files=["src/a`@org/everyone`.ts"])
        )
        assert "  - `src/a@org/everyone.ts`" in body
        # No stray double-backtick breakout sequence.
        assert "``" not in body

    def test_path_that_sanitizes_to_empty_is_skipped(self) -> None:
        body = _render_body(self._detail(files=["```"]))
        assert "- files:" in body
        assert "  - ``" not in body

    def test_holder_engineer_is_neutralized_in_header(self) -> None:
        body = _render_body(self._detail(holder_engineer="@org/everyone"))
        assert "### `@org/everyone`" in body

    def test_marker_still_first_line(self) -> None:
        body = _render_body(self._detail())
        assert body.splitlines()[0] == MARKER

    def test_clean_detail_renders_all_fields(self) -> None:
        body = _render_body(self._detail())
        assert "`alice` pushed to `feature/x` in `octo/widgets`" in body
        assert "### `bob` (`feature/y`)" in body
        assert "- claim: `src/auth/**`" in body
        assert "- description: `auth refactor`" in body
        assert "  - `src/auth/login.ts`" in body

    def test_sanitize_inline_collapses_whitespace(self) -> None:
        assert _sanitize_inline("a\r\n b\t\tc") == "a b c"
        assert _sanitize_inline("`` `") == ""


class TestMigrationScriptSchemaLockstep:
    def test_known_schema_version_matches_db(self) -> None:
        from coordination.db import CURRENT_SCHEMA_VERSION

        module = _load_migrate_script()
        assert module.KNOWN_SCHEMA_VERSION == CURRENT_SCHEMA_VERSION, (
            "scripts/migrate_tokens_ownership.py KNOWN_SCHEMA_VERSION has "
            "drifted behind coordination.db.CURRENT_SCHEMA_VERSION; bump the "
            "constant and extend EXPECTED_TOKEN_COLUMNS/the self-test fixture "
            "for any new engineer_tokens columns"
        )

    def test_self_test_passes(self, capsys: pytest.CaptureFixture) -> None:
        module = _load_migrate_script()
        assert module.self_test() == 0
        out = capsys.readouterr().out
        assert "SELF-TEST PASSED" in out

    @pytest.mark.skipif(
        _POSTGRES_SELECTED,
        reason="live-schema check builds a SQLite database directly",
    )
    async def test_expected_token_columns_match_live_schema(
        self, tmp_path: Path
    ) -> None:
        from coordination.db import Database

        db_path = tmp_path / "coordination.db"
        db = Database(db_path)
        await db.init()

        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute("PRAGMA table_info(engineer_tokens)")
            live_columns = {row[1] for row in cur.fetchall()}
        finally:
            conn.close()

        module = _load_migrate_script()
        assert set(module.EXPECTED_TOKEN_COLUMNS) == live_columns, (
            "EXPECTED_TOKEN_COLUMNS in scripts/migrate_tokens_ownership.py "
            "does not match the live engineer_tokens schema; the cutover "
            "script would emit spurious warnings (or miss real drift)"
        )


class TestReleaseWorkflowPublicationFence:
    def _workflow(self) -> dict:
        raw = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        return yaml.safe_load(raw)

    def test_overwrite_escape_hatch_is_removed_and_publishers_are_serialized(
        self,
    ) -> None:
        workflow = self._workflow()
        # PyYAML parses the bare `on` key as boolean True.
        triggers = workflow.get("on") or workflow.get(True)
        assert set(triggers) == {"workflow_dispatch"}
        inputs = triggers["workflow_dispatch"]["inputs"]
        assert "overwrite" not in inputs
        assert inputs["version"]["required"] is False
        assert inputs["oidc_trigger_tag"]["required"] is False
        concurrency = workflow["concurrency"]
        assert concurrency["group"] == (
            "${{ github.workflow }}-"
            "${{ inputs.oidc_trigger_tag || inputs.version }}"
        )
        assert concurrency["cancel-in-progress"] is False

    def test_candidate_is_authenticated_without_an_official_promotion(self) -> None:
        workflow = self._workflow()
        steps = workflow["jobs"]["publish-image"]["steps"]
        guard = next(
            step
            for step in steps
            if step.get("name") == "Refuse to republish an existing candidate tag"
        )
        assert 'crane digest "$CANDIDATE"' in guard["run"]
        assert "exit 1" in guard["run"]
        candidate = next(
            step for step in steps if step.get("name") == "Compose candidate image tag"
        )
        assert "candidate-${VERSION}-${COMMIT_SHA:0:12}" in candidate["run"]

        step_names = [str(step.get("name") or "") for step in steps]
        build = step_names.index("Build and push image")
        verify_attestations = step_names.index(
            "Verify both platform SBOM and provenance attestations"
        )
        verify_signature = step_names.index(
            "Sign and verify candidate digest with workflow identity"
        )
        assert build < verify_attestations < verify_signature
        assert "--builder-id \"$BUILDER_ID\"" in steps[verify_attestations]["run"]
        assert all("crane tag" not in str(step.get("run") or "") for step in steps)
        checkout = next(
            step for step in steps if step.get("name") == "Check out repository"
        )
        assert checkout["with"]["persist-credentials"] is False
        authorization_job = workflow["jobs"]["authorize-pypi"]
        assert authorization_job["if"] == (
            "inputs.oidc_trigger_tag != '' "
            "&& github.ref == 'refs/heads/main'"
        )
        assert authorization_job["permissions"] == {"contents": "read"}
        assert "environment" not in authorization_job
        authorization_steps = [
            str(step) for step in authorization_job["steps"]
        ]
        authorization_checkouts = [
            step
            for step in authorization_job["steps"]
            if str(step.get("uses") or "").startswith("actions/checkout@")
        ]
        assert len(authorization_checkouts) == 2
        assert all(
            step["with"]["persist-credentials"] is False
            for step in authorization_checkouts
        )
        authorization = next(
            index
            for index, step in enumerate(authorization_steps)
            if "Verify Vault-signed release authorization" in step
        )
        assert "cosign verify-blob" in authorization_steps[authorization]
        assert "release/coord-release.pub" in authorization_steps[authorization]
        artifact = next(
            index
            for index, step in enumerate(authorization_steps)
            if "actions/upload-artifact@" in step
        )
        assert authorization < artifact
        assert all("Classify existing PyPI" not in step for step in authorization_steps)

        gate = workflow["jobs"]["gate-pypi"]
        assert gate["needs"] == "authorize-pypi"
        assert gate["permissions"] == {"contents": "read", "actions": "read"}
        assert "environment" not in gate
        gate_steps = [str(step) for step in gate["steps"]]
        gate_checkout = next(
            index
            for index, step in enumerate(gate_steps)
            if "Check out locked-main verifier without credentials" in step
        )
        gate_download = next(
            index
            for index, step in enumerate(gate_steps)
            if "actions/download-artifact@" in step
        )
        gate_recheck = next(
            index
            for index, step in enumerate(gate_steps)
            if "Recheck exact signed package hashes" in step
        )
        gate_classifier = next(
            index
            for index, step in enumerate(gate_steps)
            if "Classify existing PyPI release" in step
        )
        assert gate_checkout < gate_download < gate_recheck < gate_classifier
        assert "scripts/check_pypi_release.py" in gate_steps[gate_classifier]
        gate_text = "\n".join(gate_steps)
        assert "release-source" not in gate_text
        assert "python -m build" not in gate_text

        pypi = workflow["jobs"]["publish-pypi"]
        assert "needs.gate-pypi.outputs.state == 'absent'" in pypi["if"]
        assert "needs.gate-pypi.outputs.state == 'partial'" in pypi["if"]
        assert "!= 'exact'" not in pypi["if"]
        assert "github.ref == 'refs/heads/main'" in pypi["if"]
        assert pypi["environment"]["name"] == "pypi"
        assert pypi["permissions"] == {"id-token": "write", "actions": "read"}
        pypi_steps = [str(step) for step in pypi["steps"]]
        publish = next(
            index
            for index, step in enumerate(pypi_steps)
            if "pypa/gh-action-pypi-publish@" in step
        )
        recheck = next(
            index
            for index, step in enumerate(pypi_steps)
            if "Recheck exact signed package hashes" in step
        )
        assert recheck < publish
        pypi_text = "\n".join(pypi_steps)
        assert "actions/download-artifact@" in pypi_text
        assert "actions/checkout@" not in pypi_text
        assert "actions/setup-python@" not in pypi_text
        assert "pip install" not in pypi_text
        assert "python -m build" not in pypi_text
        assert "scripts/" not in pypi_text
        assert "bump-manifest" not in workflow["jobs"]
        assert "git push origin HEAD:main" not in str(workflow)
        assert "password:" not in str(pypi)
        assert "SKIP_SIGNING" not in str(workflow)
        assert "git push origin \"${VERSION}\" --force" not in str(workflow)

    @pytest.mark.parametrize(
        "candidate",
        ("", "bad/value", "bad\nextra=tag", "bad\rvalue", "x" * 106),
    )
    def test_candidate_label_rejects_output_and_tag_injection(
        self,
        tmp_path: Path,
        candidate: str,
    ) -> None:
        workflow = self._workflow()
        steps = workflow["jobs"]["publish-image"]["steps"]
        resolve = next(
            step for step in steps if step.get("name") == "Resolve candidate label"
        )
        output = tmp_path / "output"
        result = subprocess.run(
            ["bash", "-c", resolve["run"]],
            env={
                **os.environ,
                "INPUT_VERSION": candidate,
                "GITHUB_OUTPUT": str(output),
            },
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0

    def test_candidate_label_produces_exactly_one_candidate_tag(
        self,
        tmp_path: Path,
    ) -> None:
        workflow = self._workflow()
        steps = workflow["jobs"]["publish-image"]["steps"]
        resolve = next(
            step for step in steps if step.get("name") == "Resolve candidate label"
        )
        compose = next(
            step for step in steps if step.get("name") == "Compose candidate image tag"
        )
        resolved = tmp_path / "resolved"
        result = subprocess.run(
            ["bash", "-c", resolve["run"]],
            env={
                **os.environ,
                "INPUT_VERSION": "v0.49.0-review1",
                "GITHUB_OUTPUT": str(resolved),
            },
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert resolved.read_text(encoding="utf-8") == "tag=v0.49.0-review1\n"

        tags = tmp_path / "tags"
        result = subprocess.run(
            ["bash", "-c", compose["run"]],
            env={
                **os.environ,
                "IMAGE": "ghcr.io/amittell/coord",
                "VERSION": "v0.49.0-review1",
                "COMMIT_SHA": "a" * 40,
                "GITHUB_OUTPUT": str(tags),
            },
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert tags.read_text(encoding="utf-8") == (
            "tags=ghcr.io/amittell/coord:"
            "candidate-v0.49.0-review1-aaaaaaaaaaaa\n"
        )

    def test_fresh_pypi_gate_ignores_poisoned_build_workspace_classifier(
        self,
        tmp_path: Path,
    ) -> None:
        workflow = self._workflow()
        gate_steps = workflow["jobs"]["gate-pypi"]["steps"]
        classifier = next(
            step
            for step in gate_steps
            if step.get("name") == "Classify existing PyPI release"
        )
        trusted = tmp_path / "scripts"
        poisoned = tmp_path / "release-source" / "scripts"
        trusted.mkdir()
        poisoned.mkdir(parents=True)
        (trusted / "check_pypi_release.py").write_text(
            "print('conflict')\n",
            encoding="utf-8",
        )
        (poisoned / "check_pypi_release.py").write_text(
            "print('absent')\n",
            encoding="utf-8",
        )
        output = tmp_path / "output"
        result = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", classifier["run"]],
            cwd=tmp_path,
            env={
                **os.environ,
                "VERSION": "0.49.0",
                "GITHUB_OUTPUT": str(output),
            },
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert not output.exists()

    @pytest.mark.skipif(
        os.name == "nt",
        reason="release workflow guard runs under the POSIX GitHub runner",
    )
    @pytest.mark.parametrize(
        ("mode", "expected_returncode", "expected_output"),
        [
            ("existing", 1, "already exists in the registry"),
            ("missing", 0, ""),
            ("transient", 1, "could not prove"),
            ("auth", 1, "could not prove"),
        ],
    )
    def test_initial_guard_fails_closed_except_for_manifest_unknown(
        self,
        tmp_path: Path,
        mode: str,
        expected_returncode: int,
        expected_output: str,
    ) -> None:
        workflow = self._workflow()
        steps = workflow["jobs"]["publish-image"]["steps"]
        guard = next(
            step
            for step in steps
            if step.get("name") == "Refuse to republish an existing candidate tag"
        )

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_crane = fake_bin / "crane"
        fake_crane.write_text(
            """#!/bin/sh
case "$FAKE_CRANE_MODE" in
  existing)
    exit 0
    ;;
  missing)
    echo '{"errors":[{"code":"MANIFEST_UNKNOWN"}]}' >&2
    exit 1
    ;;
  transient)
    echo 'ERROR: failed to do request: lookup ghcr.io: temporary failure in name resolution' >&2
    exit 1
    ;;
  auth)
    echo 'ERROR: unexpected status from HEAD request: 401 Unauthorized' >&2
    exit 1
    ;;
esac
exit 2
""",
            encoding="utf-8",
        )
        fake_crane.chmod(0o755)

        env = os.environ.copy()
        env.update(
            {
                "FAKE_CRANE_MODE": mode,
                "CANDIDATE": "ghcr.io/octo/coord:candidate-v1-deadbeef",
                "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            }
        )
        result = subprocess.run(
            [
                "bash",
                "--noprofile",
                "--norc",
                "-e",
                "-o",
                "pipefail",
                "-c",
                guard["run"],
            ],
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )

        output = result.stdout + result.stderr
        assert result.returncode == expected_returncode, output
        assert expected_output in output


class TestCiMatrixCoversClassifiers:
    @staticmethod
    def _workflow(name: str) -> dict:
        return yaml.safe_load(
            (REPO_ROOT / ".github" / "workflows" / name).read_text(
                encoding="utf-8"
            )
        )

    def test_every_python_classifier_version_is_tested(self) -> None:
        pyproject = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        classifier_versions = {
            classifier.rsplit(":: ", 1)[1]
            for classifier in pyproject["project"]["classifiers"]
            if classifier.startswith("Programming Language :: Python :: 3.")
        }

        ci = self._workflow("ci.yml")
        pr_versions = {
            str(version)
            for version in ci["jobs"]["test"]["strategy"]["matrix"][
                "python-version"
            ]
        }
        compatibility = self._workflow("compatibility.yml")
        weekly_versions = {
            str(leg["python-version"])
            for leg in compatibility["jobs"]["test"]["strategy"][
                "matrix"
            ]["include"]
        }
        tested_versions = pr_versions | weekly_versions

        missing = classifier_versions - tested_versions
        assert not missing, (
            f"pyproject.toml advertises Python {sorted(missing)} but neither "
            "PR nor weekly compatibility CI tests them"
        )

    def test_pr_full_suite_only_covers_floor_and_production(self) -> None:
        ci = self._workflow("ci.yml")
        versions = {
            str(version)
            for version in ci["jobs"]["test"]["strategy"]["matrix"][
                "python-version"
            ]
        }
        assert versions == {"3.11", "3.14"}
        assert ci["jobs"]["test"]["runs-on"] == "ubuntu-latest"

    def test_pr_platform_jobs_run_only_marked_smoke_tests(self) -> None:
        ci = self._workflow("ci.yml")
        platform = ci["jobs"]["platform"]
        assert set(platform["strategy"]["matrix"]["os"]) == {
            "macos-latest",
            "windows-latest",
        }
        commands = "\n".join(
            str(step.get("run") or "") for step in platform["steps"]
        )
        assert "pytest -q -m platform" in commands
        assert ".[dev]" not in commands
        assert "pytest-timeout" in commands

    def test_quality_is_single_job_and_includes_otel(self) -> None:
        ci = self._workflow("ci.yml")
        commands = "\n".join(
            str(step.get("run") or "")
            for step in ci["jobs"]["quality"]["steps"]
        )
        assert "ruff check ." in commands
        assert "mypy coordination" in commands
        assert "pytest tests/test_otel.py -q" in commands
        assert "otel" not in ci["jobs"]
        assert "type-check" not in ci["jobs"]

    def test_pr_ci_ignores_markdown_and_does_not_write_pip_caches(self) -> None:
        ci = self._workflow("ci.yml")
        triggers = ci.get("on") or ci.get(True)
        assert triggers["pull_request"]["paths-ignore"] == ["**/*.md"]
        setup_steps = [
            step
            for job in ci["jobs"].values()
            for step in job.get("steps", [])
            if str(step.get("uses") or "").startswith("actions/setup-python@")
        ]
        assert setup_steps
        assert all("cache" not in step.get("with", {}) for step in setup_steps)

    def test_docker_smoke_starts_authenticated_sqlite_service(self) -> None:
        ci = self._workflow("ci.yml")
        docker = ci["jobs"]["docker-build"]
        sqlite_step = next(
            step
            for step in docker["steps"]
            if step.get("name") == "Start image and verify real SQLite readiness"
        )
        commands = str(sqlite_step.get("run") or "")
        assert "--env COORD_AUTH_TOKEN=ci-smoke" in commands
        assert "/readyz" in commands
        assert "sqlite3.connect" in commands
        assert "import asyncpg" not in commands
        assert 'find_spec("asyncpg") is None' in commands
        assert "coord:ci-postgres" not in commands
        assert re.search(r"\bcoord:ci\b", commands)
        assert docker["env"]["DOCKER_BUILD_RECORD_UPLOAD"] == "false"

    def test_every_ci_job_has_a_bounded_timeout(self) -> None:
        for workflow_name in ("ci.yml", "compatibility.yml", "postgres.yml"):
            workflow = self._workflow(workflow_name)
            missing = [
                name
                for name, job in workflow["jobs"].items()
                if "timeout-minutes" not in job
            ]
            assert not missing, f"{workflow_name} has unbounded jobs: {missing}"
            assert all(
                1 <= int(job["timeout-minutes"]) <= 30
                for job in workflow["jobs"].values()
            )

    def test_ci_invokes_pytest_as_a_module_from_repo_root(self) -> None:
        offenders: list[str] = []
        for workflow_name in ("ci.yml", "compatibility.yml", "postgres.yml"):
            workflow = self._workflow(workflow_name)
            for job_name, job in workflow["jobs"].items():
                for step in job.get("steps", []):
                    command = str(step.get("run") or "")
                    if re.search(r"(?m)^\s*pytest\b", command):
                        offenders.append(f"{workflow_name}:{job_name}")
        assert not offenders, (
            "bare pytest can omit the repository root from sys.path and "
            f"break imports from scripts/: {offenders}"
        )

    def test_postgres_path_gate_has_no_stale_explicit_paths(self) -> None:
        postgres = self._workflow("postgres.yml")
        triggers = postgres.get("on") or postgres.get(True)
        paths = triggers["pull_request"]["paths"]
        explicit_paths = [path for path in paths if not set(path) & set("*?[")]
        missing = [path for path in explicit_paths if not (REPO_ROOT / path).exists()]
        assert not missing, f"postgres workflow has stale paths: {missing}"
        assert "schedule" in triggers
        assert "workflow_dispatch" in triggers
        assert "workflow_call" in triggers

    def test_postgres_path_gate_covers_backend_overlap_translation(self) -> None:
        postgres = self._workflow("postgres.yml")
        triggers = postgres.get("on") or postgres.get(True)
        for event in ("pull_request", "push"):
            assert "coordination/overlap_symbols.py" in triggers[event]["paths"]

    def test_release_publish_jobs_require_real_postgres_gate(self) -> None:
        release = self._workflow("release.yml")
        jobs = release["jobs"]
        assert jobs["postgres-gate"]["uses"] == (
            "./.github/workflows/postgres.yml"
        )
        assert jobs["publish-image"]["needs"] == "postgres-gate"
        assert jobs["authorize-pypi"]["needs"] == "postgres-gate"
        assert jobs["publish-pypi"]["needs"] == [
            "postgres-gate",
            "authorize-pypi",
            "gate-pypi",
        ]
        assert jobs["gate-pypi"]["needs"] == "authorize-pypi"
        assert jobs["verify-pypi"]["needs"] == [
            "authorize-pypi",
            "publish-pypi",
        ]


def test_release_authority_files_have_codeowners() -> None:
    codeowners = (REPO_ROOT / ".github" / "CODEOWNERS").read_text(
        encoding="utf-8"
    )
    assert "/.github/CODEOWNERS @amittell" in codeowners
    assert "/.github/workflows/release.yml @amittell" in codeowners
    assert "/scripts/release.sh @amittell" in codeowners
    assert "/release/coord-release.pub @amittell" in codeowners
