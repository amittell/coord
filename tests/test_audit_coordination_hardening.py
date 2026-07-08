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
* The CI test matrix must exercise every Python version advertised in
  the pyproject classifiers.
"""

from __future__ import annotations

import importlib.util
import json
import logging as pylogging
import math
import os
import sqlite3
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


class TestReleaseWorkflowDispatchGuard:
    def _workflow(self) -> dict:
        raw = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        return yaml.safe_load(raw)

    def test_overwrite_input_defined(self) -> None:
        workflow = self._workflow()
        # PyYAML parses the bare `on` key as boolean True.
        triggers = workflow.get("on") or workflow.get(True)
        inputs = triggers["workflow_dispatch"]["inputs"]
        assert "overwrite" in inputs
        assert inputs["overwrite"]["type"] == "boolean"
        assert inputs["overwrite"].get("default") is False

    def test_guard_step_refuses_existing_tag_on_dispatch(self) -> None:
        workflow = self._workflow()
        steps = workflow["jobs"]["publish-image"]["steps"]
        guards = [
            step
            for step in steps
            if "imagetools inspect" in str(step.get("run") or "")
        ]
        assert len(guards) == 1, (
            "release.yml must carry exactly one registry-tag republish guard"
        )
        guard = guards[0]
        condition = str(guard.get("if") or "")
        assert "workflow_dispatch" in condition
        assert "inputs.overwrite != true" in condition
        assert "exit 1" in guard["run"]


class TestCiMatrixCoversClassifiers:
    def test_every_python_classifier_version_is_tested(self) -> None:
        pyproject = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        classifier_versions = {
            classifier.rsplit(":: ", 1)[1]
            for classifier in pyproject["project"]["classifiers"]
            if classifier.startswith("Programming Language :: Python :: 3.")
        }

        ci = yaml.safe_load(
            (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
                encoding="utf-8"
            )
        )
        matrix = ci["jobs"]["test"]["strategy"]["matrix"]["include"]
        tested_versions = {str(leg["python-version"]) for leg in matrix}

        missing = classifier_versions - tested_versions
        assert not missing, (
            f"pyproject.toml advertises Python {sorted(missing)} but ci.yml "
            "never tests them; add matrix legs or drop the classifiers"
        )
