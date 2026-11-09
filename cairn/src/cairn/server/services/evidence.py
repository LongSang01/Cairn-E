"""Frozen HTTP evidence, lifted out of a run's transcript.

The worker CLIs print whatever their tools printed, so the same request shows up
in three shapes: raw `curl -i` output, verbose `curl -v` (`> GET ...` /
`< HTTP/1.1 200`), or a `requests`-style dump (`<Response [200]>`). Everything
here is a best-effort line scan. When nothing matches, the caller keeps the
finding tentative: an unreproducible finding is better than an invented one.

Rows are keyed by `(project_id, session_id, seq, idx)` so re-uploading the same
transcript batch cannot duplicate evidence, and `digest` lets a finding cite the
exact bytes it was built from.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass

from cairn.contracts.models import MAX_DOCUMENT_CHARS, EvidenceRef

from .timeutil import utcnow

_METHODS = "GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE|CONNECT"

_REQUEST_LINE = re.compile(rf"^({_METHODS})\s+(\S+)\s+HTTP/\d(?:\.\d)?\s*$")
_VERBOSE_REQUEST = re.compile(rf"^>\s+({_METHODS})\s+(\S+)\s+HTTP/")
_STATUS_LINE = re.compile(r"^HTTP/\d(?:\.\d)?\s+(\d{3})\b")
_VERBOSE_STATUS = re.compile(r"^<\s+HTTP/\d(?:\.\d)?\s+(\d{3})\b")
_RESPONSE_REPR = re.compile(r"<Response \[(\d{3})\]")
_URL_FALLBACK = re.compile(r"https?://[^\s'\"<>]+")

# Transcript roles whose content can carry raw tool output.
EVIDENCE_ROLES = ("tool", "tool_result")


@dataclass(slots=True)
class ParsedExchange:
    """One request/response pair found in a single message."""

    method: str | None
    url: str | None
    status: int | None
    request: str
    response: str

    @property
    def digest(self) -> str:
        payload = "\n".join(
            [
                f"{self.method or ''} {self.url or ''} {self.status or ''}".strip(),
                self.request,
                self.response,
            ]
        )
        return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def _clip(text: str) -> str:
    if len(text) <= MAX_DOCUMENT_CHARS:
        return text
    return text[:MAX_DOCUMENT_CHARS] + "\n[truncated]"


def parse_http_exchanges(text: str) -> list[ParsedExchange]:
    """Return every request/response pair visible in `text`, in transcript order."""
    if not text:
        return []

    lines = text.splitlines()
    exchanges: list[ParsedExchange] = []
    current: _Builder | None = None

    for index, line in enumerate(lines):
        request = _REQUEST_LINE.match(line.strip())
        verbose_request = None if request else _VERBOSE_REQUEST.match(line.strip())
        if request or verbose_request:
            if current is not None:
                exchanges.append(current.finish())
            match = request or verbose_request
            assert match is not None  # narrowed by the branch above
            current = _Builder(method=match.group(1), url=match.group(2), start=index)
            current.lines.append(line)
            continue

        if current is None:
            continue

        status = _STATUS_LINE.match(line.strip())
        verbose_status = None if status else _VERBOSE_STATUS.match(line.strip())
        if status or verbose_status:
            match = status or verbose_status
            assert match is not None
            if current.status is None:
                current.status = int(match.group(1))
                current.response_start = len(current.lines)
            current.lines.append(line)
            continue

        current.lines.append(line)

    if current is not None:
        exchanges.append(current.finish())

    exchanges = [exchange for exchange in exchanges if exchange.request or exchange.response]
    if exchanges:
        return exchanges
    # No request line (e.g. a `requests` dump): fall back to the response repr.
    return _parse_json_fallback(text)


def _absolute_url(path: str, host: str | None) -> str:
    if "://" in path or not host:
        return path
    if not path.startswith("/"):
        path = f"/{path}"
    return f"http://{host}{path}"


def _parse_json_fallback(text: str) -> list[ParsedExchange]:
    """`requests` prints a status repr without a request line; keep the URL nearby."""
    found: list[ParsedExchange] = []
    last_url: str | None = None
    for line in text.splitlines():
        url_match = _URL_FALLBACK.search(line)
        if url_match:
            last_url = url_match.group(0)
        status_match = _RESPONSE_REPR.search(line)
        if status_match:
            found.append(
                ParsedExchange(
                    method=None,
                    url=last_url,
                    status=int(status_match.group(1)),
                    request=last_url or "",
                    response=line.strip(),
                )
            )
    return found


class _Builder:
    """Accumulates the lines of one exchange until the next request line."""

    __slots__ = ("lines", "method", "response_start", "start", "status", "url")

    def __init__(self, *, method: str, url: str, start: int) -> None:
        self.method = method
        self.url = url
        self.status: int | None = None
        self.lines: list[str] = []
        self.start = start
        self.response_start: int | None = None

    def finish(self) -> ParsedExchange:
        if self.response_start is None:
            request = "\n".join(self.lines)
            response = ""
        else:
            request = "\n".join(self.lines[: self.response_start])
            response = "\n".join(self.lines[self.response_start :])
        return ParsedExchange(
            method=self.method,
            # A request line only carries the path; the Host header supplies the host,
            # so findings can cite `http://host/path` instead of a bare path.
            url=_absolute_url(self.url, self._host()),
            status=self.status,
            request=_clip(request),
            response=_clip(response),
        )

    def _host(self) -> str | None:
        for line in self.lines:
            # `curl -v` prefixes both directions: `> Host: example.com`.
            candidate = line.lstrip("<> ").strip()
            if candidate.lower().startswith("host:"):
                return candidate.split(":", 1)[1].strip() or None
        return None


def store_exchanges(
    conn: sqlite3.Connection,
    project_id: str,
    session_id: str,
    seq: int,
    role: str,
    content: str,
) -> int:
    """Parse one transcript message and persist whatever HTTP pairs it contains."""
    if role not in EVIDENCE_ROLES or not content:
        return 0

    exchanges = parse_http_exchanges(content)
    if not exchanges:
        return 0

    now = utcnow()
    for idx, exchange in enumerate(exchanges):
        conn.execute(
            """
            INSERT OR REPLACE INTO http_exchanges (
                project_id, session_id, seq, idx, method, url, status,
                request, response, digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                session_id,
                seq,
                idx,
                exchange.method,
                exchange.url,
                exchange.status,
                exchange.request,
                exchange.response,
                exchange.digest,
                now,
            ),
        )
    return len(exchanges)


