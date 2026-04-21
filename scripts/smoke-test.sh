#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${COORD_API_URL:-http://127.0.0.1:8080}"
TOKEN="${COORD_AUTH_TOKEN:-dev-token}"
ENGINEER_A="${COORD_SMOKE_ENGINEER_A:-smoke/alice}"
ENGINEER_B="${COORD_SMOKE_ENGINEER_B:-smoke/bob}"

tmp_claim="$(mktemp)"
tmp_conflict="$(mktemp)"

cleanup() {
  rm -f "$tmp_claim" "$tmp_conflict"
}
trap cleanup EXIT

echo "Checking readiness at $BASE_URL/readyz"
curl -fsS "$BASE_URL/readyz" >/dev/null

echo "Creating smoke claim"
curl -fsS -X POST "$BASE_URL/claims" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"engineer\": \"$ENGINEER_A\",
    \"branch\": \"smoke/test\",
    \"description\": \"smoke test claim\",
    \"claims\": [{\"type\": \"file\", \"pattern\": \"src/auth/**\"}]
  }" >"$tmp_claim"

claim_id="$(
  python - "$tmp_claim" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)
ids = data.get("claim_ids") or []
if not ids:
    raise SystemExit("No claim_ids returned from POST /claims")
print(ids[0])
PY
)"

echo "Checking overlapping conflict"
curl -fsS "$BASE_URL/conflicts?engineer=$ENGINEER_B&pattern=src/auth/login.ts" \
  -H "Authorization: Bearer $TOKEN" >"$tmp_conflict"

python - "$tmp_conflict" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)
if not data.get("has_conflicts"):
    raise SystemExit("Expected a conflict but none was reported")
PY

echo "Releasing smoke claim"
curl -fsS -X POST "$BASE_URL/claims/release" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"claim_ids\": [\"$claim_id\"], \"engineer\": \"$ENGINEER_A\"}" >/dev/null

echo "Smoke test passed"
