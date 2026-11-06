"""Task-level authorization scope (ARTEX ``task_scope`` equivalent).

Cairn has no asset inventory; its "assets" are the hosts a command names. Scope
therefore gates at the command layer, which is stricter than an asset-layer gate:
``curl`` to an unlisted host is denied even if nothing was ever recorded as an
asset. An empty scope means no gating, so adding one entry can never silently
block the entire task by accident.
"""

from __future__ import annotations

import ipaddress
import sqlite3

from .hosts import host_candidates


def load_scope(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM project_scope WHERE project_id = ? ORDER BY id",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _ip_in(value: str, host: str, *, cidr: bool) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    try:
        if cidr:
            return address in ipaddress.ip_network(value, strict=False)
        return address == ipaddress.ip_address(value)
    except ValueError:
        return False


def host_in_scope(host: str, entries: list[dict]) -> bool:
    host = host.lower().rstrip(".")
    for entry in entries:
        value = str(entry["value"]).lower().rstrip(".")
        kind = entry["kind"]
        if kind == "ip" and _ip_in(value, host, cidr=False):
            return True
        if kind == "cidr" and _ip_in(value, host, cidr=True):
            return True
        if kind == "host" and host == value:
            return True
        if kind == "domain" and (host == value or host.endswith("." + value)):
            return True
    return False


def first_out_of_scope_host(command: str, entries: list[dict]) -> str | None:
    """Return the first host outside an allowlist, or None when the command is fine.

    With no entries the gate is off; commands with no host (``whoami``) also
    stay allowed.
    """
    if not entries:
        return None
    for host in host_candidates(command):
        if not host_in_scope(host, entries):
            return host
    return None