def exchange_evidence_id(session_id: str, seq: int, idx: int) -> str:
    """Stable id a finding cites for one parsed exchange."""
    return f"{session_id}:{seq}:{idx}"


def get_exchange(conn: sqlite3.Connection, project_id: str, session_id: str, seq: int, idx: int):
    return conn.execute(
        "SELECT * FROM http_exchanges WHERE project_id = ? AND session_id = ? AND seq = ?"
        " AND idx = ?",
        (project_id, session_id, seq, idx),
    ).fetchone()


def parse_evidence_ref(conn: sqlite3.Connection, project_id: str, ref: str):
    """Resolve an `exchange` reference id; None means it does not exist."""
    parts = ref.split(":")
    if len(parts) != 3:
        return None
    session_id, seq_text, idx_text = parts
    try:
        seq = int(seq_text)
        idx = int(idx_text)
    except ValueError:
        return None
    return get_exchange(conn, project_id, session_id, seq, idx)


def find_exchange_by_url(conn: sqlite3.Connection, project_id: str, url: str):
    """Resolve an exchange reference given as a URL instead of a positional id.

    Workers cannot know the `session:seq:idx` id, but they do know the URL they
    just tested. An exact match wins; otherwise the newest exchange whose URL
    starts with the given text.
    """
    exact = conn.execute(
        "SELECT * FROM http_exchanges WHERE project_id = ? AND url = ?"
        " ORDER BY seq DESC, idx DESC LIMIT 1",
        (project_id, url),
    ).fetchone()
    if exact is not None:
        return exact
    prefix = conn.execute(
        "SELECT * FROM http_exchanges WHERE project_id = ? AND url LIKE ?"
        " ORDER BY seq DESC, idx DESC LIMIT 1",
        (project_id, f"{url}%"),
    ).fetchone()
    if prefix is not None:
        return prefix
    if "://" not in url:
        # A bare path such as `/login.php`: match the tail of the captured URL.
        return conn.execute(
            "SELECT * FROM http_exchanges WHERE project_id = ? AND url LIKE ?"
            " ORDER BY seq DESC, idx DESC LIMIT 1",
            (project_id, f"%{url}"),
        ).fetchone()
    return None


