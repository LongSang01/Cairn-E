"""Web-front configuration shared by the dispatcher (parses it) and the server (enforces it).

The ``web.auth`` block used to exist only for the nginx front end, which read the same
``dispatch.yaml`` with an ``awk`` script and rendered an ``.htpasswd``. The server now
enforces it directly, so the schema lives in ``contracts`` rather than in either side:
the dispatcher still validates the whole file strictly, and the server validates only
the ``web`` block it actually needs.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WebAuthUser(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    password: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_credentials(self) -> WebAuthUser:
        # Basic Auth has no escaping for ':' in the user name, and a leading '-' would
        # make an entry look like a YAML list marker to a human reading the file.
        if self.name.startswith("-") or ":" in self.name or "\t" in self.name:
            raise ValueError('user name must not start with "-" or contain ":" or tabs')
        if "\t" in self.password:
            raise ValueError("password must not contain tabs")
        return self


class WebAuthConfig(BaseModel):
    """HTTP Basic Auth users for the UI. Passwords stay in memory as written."""

    model_config = ConfigDict(extra="forbid")

    realm: str = Field(min_length=1, default="Cairn")
    users: list[WebAuthUser] = Field(default_factory=list)


class WebConfig(BaseModel):
    """Web front settings; only ``auth`` matters to the server."""

    model_config = ConfigDict(extra="forbid")

    auth: WebAuthConfig | None = None
