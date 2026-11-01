"""Apps: Claude's pages published to this rrotor and served to the org's people (docs/architecture.md, 5.8; the
server's docs/specs/artifacts.md, 4). Publishing takes the page's files and makes a version of `"kind": "app"`
(rrotord/deploy.py, `from_files`); serving answers the active version's `src/` to a browser whose sign-in the
gate admitted (rrotord/browser.py), when the catalogue says the version is approved (rrotord/artifacts.py) and the
viewer's role or groups the app's `roles` allow; the page's calls run the app's functions as the viewer."""

from __future__ import annotations

import base64
import binascii
import html
import json
import mimetypes
import re
from pathlib import Path, PurePosixPath
from typing import Any

from rrotord import manifest as manifests
from rrotord.artifacts import Artifacts
from rrotord.errors import BadRequest, RrotorError
from rrotord.programs import Programs, Version

FILES_MAX, BYTES_MAX = 64, 16 * 1024 * 1024
ROLES_MAX = 50
CDN = "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://cdn.tailwindcss.com https://code.jquery.com https://unpkg.com"
CSP = (
    "default-src 'none'; "
    f"script-src 'self' 'unsafe-inline' 'unsafe-eval' {CDN}; "
    f"style-src 'self' 'unsafe-inline' {CDN} https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
PRIVATE = frozenset({"glyphh.json", "program.py", "requirements.lock", "builtin.json"})  # never served
TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".txt": "text/plain",
    ".md": "text/html",
    ".wasm": "application/wasm",
    ".map": "application/json",
}
# A Markdown page inside rrotor's document shell: rendered by marked from a CDN the CSP allows; its source shown as
# text when the library does not load.
SHELL = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>'
    "<style>body{{max-width:72ch;margin:2rem auto;padding:0 1rem;font-family:system-ui,sans-serif;line-height:1.5;"
    "color:#1a1a1a;background:#fff}}pre{{overflow:auto;background:#f4f4f4;padding:1rem;border-radius:6px}}"
    "code{{font-family:ui-monospace,monospace}}</style>"
    '<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/15.0.12/marked.min.js"></script></head>'
    '<body><main id="page"></main><textarea id="source" hidden>{source}</textarea>'
    '<script>const s=document.getElementById("source").value,p=document.getElementById("page");'
    'if(window.marked){{p.innerHTML=window.marked.parse(s)}}else{{const e=document.createElement("pre");'
    "e.textContent=s;p.append(e)}}</script></body></html>"
)
REFUSED = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title}</title>'
    "<style>body{{max-width:40rem;margin:4rem auto;padding:0 1rem;font-family:system-ui,sans-serif;color:#1a1a1a}}"
    "</style></head><body><h1>{title}</h1><p>{message}</p>{action}</body></html>"
)
SIGN_OUT = '<form method="post" action="/auth/logout"><button type="submit">Sign out</button></form>'


class AppError(RrotorError):
    """A refusal to open an app's page: answered as a page (a browser is reading), its code in the error too."""


def check_name(value: Any) -> str:
    if not isinstance(value, str) or not re.match(manifests.NAME, value):
        raise BadRequest(
            "name is the app's program name: lower-case letters, digits and _, 63 at most.", code="E_INPUTS"
        )
    return value


def read_files(given: Any) -> dict[str, bytes]:
    """The page's files as sent: relative paths to text, or {base64}; 64 files and 16 MiB in all at most."""
    if not isinstance(given, dict) or not given:
        raise BadRequest("files is an object: each file's relative path to its text, or {base64}.", code="E_INPUTS")
    if len(given) > FILES_MAX:
        raise BadRequest(f"At most {FILES_MAX} files.", code="E_INPUTS")
    out: dict[str, bytes] = {}
    total = 0
    for path, content in given.items():
        if not isinstance(path, str) or not manifests.HOME_FILE.match(path) or {".", ".."} & set(path.split("/")):
            raise BadRequest(f"{path!r} is not a relative path inside the app.", code="E_INPUTS")
        if PurePosixPath(path).name in PRIVATE or path.startswith("files/"):
            raise BadRequest(f"{path} is rrotor's own: give the page's files other names.", code="E_INPUTS")
        if isinstance(content, str):
            raw = content.encode()
        elif isinstance(content, dict) and isinstance(content.get("base64"), str):
            try:
                raw = base64.b64decode(content["base64"], validate=True)
            except (binascii.Error, ValueError):
                raise BadRequest(f"{path}: base64 is not valid.", code="E_INPUTS") from None
        else:
            raise BadRequest(f"{path}: a file is text, or {{base64}}.", code="E_INPUTS")
        total += len(raw)
        if total > BYTES_MAX:
            raise BadRequest("The files are over 16 MiB in all.", code="E_INPUTS")
        out[path] = raw
    return out


