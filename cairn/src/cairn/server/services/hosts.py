"""Extract candidate hosts from a shell command for scope / rule matching.

Deliberately conservative: a host must start at a token boundary (or after an
optional URL scheme), so file names such as ``/var/log/app.log`` or ``report.json``
are never mistaken for out-of-scope targets. This is not a shell parser — it is
enough to gate the usual ``curl``/``nmap``/``sqlmap`` forms.
"""

from __future__ import annotations

import re

# Requires whitespace, quote, @ or ``=http(s)://`` before the token; stops before a
# port, path, query, fragment or the next token. The ``=`` form is limited to a full
# URL on purpose: ``--url=https://x`` is a target, while ``--output=report.txt`` is a
# file name and must not become one.
_HOST_RE = re.compile(
    r"(?:^|[\s\"'@]|=(?=https?://))(?:https?://)?"
    r"(\[[0-9a-fA-F:]+\]|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+|\d{1,3}(?:\.\d{1,3}){3})"
    r"(?=[:/?#\s\"']|$)"
)


def host_candidates(command: str) -> list[str]:
    """Bracketed IPv6 literals are supported; unbracketed ones are not (ambiguous)."""
    hosts: set[str] = set()
    for match in _HOST_RE.finditer(command):
        raw = match.group(1)
        if raw.startswith("[") and raw.endswith("]"):
            host = raw[1:-1].lower()
        else:
            host = raw.split(":", 1)[0].rstrip(".").lower()
        if host:
            hosts.add(host)
    return sorted(hosts)