def resolve_evidence(
    conn: sqlite3.Connection, project_id: str, refs: list[EvidenceRef]
) -> list[EvidenceRef]:
    """Keep only the references that point at something stored.

    An `exchange` ref is either `session:seq:idx` or the request URL; a
    `document` ref is a document id; a `seq` ref is a transcript sequence number.
    Unknown kinds or dangling ids are dropped rather than failing the whole
    submission: the caller downgrades the finding when the list ends up empty.
    """
    resolved: list[EvidenceRef] = []
    for ref in refs:
        if ref.kind == "exchange":
            row = parse_evidence_ref(conn, project_id, ref.ref)
            if row is None:
                row = find_exchange_by_url(conn, project_id, ref.ref)
            if row is None:
                continue
            exchange_id = f"{row['session_id']}:{row['seq']}:{row['idx']}"
            resolved.append(EvidenceRef(kind="exchange", ref=exchange_id, digest=row["digest"]))
        elif ref.kind == "document":
            exists = conn.execute(
                "SELECT 1 FROM documents WHERE project_id = ? AND id = ?",
                (project_id, ref.ref),
            ).fetchone()
            if exists is None:
                continue
            resolved.append(EvidenceRef(kind="document", ref=ref.ref, digest=ref.digest))
        elif ref.kind == "seq":
            exists = conn.execute(
                "SELECT 1 FROM session_messages WHERE project_id = ? AND seq = ?",
                (project_id, _as_int(ref.ref)),
            ).fetchone()
            if exists is None:
                continue
            resolved.append(EvidenceRef(kind="seq", ref=ref.ref, digest=ref.digest))
    return resolved


def store_finding_evidence(
    conn: sqlite3.Connection, project_id: str, finding_id: str, refs: list[EvidenceRef]
) -> None:
    for ref in refs:
        conn.execute(
            "INSERT OR REPLACE INTO finding_evidence (project_id, finding_id, kind, ref, digest)"
            " VALUES (?, ?, ?, ?, ?)",
            (project_id, finding_id, ref.kind, ref.ref, ref.digest),
        )


def build_finding_evidence(
    conn: sqlite3.Connection, project_id: str, finding_ids: list[str]
) -> dict[str, list[EvidenceRef]]:
    """One round trip for every finding a caller is about to render."""
    if not finding_ids:
        return {}
    placeholders = ",".join("?" for _ in finding_ids)
    rows = conn.execute(
        f"SELECT finding_id, kind, ref, digest FROM finding_evidence"
        f" WHERE project_id = ? AND finding_id IN ({placeholders})"
        f" ORDER BY rowid",
        (project_id, *finding_ids),
    ).fetchall()
    grouped: dict[str, list[EvidenceRef]] = {}
    for row in rows:
        grouped.setdefault(row["finding_id"], []).append(
            EvidenceRef(kind=row["kind"], ref=row["ref"], digest=row["digest"])
        )
    return grouped


def _as_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1