def manifest_of(data: dict[str, Any], name: str) -> dict[str, Any]:
    """The app's glyphh.json from what app_publish was sent: name, title, description, roles, entry, functions."""
    title, description = data.get("title"), data.get("description", "")
    if not isinstance(title, str) or not title.strip():
        raise BadRequest("Send title: what the app is called.", code="E_INPUTS")
    if not isinstance(description, str):
        raise BadRequest("description is text.", code="E_INPUTS")
    roles = data.get("roles") or []
    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles) or len(roles) > ROLES_MAX:
        raise BadRequest("roles is a list of role names (groups, admin, member); empty: everyone.", code="E_INPUTS")
    functions = data.get("functions") or {}
    if not isinstance(functions, dict) or not all(isinstance(v, dict) for v in functions.values()):
        raise BadRequest("functions is an object: each function's name to {description, inputs}.", code="E_INPUTS")
    manifest: dict[str, Any] = {
        "name": name,
        "kind": "app",
        "description": description.strip(),
        "app": {
            "entry": data.get("entry") or "index.html",
            "title": title.strip(),
            "roles": roles,
            "functions": list(functions),
        },
        "functions": functions,
    }
    for key in ("hosts", "secrets", "clis", "limits", "data", "databases", "schedules"):
        if key in data:
            manifest[key] = data[key]
    return manifest


def content_type(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in TYPES:
        return TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def refused_page(err: RrotorError, signed_in: bool) -> str:
    titles = {
        "E_APP_FORBIDDEN": "This app is not yours to open",
        "E_APP_MISSING": "No such app",
        "E_APP_UNREVIEWED": "Not reviewed yet",
    }
    return REFUSED.format(
        title=html.escape(titles.get(err.code, "Not served")),
        message=html.escape(err.message),
        action=SIGN_OUT if signed_in else "",
    )


class Apps:
    def __init__(self, programs: Programs, artifacts: Artifacts, address: str) -> None:
        self.programs, self.artifacts, self.address = programs, artifacts, address.rstrip("/")

    def url(self, name: str) -> str:
        return f"{self.address}/apps/{name}/"

    def _app(self, name: str) -> Version:
        """The app's active version, or AppError (missing, or a program that is not an app)."""
        try:
            found = self.programs.get(name)
        except RrotorError:
            raise AppError(f"This runtime has no app {name}.", code="E_APP_MISSING", status=404) from None
        if found.manifest.app is None:
            raise AppError(f"{name} is a program, not an app.", code="E_APP_MISSING", status=404)
        return found

    def state(self, name: str, version: int) -> str:
        return self.artifacts.state(name, version)

    def listed(self) -> list[dict[str, Any]]:
        """Every app here, by its active version: what the person sees of it, and the catalogue's word."""
        out = []
        for name in self.programs.names():
            active = self.programs.active(name)
            if active is None or active.manifest.app is None:
                continue
            app = active.manifest.app
            out.append(
                {
                    "name": name,
                    "title": app.title,
                    "description": active.manifest.description,
                    "version": active.version,
                    "active": True,
                    "roles": app.roles,
                    "functions": app.functions,
                    "state": self.state(name, active.version),
                    "url": self.url(name),
                }
            )
        return out

    def serving(self, name: str, roles: set[str] | None, asked: int | None = None) -> Version:
        """The version of the app a viewer with these roles may open (None: a local rrotor's own browser, which
        may open anything): the active, approved one; or, for a reviewer, the draft version asked for."""
        found = self._app(name)
        if roles is None:
            return found if asked is None else self.programs.get(name, asked)
        app = found.manifest.app
        assert app is not None  # noqa: S101  (checked by _app)
        reviewer = self.artifacts.is_reviewer(roles)  # reviews every app: opens any version, whatever its roles
        if asked is not None and asked != found.version:
            if not reviewer:
                raise AppError("Only a reviewer may open a version under review.", code="E_APP_FORBIDDEN", status=403)
            try:
                return self.programs.get(name, asked)
            except RrotorError:
                raise AppError(f"{name} has no version {asked} here.", code="E_APP_MISSING", status=404) from None
        if reviewer:
            return found
        if not self.artifacts.approved(name, found.version):
            raise AppError(f"{app.title} is awaiting review by your organisation.", code="E_APP_UNREVIEWED", status=409)
        if not app.allows(roles):
            raise AppError("Your role does not open this app.", code="E_APP_FORBIDDEN", status=403)
        return found

    @staticmethod
    def file(version: Version, path: str) -> tuple[bytes, str]:
        """A file of the version's src/ by its path under /apps/<name>/ (`` or `/`: the entry), its content type;
        a Markdown page inside the document shell. AppError E_APP_MISSING for a path not there, or not to be served."""
        app = version.manifest.app
        assert app is not None  # noqa: S101
        relative = path.strip("/") or app.entry
        parts = relative.split("/")
        if {".", "..", ""} & set(parts) or parts[-1] in PRIVATE or parts[0] == "files":
            raise AppError("No such file in this app.", code="E_APP_MISSING", status=404)
        target = Path(version.src, *parts)
        try:
            target.resolve().relative_to(version.src.resolve())
            data = target.read_bytes()
        except (ValueError, OSError):
            raise AppError("No such file in this app.", code="E_APP_MISSING", status=404) from None
        if relative.lower().endswith(".md"):
            page = SHELL.format(title=html.escape(app.title), source=html.escape(data.decode("utf-8", "replace")))
            return page.encode(), "text/html"
        return data, content_type(relative)

    @staticmethod
    def check_call(version: Version, function: str) -> None:
        app = version.manifest.app
        assert app is not None  # noqa: S101
        if function not in app.functions:
            raise RrotorError(f"The page may not call {function}.", code="E_APP_FUNCTION", status=403)

    @staticmethod
    def inputs_of(raw: bytes) -> dict[str, Any]:
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            raise BadRequest("The call's body is JSON: the function's inputs.") from None
        if not isinstance(data, dict):
            raise BadRequest("The call's body is a JSON object: the function's inputs.")
        return data
