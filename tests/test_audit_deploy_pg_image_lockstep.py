"""Audit: CI and the HA-cutover manifest must run the same Postgres build.

The CI ``test-postgres`` job's service container and the cutover
StatefulSet (deploy/k8s/ha-cutover/postgres.yaml) are both pinned to a
minor version + digest. If either side drifts, CI stops exercising the
exact Postgres build prod runs and the pin comment in ci.yml becomes a
lie. This lockstep test fails the build until both are bumped together.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"
PG_MANIFEST = ROOT / "deploy" / "k8s" / "ha-cutover" / "postgres.yaml"

_PINNED = re.compile(r"^postgres:\d+\.\d+@sha256:[0-9a-f]{64}$")


def _ci_postgres_image() -> str:
    doc = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    for job in doc["jobs"].values():
        services = job.get("services") or {}
        pg = services.get("postgres")
        if pg:
            return str(pg["image"])
    raise AssertionError(
        "no job in .github/workflows/ci.yml declares a postgres service "
        "container; the test-postgres job is expected to."
    )


def _manifest_postgres_image() -> str:
    docs = [
        d
        for d in yaml.safe_load_all(PG_MANIFEST.read_text(encoding="utf-8"))
        if d
    ]
    for doc in docs:
        if doc.get("kind") != "StatefulSet":
            continue
        containers = doc["spec"]["template"]["spec"]["containers"]
        for c in containers:
            image = str(c.get("image", ""))
            if image.startswith("postgres:"):
                return image
    raise AssertionError(
        "no postgres container image found in the ha-cutover StatefulSet."
    )


def test_ci_postgres_image_is_digest_pinned() -> None:
    image = _ci_postgres_image()
    assert _PINNED.match(image), (
        f"CI postgres service image {image!r} must be pinned as "
        "postgres:<major>.<minor>@sha256:<digest> so CI cannot silently "
        "float to a different build than prod."
    )


def test_manifest_postgres_image_is_digest_pinned() -> None:
    image = _manifest_postgres_image()
    assert _PINNED.match(image), (
        f"ha-cutover postgres image {image!r} must be pinned as "
        "postgres:<major>.<minor>@sha256:<digest> per the repo's "
        "digest-pinning policy."
    )


def test_ci_and_manifest_postgres_images_match() -> None:
    ci_image = _ci_postgres_image()
    manifest_image = _manifest_postgres_image()
    assert ci_image == manifest_image, (
        f"CI runs {ci_image!r} but the cutover manifest deploys "
        f"{manifest_image!r}. Bump both together (the ci.yml pin comment "
        "promises they match)."
    )
