"""Minimal stdlib-only LSP server for exercising coordination.lsp.

Runnable as ``python tests/fake_lsp_server.py``. Speaks just enough
Content-Length-framed JSON-RPC over stdio for the client pool's
lifecycle: initialize -> initialized -> didOpen -> documentSymbol ->
shutdown -> exit. Everything interesting is driven by environment
variables the test sets before spawning:

- ``FAKE_LSP_SYMBOLS_JSON``: the raw JSON payload returned as the
  ``textDocument/documentSymbol`` result (hierarchical DocumentSymbol
  shape, flat SymbolInformation shape, or deliberate garbage for
  failure-path tests). Defaults to an empty list.
- ``FAKE_LSP_SYMBOLS_BY_FILE_JSON`` (wave 2): JSON object mapping a
  file BASENAME to the documentSymbol payload for that file. Lets one
  server process answer differently per file -- the refactor-claim
  tests need the definition file and each reference file to report
  their own symbols. A request whose URI basename is not in the map
  falls back to ``FAKE_LSP_SYMBOLS_JSON``.
- ``FAKE_LSP_REFERENCES_JSON`` (wave 2): the raw JSON payload returned
  as the ``textDocument/references`` result (``Location[]`` shape:
  ``uri`` + ``range``, or deliberate garbage). Defaults to an empty
  list.
- ``FAKE_LSP_DELAY_SEC``: float seconds to sleep before sending each
  response -- set it above the client timeout for timeout tests.
- ``FAKE_LSP_DIE_AFTER``: integer; the process exits abruptly (no
  response, no shutdown) when asked for response N+1 after having sent
  N responses. ``FAKE_LSP_DIE_AFTER=1`` answers initialize then dies on
  the first documentSymbol -- the mid-stream crash scenario.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, BinaryIO


def read_message(stdin: BinaryIO) -> dict[str, Any] | None:
    """Read one Content-Length framed message; None on EOF."""

    content_length: int | None = None
    while True:
        line = stdin.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            if content_length is not None:
                break
            continue
        name, _, value = stripped.partition(b":")
        if name.strip().lower() == b"content-length":
            content_length = int(value.strip())
    body = stdin.read(content_length)
    if not body:
        return None
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict):
        return None
    return parsed


def write_message(stdout: BinaryIO, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    stdout.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
    stdout.write(body)
    stdout.flush()


def main() -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    symbols = json.loads(os.environ.get("FAKE_LSP_SYMBOLS_JSON", "[]"))
    symbols_by_file = json.loads(
        os.environ.get("FAKE_LSP_SYMBOLS_BY_FILE_JSON", "{}") or "{}"
    )
    references = json.loads(os.environ.get("FAKE_LSP_REFERENCES_JSON", "[]"))
    delay = float(os.environ.get("FAKE_LSP_DELAY_SEC", "0") or "0")
    die_after_raw = os.environ.get("FAKE_LSP_DIE_AFTER", "")
    die_after = int(die_after_raw) if die_after_raw else None

    responses_sent = 0
    while True:
        message = read_message(stdin)
        if message is None:
            return 0
        method = message.get("method")
        msg_id = message.get("id")
        if method == "exit":
            return 0
        if msg_id is None:
            # Notifications: initialized, textDocument/didOpen, etc.
            continue
        if delay:
            time.sleep(delay)
        if die_after is not None and responses_sent >= die_after:
            # Crash abruptly mid-stream: no response, no goodbye.
            os._exit(1)
        result: Any
        if method == "initialize":
            result = {"capabilities": {}}
        elif method == "textDocument/documentSymbol":
            result = symbols
            if isinstance(symbols_by_file, dict) and symbols_by_file:
                uri = (
                    message.get("params", {})
                    .get("textDocument", {})
                    .get("uri", "")
                )
                basename = uri.rsplit("/", 1)[-1]
                if basename in symbols_by_file:
                    result = symbols_by_file[basename]
        elif method == "textDocument/references":
            result = references
        else:
            # shutdown and anything else we do not model: null result
            # keeps the client's request/response bookkeeping happy.
            result = None
        write_message(
            stdout, {"jsonrpc": "2.0", "id": msg_id, "result": result}
        )
        responses_sent += 1


if __name__ == "__main__":
    sys.exit(main())
