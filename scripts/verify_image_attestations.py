#!/usr/bin/env python3
"""Fail closed unless every required image platform has SBOM + provenance.

BuildKit places one attestation manifest beside each platform manifest in a
multi-platform OCI index. The index links them with
``vnd.docker.reference.digest``. Counting attestation manifests is not enough:
this verifier follows each link and requires both the SPDX SBOM and SLSA v1
provenance predicates for every requested platform.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from typing import Any

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_REQUIRED_PREDICATES = {
    "https://spdx.dev/Document",
    "https://slsa.dev/provenance/v1",
}


def _inspect_raw(reference: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--raw",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError(f"{reference} did not resolve to an OCI JSON object")
    return value


def _fetch_blob(reference: str, insecure: bool) -> bytes:
    command = ["crane"]
    if insecure:
        command.append("--insecure")
    command.extend(["blob", reference])
    return subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def verify_image_attestations(
    image: str,
    required_platforms: set[str],
    *,
    inspect_raw: Callable[[str], dict[str, Any]] = _inspect_raw,
    fetch_blob: Callable[[str, bool], bytes] = _fetch_blob,
    insecure: bool = False,
) -> None:
    match = re.fullmatch(r"(.+)@(sha256:[0-9a-f]{64})", image)
    if match is None:
        raise ValueError(
            "image must be an immutable repository@sha256:<64 lowercase hex> "
            f"reference, got {image!r}"
        )
    if not required_platforms:
        raise ValueError("at least one required platform must be supplied")
    repository = match.group(1)
    index = inspect_raw(image)
    manifests = index.get("manifests")
    if not isinstance(manifests, list):
        raise ValueError(f"{image} is not a multi-platform OCI index")

    platform_digests: dict[str, str] = {}
    attestation_digests: dict[str, str] = {}
    for item in manifests:
        if not isinstance(item, dict):
            continue
        digest = item.get("digest")
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ValueError(f"{image} contains an invalid manifest digest")
        annotations = item.get("annotations")
        if isinstance(annotations, dict) and (
            annotations.get("vnd.docker.reference.type")
            == "attestation-manifest"
        ):
            subject = annotations.get("vnd.docker.reference.digest")
            if not isinstance(subject, str) or _DIGEST.fullmatch(subject) is None:
                raise ValueError(
                    f"attestation {digest} has no valid linked subject digest"
                )
            if subject in attestation_digests:
                raise ValueError(
                    f"{image} has duplicate attestation manifests for {subject}"
                )
            attestation_digests[subject] = digest
            continue

        platform = item.get("platform")
        if not isinstance(platform, dict):
            continue
        name = f"{platform.get('os', '')}/{platform.get('architecture', '')}"
        if name not in required_platforms:
            continue
        if name in platform_digests:
            raise ValueError(f"{image} has duplicate manifests for {name}")
        platform_digests[name] = digest

    missing_platforms = required_platforms - set(platform_digests)
    if missing_platforms:
        raise ValueError(
            f"{image} is missing required platforms: {sorted(missing_platforms)}"
        )

    for platform in sorted(required_platforms):
        subject = platform_digests[platform]
        attestation = attestation_digests.get(subject)
        if attestation is None:
            raise ValueError(
                f"{image} platform {platform} ({subject}) has no linked "
                "attestation manifest"
            )
        attestation_manifest = inspect_raw(f"{repository}@{attestation}")
        layers = attestation_manifest.get("layers")
        if not isinstance(layers, list):
            raise ValueError(
                f"{image} platform {platform} attestation has no layers"
            )
        predicates: set[str] = set()
        for layer in layers:
            if (
                not isinstance(layer, dict)
                or layer.get("mediaType") != "application/vnd.in-toto+json"
            ):
                continue
            layer_digest = layer.get("digest")
            if (
                not isinstance(layer_digest, str)
                or _DIGEST.fullmatch(layer_digest) is None
            ):
                raise ValueError(
                    f"{image} platform {platform} has an invalid "
                    "in-toto layer digest"
                )
            payload = fetch_blob(
                f"{repository}@{layer_digest}",
                insecure,
            )
            actual_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
            if actual_digest != layer_digest:
                raise ValueError(
                    f"{image} platform {platform} in-toto layer content "
                    f"hashes to {actual_digest}, expected {layer_digest}"
                )
            try:
                statement = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{image} platform {platform} has malformed in-toto JSON"
                ) from exc
            if not isinstance(statement, dict):
                raise ValueError(
                    f"{image} platform {platform} in-toto payload is not "
                    "a statement object"
                )
            predicate = statement.get("predicateType")
            annotations = layer.get("annotations")
            advertised = (
                annotations.get("in-toto.io/predicate-type")
                if isinstance(annotations, dict)
                else None
            )
            if predicate != advertised:
                raise ValueError(
                    f"{image} platform {platform} predicate annotation "
                    f"{advertised!r} disagrees with payload {predicate!r}"
                )
            if not isinstance(predicate, str):
                raise ValueError(
                    f"{image} platform {platform} has no predicateType"
                )
            subjects = statement.get("subject")
            if not isinstance(subjects, list) or not subjects:
                raise ValueError(
                    f"{image} platform {platform} in-toto statement has "
                    "no subjects"
                )
            subject_hex = subject.removeprefix("sha256:")
            subject_digests = [
                item.get("digest", {}).get("sha256")
                for item in subjects
                if isinstance(item, dict)
                and isinstance(item.get("digest"), dict)
            ]
            if (
                len(subject_digests) != len(subjects)
                or any(value != subject_hex for value in subject_digests)
            ):
                raise ValueError(
                    f"{image} platform {platform} in-toto subjects do not "
                    f"match linked manifest {subject}"
                )
            predicates.add(predicate)
        missing_predicates = _REQUIRED_PREDICATES - predicates
        if missing_predicates:
            raise ValueError(
                f"{image} platform {platform} is missing attestation "
                f"predicates: {sorted(missing_predicates)}"
            )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image",
        required=True,
        help="immutable repository@sha256:digest reference",
    )
    parser.add_argument(
        "--platform",
        action="append",
        required=True,
        help="required os/architecture; repeat for every platform",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="allow plain HTTP when fetching blobs from a local test registry",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        verify_image_attestations(
            args.image,
            set(args.platform),
            insecure=args.insecure,
        )
    except (
        OSError,
        ValueError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as exc:
        raise SystemExit(f"image attestation verification failed: {exc}") from exc
    print(
        f"verified SBOM + SLSA provenance for "
        f"{', '.join(sorted(set(args.platform)))} on {args.image}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
