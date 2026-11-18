"""The panels: MCP App resources (MCP Apps 2026-01-26), each an HTML page a tool's answer is shown in, in Claude,
which acts by calling this runtime's own tools through the host.

The secrets form (`ui://rrotor/secrets/1`, shown by `secrets_form`): a program's secrets by name, each with a field
the person types its value into. The value goes from the form to `secret_set` through the host and into the vault
(rrotord/vault.py): it is never in the chat, and Claude is told only which names were set. The page inlines the MCP
Apps client (@modelcontextprotocol/ext-apps 2.0.3, the bundle the Glyphh server's panels ship). An address carries
its version: a new panel, a new address."""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from typing import Any

SECRETS_URI = "ui://rrotor/secrets/1"
MIME = "text/html;profile=mcp-app"
RESOURCES: tuple[dict[str, str], ...] = (
    {
        "uri": SECRETS_URI,
        "name": "secrets",
        "title": "Program secrets",
        "description": "A program's secrets on this runtime: which are set, and a field to set each. What is typed "
        "goes to the runtime's vault, never into the chat.",
        "mimeType": MIME,
    },
)
PAGES: dict[str, str] = {SECRETS_URI: "secrets.html"}


@lru_cache
def html(uri: str) -> str:
    folder = resources.files("rrotord.api") / "panel"
    script = (folder / "mcp-app.js").read_text().replace("</script", "<\\/script")
    return (folder / PAGES[uri]).read_text().replace("__APP_JS__", script)


def contents(uri: str) -> dict[str, Any]:
    return {"uri": uri, "mimeType": MIME, "text": html(uri), "_meta": {"ui": {"prefersBorder": True}}}
