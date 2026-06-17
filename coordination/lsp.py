"""Async LSP client pool for span-accurate symbol claims (v0.31).

The coordination server talks to real language servers (pylsp,
typescript-language-server, gopls) over the standard JSON-RPC-over-stdio
LSP transport. Wave 1 answers "where does this symbol live in this file
right now?" (``textDocument/documentSymbol``); wave 2 adds "who calls
it?" (``textDocument/references``) on the same pool, feeding the
callsite-enrichment and refactor-claim machinery in the service layer.

Design posture, in order of importance:

1. **Never hurt the claim path.** Every public method on the pool
   returns ``None`` on any failure and never raises to callers. The
   service layer treats ``None`` as "no LSP today" and falls back to
   parser spans / the v0.17 validation behaviour. A broken or missing
   language server must never turn a claim into a 500.
2. **Fail fast when the server is sick.** A per-(language, repo_root)
   circuit breaker counts consecutive failures; once
   ``lsp_circuit_failure_threshold`` is hit the circuit opens for
   ``lsp_circuit_cooldown_sec`` and every call short-circuits to
   ``None`` without touching a subprocess. A spawn failure (binary not
   installed) opens the circuit immediately -- retrying an uninstalled
   binary on every claim would only add latency.
3. **One server per (language, repo_root), serialized requests.** LSP
   servers are stateful and most tolerate exactly one rootUri per
   process, so the pool keys clients on the pair. An ``asyncio.Lock``
   per client serializes requests; we deliberately do not multiplex,
   because the request volume here is one documentSymbol per claimed
   file per claim call.

Span convention (mirrors the v16 migration): lines are converted to
1-based at this boundary so everything north of the pool speaks
operator-friendly line numbers; columns stay 0-based exactly as LSP
reports them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

from coordination.config import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "LspClient",
    "LspClientPool",
    "get_lsp_pool",
    "language_for_path",
]


# Extension -> pool language key. Anything not listed gets no LSP at
# all (the parser path still works for it if a backend exists).
# .js/.jsx ride on typescript-language-server, which speaks plain
# JavaScript happily -- one server, one pool entry.
_LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".go": "go",
}


def language_for_path(file_path: str) -> str | None:
    """Map a file path to the pool's language key by extension, or
    ``None`` when no language server is registered for it."""

    suffix = Path(file_path).suffix.lower()
    return _LANGUAGE_BY_EXTENSION.get(suffix)


# ---------------------------------------------------------------------------
# JSON-RPC framing (Content-Length headers, the base LSP transport)
# ---------------------------------------------------------------------------


def _encode_message(payload: dict[str, Any]) -> bytes:
    """Serialize one JSON-RPC message with its Content-Length header."""

    body = json.dumps(payload).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


async def _read_message(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    """Read one Content-Length-framed JSON-RPC message from ``reader``.

    Returns ``None`` on clean EOF before any header byte. Tolerates the
    optional Content-Type header some servers emit; header names are
    case-insensitive per the LSP spec. Raises on malformed framing or
    mid-message EOF -- the caller treats any exception as a dead client.
    """

    content_length: int | None = None
    saw_header = False
    while True:
        line = await reader.readline()
        if not line:
            if saw_header:
                raise ConnectionError("LSP stream closed mid-headers")
            return None
        stripped = line.strip()
        if not stripped:
            if not saw_header:
                # Stray blank line between messages; keep scanning.
                continue
            break
        saw_header = True
        name, _, value = stripped.partition(b":")
        if name.strip().lower() == b"content-length":
            content_length = int(value.strip())
    if content_length is None:
        raise ConnectionError("LSP message missing Content-Length header")
    body = await reader.readexactly(content_length)
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ConnectionError("LSP message body is not a JSON object")
    return parsed


# ---------------------------------------------------------------------------
# documentSymbol flattening
# ---------------------------------------------------------------------------


def _flatten_document_symbols(raw: Any) -> list[dict[str, Any]] | None:
    """Flatten a ``textDocument/documentSymbol`` result to span dicts.

    Servers return one of two shapes: hierarchical ``DocumentSymbol[]``
    (has ``range`` and optional ``children``) or flat
    ``SymbolInformation[]`` (has ``location`` and optional
    ``containerName``). Both are normalised to::

        {name, kind, parent_path, start_line, start_col, end_line, end_col}

    where ``parent_path`` is the ``'::'``-joined ancestor chain (or
    ``None`` for top-level) so entries line up with coord's
    ``Outer::Inner::method`` claim notation, and ``start_line`` /
    ``end_line`` are converted to 1-based here, once, at the boundary.
    Columns stay 0-based as the server reported them.

    Returns ``None`` for anything that is not a well-formed symbol
    list -- garbage from a misbehaving server is a failure, not an
    empty result.
    """

    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    try:
        _flatten_into(raw, None, out)
    except (KeyError, TypeError, ValueError):
        return None
    return out


def _flatten_into(
    items: list[Any],
    parent_path: str | None,
    out: list[dict[str, Any]],
) -> None:
    for item in items:
        if not isinstance(item, dict):
            raise TypeError("symbol entry is not an object")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise TypeError("symbol entry has no usable name")
        if "location" in item:
            # SymbolInformation: flat shape, parent comes from
            # containerName (a single name, not a path -- the flat
            # shape simply cannot express deeper nesting).
            rng = item["location"]["range"]
            parent = item.get("containerName") or None
        else:
            rng = item["range"]
            parent = parent_path
        out.append(
            {
                "name": name,
                "kind": item.get("kind"),
                "parent_path": parent,
                "start_line": int(rng["start"]["line"]) + 1,
                "start_col": int(rng["start"]["character"]),
                "end_line": int(rng["end"]["line"]) + 1,
                "end_col": int(rng["end"]["character"]),
            }
        )
        children = item.get("children")
        if children:
            child_parent = name if parent is None else f"{parent}::{name}"
            _flatten_into(children, child_parent, out)


# ---------------------------------------------------------------------------
# references normalisation
# ---------------------------------------------------------------------------


def _uri_to_path(uri: str) -> Path:
    """Convert a ``file://`` URI to a local Path. Raises ``ValueError``
    for any other scheme -- a reference into an unsaved buffer or a
    virtual document is something we cannot claim, and the caller
    treats the raise as protocol garbage."""

    parts = urlsplit(uri)
    if parts.scheme != "file":
        raise ValueError(f"non-file URI in references result: {uri!r}")
    # url2pathname handles the platform quirks unquote alone does not:
    # on Windows a ``file:///C:/x`` URI has path ``/C:/x`` (a leading
    # slash before the drive), and ``Path('/C:/x')`` mangles it into the
    # drive-relative ``C:x``. url2pathname strips that slash and emits a
    # real ``C:\x``; on POSIX it is an unquoting no-op.
    return Path(url2pathname(parts.path))


def relpath_under_root(path: str | Path, root: str | Path) -> str | None:
    """Return ``path`` rendered repo-root-relative with posix separators
    when it lands under ``root``, else None.

    Uses ``os.path.relpath`` over realpath'd operands rather than
    ``Path.relative_to``. On Windows ``relative_to`` compares path
    components case-sensitively, so it wrongly rejects a path that sits
    under the root but differs only in drive-letter or component case --
    the repo's long-standing Windows CI failure. ``os.path.relpath``
    (ntpath) compares case-insensitively; a path on a different drive
    raises ValueError, which we treat as "outside the root". realpath on
    both operands also collapses the macOS ``/var`` -> ``/private/var``
    symlink so the comparison is stable on every platform."""
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    except (OSError, ValueError):
        return None
    if rel == os.curdir:
        return ""
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return Path(rel).as_posix()


def _normalize_references(raw: Any, repo_root: Path) -> list[dict[str, Any]] | None:
    """Normalise a ``textDocument/references`` result to callsite dicts.

    The wire shape is ``Location[]`` (``uri`` + ``range``) or ``null``
    when the server found nothing. Each location becomes::

        {file_path, line, character}

    where ``file_path`` is repo-root-relative (posix separators) when
    the URI lands under ``repo_root`` and absolute otherwise, ``line``
    is converted to 1-based here at the boundary (mirroring the
    documentSymbol convention) and ``character`` stays 0-based exactly
    as the server reported it.

    Returns ``None`` for any shape that is not a well-formed location
    list -- garbage is a failure, not an empty result. A ``null``
    result is a legitimate "no references" answer and maps to ``[]``.
    """

    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    try:
        for item in raw:
            if not isinstance(item, dict):
                raise TypeError("references entry is not an object")
            src = _uri_to_path(str(item["uri"]))
            rel = relpath_under_root(src, repo_root)
            rendered = rel if rel is not None else Path(src).resolve().as_posix()
            start = item["range"]["start"]
            out.append(
                {
                    "file_path": rendered,
                    "line": int(start["line"]) + 1,
                    "character": int(start["character"]),
                }
            )
    except (KeyError, TypeError, ValueError):
        return None
    return out


# ---------------------------------------------------------------------------
# Single client: one language server subprocess
# ---------------------------------------------------------------------------


class LspClient:
    """One language-server subprocess speaking LSP over stdio.

    Requests are serialized through ``self._lock`` -- no multiplexing.
    A request that fails (timeout, crash, protocol garbage) leaves the
    stream in an unknown state, so the pool discards the whole client
    on any exception rather than trying to resynchronise.
    """

    def __init__(
        self,
        *,
        command: str,
        repo_root: Path,
        language: str,
        request_timeout_sec: float,
    ) -> None:
        self.command = command
        self.repo_root = repo_root
        self.language = language
        self.request_timeout_sec = request_timeout_sec
        self.process: asyncio.subprocess.Process | None = None
        self.last_used = time.monotonic()
        self._next_id = 0
        self._lock = asyncio.Lock()
        # uri -> (st_mtime_ns, st_size) at the moment we last sent the
        # file's content. Per the LSP contract a server owns the text of
        # an open document and never re-reads disk, so an open-once set
        # would serve stale content for the whole client lifetime --
        # silently defeating the rename sweep (the symbol would still
        # "exist" in the server's stale copy). Re-open on signature
        # change instead.
        self._opened: dict[str, tuple[int, int]] = {}
        self._stderr_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Spawn the server and run the initialize handshake.

        The command string is shlex-split and exec'd directly -- never
        through a shell -- so a hostile COORD_LSP_COMMAND_* value cannot
        smuggle shell syntax past us.
        """

        argv = shlex.split(self.command)
        if not argv:
            raise FileNotFoundError("empty LSP command")
        self.process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.repo_root),
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        # Minimal capabilities: we only ever ask for documentSymbol, so
        # advertising nothing keeps every server on its simplest path.
        await self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self.repo_root.resolve().as_uri(),
                "capabilities": {},
            },
        )
        await self.notify("initialized", {})

    async def _drain_stderr(self) -> None:
        """Pump the server's stderr to the debug log so a chatty server
        cannot fill its pipe and deadlock against our reads."""

        proc = self.process
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                logger.debug(
                    "lsp[%s] stderr: %s",
                    self.language,
                    line.decode("utf-8", errors="replace").rstrip(),
                )
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            return

    async def _write_message(self, payload: dict[str, Any]) -> None:
        proc = self.process
        if proc is None or proc.stdin is None:
            raise ConnectionError("LSP client has no live process")
        proc.stdin.write(_encode_message(payload))
        await proc.stdin.drain()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a notification (no id, no response expected)."""

        await self._write_message(
            {"jsonrpc": "2.0", "method": method, "params": params}
        )

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        """Send a request and await its response, serialized and bounded
        by ``request_timeout_sec``. Raises on timeout, transport death,
        or an LSP error response; the pool maps every raise to a
        circuit-breaker failure."""

        async with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            return await asyncio.wait_for(
                self._roundtrip(msg_id, method, params),
                timeout=self.request_timeout_sec,
            )

    async def _roundtrip(
        self, msg_id: int, method: str, params: dict[str, Any]
    ) -> Any:
        proc = self.process
        if proc is None or proc.stdout is None:
            raise ConnectionError("LSP client has no live process")
        await self._write_message(
            {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        )
        while True:
            message = await _read_message(proc.stdout)
            if message is None:
                raise ConnectionError("LSP server closed its stdout")
            if message.get("id") == msg_id and "method" not in message:
                if "error" in message:
                    raise RuntimeError(f"LSP error response: {message['error']}")
                return message.get("result")
            if "method" in message and "id" in message:
                # Server -> client request (registerCapability and
                # friends). We implement none of them; refuse politely
                # so the server does not hang waiting for an answer.
                await self._write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "method not supported by coord",
                        },
                    }
                )
                continue
            # Server notifications (diagnostics, progress, logMessage)
            # and stale responses from a previous timed-out request are
            # dropped on the floor.

    async def _ensure_open(self, file_path: Path) -> str:
        """Send ``textDocument/didOpen`` with the on-disk content and
        return the file's URI, re-opening when the file changed on disk
        since we last sent it. didOpen is technically optional for
        documentSymbol in some servers but pylsp requires the document,
        and the open-with-disk-content dance works for all three
        supported servers.

        Freshness is tracked per URI as (mtime_ns, size): on change we
        send didClose followed by a fresh didOpen, which every sync
        mode accepts (unlike full-text didChange, which gopls's
        incremental sync does not guarantee). The stat runs BEFORE the
        read so a write racing the read causes a harmless re-open on
        the next call rather than a missed update."""

        resolved = file_path.resolve()
        uri = resolved.as_uri()
        st = resolved.stat()
        sig = (st.st_mtime_ns, st.st_size)
        prev = self._opened.get(uri)
        if prev == sig:
            return uri
        if prev is not None:
            await self.notify(
                "textDocument/didClose", {"textDocument": {"uri": uri}}
            )
        text = resolved.read_text(encoding="utf-8", errors="replace")
        await self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": self.language,
                    "version": 1,
                    "text": text,
                }
            },
        )
        self._opened[uri] = sig
        return uri

    async def document_symbols(self, file_path: Path) -> Any:
        """Raw ``textDocument/documentSymbol`` result for ``file_path``."""

        uri = await self._ensure_open(file_path)
        result = await self.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": uri}},
        )
        self.last_used = time.monotonic()
        return result

    async def references(
        self, file_path: Path, line_1based: int, character: int
    ) -> Any:
        """Raw ``textDocument/references`` result for the position
        ``(line_1based, character)`` in ``file_path``.

        The position converts back to LSP's 0-based lines here, at the
        same boundary that converted them to 1-based on the way out, so
        a documentSymbol span can be fed straight back in.
        ``includeDeclaration`` is False: the caller already holds the
        definition (it is the claimed symbol itself) and only wants the
        callsites.
        """

        uri = await self._ensure_open(file_path)
        result = await self.request(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {
                    "line": max(0, int(line_1based) - 1),
                    "character": int(character),
                },
                "context": {"includeDeclaration": False},
            },
        )
        self.last_used = time.monotonic()
        return result

    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def close(self) -> None:
        """Best-effort orderly shutdown, escalating to SIGTERM/SIGKILL.

        Never raises: close() runs from cleanup paths that must not
        fail. The shutdown/exit handshake gets one second; a server
        that ignores it gets terminated, and one that ignores SIGTERM
        gets killed. ``process.wait()`` is always awaited so no zombie
        survives the pool.
        """

        proc = self.process
        if proc is not None:
            try:
                if proc.returncode is None:
                    try:
                        await asyncio.wait_for(
                            self._shutdown_handshake(), timeout=1.0
                        )
                    except Exception:  # noqa: BLE001
                        pass
                if proc.returncode is None:
                    proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._stderr_task = None

    async def _shutdown_handshake(self) -> None:
        await self.request("shutdown", {})
        await self.notify("exit", {})


# ---------------------------------------------------------------------------
# Pool with per-(language, repo_root) circuit breaker
# ---------------------------------------------------------------------------


@dataclass
class _Circuit:
    """Consecutive-failure circuit breaker state for one pool key.

    ``open_until`` is a ``time.monotonic`` deadline; 0.0 means closed.
    After the cooldown elapses the circuit is probed again -- one
    success resets the count, one more failure re-opens immediately
    because the count is still at threshold.
    """

    consecutive_failures: int = 0
    open_until: float = 0.0


class LspClientPool:
    """Get-or-spawn pool of :class:`LspClient` keyed on
    ``(language, repo_root)``.

    All public methods swallow every failure: ``document_symbols``
    returns ``None``, the shutdown helpers just log. ``spawn_count``
    is a test observability hook -- it counts spawn *attempts*, so
    tests can assert "the circuit short-circuited without touching a
    subprocess" directly instead of inferring it from wall-clock time.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._clients: dict[tuple[str, str], LspClient] = {}
        self._circuits: dict[tuple[str, str], _Circuit] = {}
        self._lock = asyncio.Lock()
        self.spawn_count = 0

    def _command_for(self, language: str) -> str | None:
        return {
            "python": self.settings.lsp_command_python,
            "typescript": self.settings.lsp_command_typescript,
            "go": self.settings.lsp_command_go,
        }.get(language)

    async def _prepare_call(
        self,
        repo_root: Path,
        language: str,
        file_path: str | Path,
    ) -> tuple[tuple[str, str], _Circuit, LspClient, Path] | None:
        """Shared front half of every pool request: circuit check,
        command lookup, file resolution, get-or-spawn. Returns the
        ``(key, circuit, client, resolved_path)`` tuple on success and
        ``None`` on any failure -- spawn failures are recorded against
        the circuit here so callers only own their request's failure
        handling."""

        key = (language, str(Path(repo_root).resolve()))
        circuit = self._circuits.setdefault(key, _Circuit())
        if circuit.open_until > time.monotonic():
            return None
        command = self._command_for(language)
        if command is None:
            return None
        resolved = Path(file_path)
        if not resolved.is_absolute():
            resolved = Path(repo_root) / resolved
        if not resolved.is_file():
            # Caller-input problem, not a server problem: claims may
            # reference files landing in the same commit. Does not
            # count against the circuit.
            return None

        try:
            client = await self._get_or_spawn(key, command, Path(repo_root))
        except TimeoutError as exc:
            # Careful ordering: builtin TimeoutError subclasses OSError
            # on 3.11+, but an initialize-handshake timeout is a slow
            # server, not a missing binary -- it counts like any other
            # request failure instead of opening the circuit instantly.
            self._record_failure(key, circuit, exc=exc, open_now=False)
            return None
        except (OSError, ValueError) as exc:
            # Binary missing / not executable / unsplittable command:
            # nothing about retrying will fix this within the cooldown,
            # so the circuit opens immediately.
            self._record_failure(key, circuit, exc=exc, open_now=True)
            return None
        except Exception as exc:  # noqa: BLE001
            # Spawn succeeded but the handshake failed some other way:
            # counts like any other request failure.
            self._record_failure(key, circuit, exc=exc, open_now=False)
            return None
        return key, circuit, client, resolved

    async def document_symbols(
        self,
        repo_root: Path,
        language: str,
        file_path: str | Path,
    ) -> list[dict[str, Any]] | None:
        """Flattened documentSymbol entries for ``file_path``, or
        ``None`` on any failure (circuit open, spawn failure, timeout,
        crash, protocol garbage). Relative paths resolve against
        ``repo_root``."""

        prepared = await self._prepare_call(repo_root, language, file_path)
        if prepared is None:
            return None
        key, circuit, client, resolved = prepared

        try:
            raw = await client.document_symbols(resolved)
        except Exception as exc:  # noqa: BLE001
            # The stream state is unknown after any failure; discard
            # the client wholesale rather than resynchronising.
            await self._discard_client(key)
            self._record_failure(key, circuit, exc=exc, open_now=False)
            return None

        flattened = _flatten_document_symbols(raw)
        if flattened is None:
            self._record_failure(
                key,
                circuit,
                exc=ValueError("unrecognised documentSymbol result shape"),
                open_now=False,
            )
            return None
        circuit.consecutive_failures = 0
        return flattened

    async def references(
        self,
        repo_root: Path,
        language: str,
        file_path: str | Path,
        line_1based: int,
        col_0based: int,
    ) -> list[dict[str, Any]] | None:
        """Normalised ``textDocument/references`` entries for the
        position ``(line_1based, col_0based)`` in ``file_path``, or
        ``None`` on any failure -- exactly the same circuit / discard
        semantics as :meth:`document_symbols`. Each entry is
        ``{file_path, line, character}`` with ``file_path`` rendered
        repo-root-relative when the location lands under ``repo_root``
        and absolute otherwise (see :func:`_normalize_references`)."""

        prepared = await self._prepare_call(repo_root, language, file_path)
        if prepared is None:
            return None
        key, circuit, client, resolved = prepared

        try:
            raw = await client.references(resolved, line_1based, col_0based)
        except Exception as exc:  # noqa: BLE001
            # The stream state is unknown after any failure; discard
            # the client wholesale rather than resynchronising.
            await self._discard_client(key)
            self._record_failure(key, circuit, exc=exc, open_now=False)
            return None

        normalized = _normalize_references(raw, Path(repo_root))
        if normalized is None:
            self._record_failure(
                key,
                circuit,
                exc=ValueError("unrecognised references result shape"),
                open_now=False,
            )
            return None
        circuit.consecutive_failures = 0
        return normalized

    async def _get_or_spawn(
        self, key: tuple[str, str], command: str, repo_root: Path
    ) -> LspClient:
        async with self._lock:
            client = self._clients.get(key)
            if client is not None and client.is_alive():
                return client
            if client is not None:
                # Dead leftover from a crash; reap before respawning.
                await client.close()
                self._clients.pop(key, None)
            self.spawn_count += 1
            client = LspClient(
                command=command,
                repo_root=repo_root,
                language=key[0],
                request_timeout_sec=self.settings.lsp_request_timeout_sec,
            )
            try:
                await client.start()
            except BaseException:
                await client.close()
                raise
            self._clients[key] = client
            return client

    async def _discard_client(self, key: tuple[str, str]) -> None:
        async with self._lock:
            client = self._clients.pop(key, None)
        if client is not None:
            await client.close()

    def _record_failure(
        self,
        key: tuple[str, str],
        circuit: _Circuit,
        *,
        exc: BaseException,
        open_now: bool,
    ) -> None:
        circuit.consecutive_failures += 1
        threshold = self.settings.lsp_circuit_failure_threshold
        should_open = open_now or circuit.consecutive_failures >= threshold
        if should_open and circuit.open_until <= time.monotonic():
            circuit.open_until = (
                time.monotonic() + self.settings.lsp_circuit_cooldown_sec
            )
            # Info once per circuit-open so the operator learns the LSP
            # is down without a per-claim log storm; the individual
            # failures stay at debug.
            logger.info(
                "LSP circuit opened for %s (failure %d: %s); "
                "falling back to parser spans for %ds",
                key,
                circuit.consecutive_failures,
                exc,
                self.settings.lsp_circuit_cooldown_sec,
            )
        else:
            logger.debug(
                "LSP failure %d for %s: %s",
                circuit.consecutive_failures,
                key,
                exc,
            )

    async def shutdown_idle(self, now: float | None = None) -> None:
        """Reap clients idle past ``lsp_idle_shutdown_sec``. ``now`` is
        injectable (monotonic seconds) so tests do not have to sleep."""

        try:
            cutoff = (
                now if now is not None else time.monotonic()
            ) - self.settings.lsp_idle_shutdown_sec
            async with self._lock:
                idle = [
                    (key, client)
                    for key, client in self._clients.items()
                    if client.last_used <= cutoff
                ]
                for key, _client in idle:
                    self._clients.pop(key, None)
            for _key, client in idle:
                await client.close()
        except Exception:  # noqa: BLE001
            logger.debug("shutdown_idle swallowed an error", exc_info=True)

    async def shutdown_all(self) -> None:
        """Close every pooled client. Never raises."""

        try:
            async with self._lock:
                clients = list(self._clients.values())
                self._clients.clear()
            for client in clients:
                await client.close()
        except Exception:  # noqa: BLE001
            logger.debug("shutdown_all swallowed an error", exc_info=True)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_POOL: LspClientPool | None = None


def get_lsp_pool(settings: Settings) -> LspClientPool:
    """Process-wide pool accessor. Built once from the first settings
    object seen; coord settings are immutable for the life of the
    process so rebuilding on settings change is deliberately not
    supported. Tests construct their own pools or call
    :func:`_reset_pool`."""

    global _POOL
    if _POOL is None:
        _POOL = LspClientPool(settings)
    return _POOL


def _reset_pool() -> None:
    """Test hook: drop the singleton so the next get_lsp_pool() call
    rebuilds from fresh settings. Callers own shutting down the old
    pool's subprocesses first."""

    global _POOL
    _POOL = None
