"""Apps: what Claude made, published to this rrotor and served to the org's people (docs/architecture.md, 5.8; the
server's docs/specs/apps.md, 4). Publishing takes the app's files and makes a version of `"kind": "app"`
(rrotord/deploy.py, `from_files`); serving answers the active version's `src/` to a browser whose sign-in the
gate admitted (rrotord/browser.py), when the catalogue says the version is approved (rrotord/catalogue.py) and the
viewer's role or groups the app's `roles` allow; the page's calls run the app's functions as the viewer.

The entry (the file opened at `/apps/<name>/`) is served as it is when it is a page (`.html`, `.htm`), an SVG or a
PDF, and inside one of rrotor's shells otherwise: the document shell renders Markdown with `marked`, the diagram
shell renders Mermaid with `mermaid`, the image shell shows an image on a page with the title, and the React shell
runs a `.jsx` or `.tsx` file as claude.ai's React artifacts run, building nothing (rrotord/manifest.py, ENTRIES).

The React shell: an import map for the packages such a file imports, Tailwind's CDN, and `@babel/standalone`
transforming the file in the browser. The file's source is inline in one `<script type="text/babel"
data-type="module">` (Babel transforms the script and runs it as an ES module), with a trailer that mounts its
default export into `#root`. Inline rather than `src="./<entry>"`: Babel standalone transforms each `text/babel`
script on its own and leaves imports to the browser, so a second script could not `import` the entry (the browser
would fetch the raw file; verified against transformScriptTags.ts, which has no module registry). A module inline
in a page exports to nobody, so `export default` is rewritten to a binding (`mounted`, a few line-anchored
replacements: a named function or class keeps its name and is bound at the end; any other default becomes
`const __App =`; other `export` keywords are dropped) and the trailer renders `__App`. Two presets are registered
before Babel runs: `jsx`, React's with the automatic runtime (a file need not import React), and `tsx`,
TypeScript's for any filename with JSX (an inline script has no `.tsx` name for the preset to go by). The packages
that import React come from esm.sh with `react` and `react-dom` external, so the import map resolves them to one
React; jsDelivr's `+esm` bundles pin their own exact React version (recharts and react-dom were seen pinning two
different ones), which would give the page two Reacts and break hooks. The libraries that do not import React
come from jsDelivr as the spec says. The pins are one table, REACT_PACKAGES."""

from __future__ import annotations

import base64
import binascii
import html
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from rrotord import manifest as manifests
from rrotord.catalogue import Catalogue
from rrotord.errors import BadRequest, RrotorError
from rrotord.programs import Programs, Version

FILES_MAX, BYTES_MAX = 64, 16 * 1024 * 1024
ROLES_MAX = 50
# The CDN hosts Claude Code's artifacts use, and esm.sh for the React shell's packages (the module docstring).
CDN = (
    "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net https://cdn.tailwindcss.com https://code.jquery.com "
    "https://unpkg.com https://esm.sh"
)
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
# Content-Type by extension for a file fetched by its path; anything else is application/octet-stream. A `.md`
# is always shelled (a document may link to another); a `.jsx` or `.tsx` fetched by its path is text, since the
# shell serves the entry.
TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".mmd": "text/plain",
    ".mermaid": "text/plain",
    ".jsx": "text/plain",
    ".tsx": "text/plain",
    ".wasm": "application/wasm",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
DEFAULT_TYPE = "application/octet-stream"
INLINE = frozenset({"application/pdf", "image/svg+xml"})  # served as they are, Content-Disposition: inline
SHELLS = {
    ".md": "doc",
    ".mmd": "diagram",
    ".mermaid": "diagram",
    ".jsx": "react",
    ".tsx": "react",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".gif": "image",
}
HEAD = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>'
)
# A Markdown document inside rrotor's document shell: rendered by marked from a CDN the CSP allows; its source
# shown as text when the library does not load.
DOC_SHELL = (
    HEAD + "<style>body{{max-width:72ch;margin:2rem auto;padding:0 1rem;font-family:system-ui,sans-serif;"
    "line-height:1.5;color:#1a1a1a;background:#fff}}pre{{overflow:auto;background:#f4f4f4;padding:1rem;"
    "border-radius:6px}}code{{font-family:ui-monospace,monospace}}</style>"
    '<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/15.0.12/marked.min.js"></script></head>'
    '<body><main id="page"></main><textarea id="source" hidden>{source}</textarea>'
    '<script>const s=document.getElementById("source").value,p=document.getElementById("page");'
    'if(window.marked){{p.innerHTML=window.marked.parse(s)}}else{{const e=document.createElement("pre");'
    "e.textContent=s;p.append(e)}}</script></body></html>"
)
# A Mermaid diagram inside rrotor's diagram shell: mermaid from jsDelivr renders what is in <pre class="mermaid">;
# when the library does not load, the source stays there, shown as text.
DIAGRAM_SHELL = (
    HEAD + "<style>body{{max-width:80rem;margin:2rem auto;padding:0 1rem;font-family:system-ui,sans-serif;"
    "color:#1a1a1a;background:#fff}}h1{{font-size:1.25rem;font-weight:600}}pre.mermaid{{white-space:pre-wrap;"
    "font-family:ui-monospace,monospace}}</style>"
    '<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script></head>'
    '<body><h1>{title}</h1><pre class="mermaid">{source}</pre>'
    '<script>if(window.mermaid){{mermaid.initialize({{startOnLoad:true,securityLevel:"strict"}})}}</script>'
    "</body></html>"
)
# An image inside rrotor's image shell: centred on a page, at its size (no larger than the page), with the title.
IMAGE_SHELL = (
    HEAD + "<style>body{{margin:0;min-height:100vh;display:flex;flex-direction:column;align-items:center;"
    "justify-content:center;background:#fff;font-family:system-ui,sans-serif;color:#1a1a1a}}"
    "h1{{font-size:1rem;font-weight:500;margin:1rem}}img{{max-width:100%;height:auto}}</style></head>"
    '<body><h1>{title}</h1><img src="./{src}" alt="{title}"></body></html>'
)
# The React shell's packages: what a claude.ai React artifact imports, each at a pinned major (`three` at a pinned
# minor: its major is 0). esm.sh builds those that import React with react and react-dom external (the module
# docstring); jsDelivr serves the rest as ES modules.
ESM, JSDELIVR = "https://esm.sh/", "https://cdn.jsdelivr.net/npm/"
REACT_PACKAGES: dict[str, str] = {
    "react": f"{ESM}react@19",
    "react/jsx-runtime": f"{ESM}react@19/jsx-runtime",
    "react/jsx-dev-runtime": f"{ESM}react@19/jsx-dev-runtime",
    "react-dom": f"{ESM}react-dom@19?external=react",
    "react-dom/client": f"{ESM}react-dom@19/client?external=react",
    "recharts": f"{ESM}recharts@3?external=react,react-dom",
    "lucide-react": f"{ESM}lucide-react@1?external=react",
    "framer-motion": f"{ESM}framer-motion@14?external=react,react-dom",
    "d3": f"{JSDELIVR}d3@7/+esm",
    "three": f"{JSDELIVR}three@0.187/+esm",
    "date-fns": f"{JSDELIVR}date-fns@4/+esm",
    "lodash": f"{JSDELIVR}lodash@4/+esm",
    "papaparse": f"{JSDELIVR}papaparse@5/+esm",
    "mathjs": f"{JSDELIVR}mathjs@15/+esm",
    **{
        f"@radix-ui/{name}": f"{ESM}@radix-ui/{name}@{major}?external=react,react-dom"
        for name, major in (
            ("react-dialog", 1),
            ("react-dropdown-menu", 2),
            ("react-popover", 1),
            ("react-select", 2),
            ("react-slider", 1),
            ("react-switch", 1),
            ("react-tabs", 1),
            ("react-tooltip", 1),
            ("react-toggle", 1),
            ("react-checkbox", 1),
            ("react-radio-group", 1),
            ("react-progress", 1),
            ("react-accordion", 1),
            ("react-scroll-area", 1),
            ("react-separator", 1),
            ("react-label", 2),
            ("react-icons", 1),
            ("react-slot", 1),
        )
    },
}
IMPORT_MAP = json.dumps({"imports": REACT_PACKAGES}, separators=(",", ":"))
BABEL = "https://cdnjs.cloudflare.com/ajax/libs/babel-standalone/7.29.10/babel.min.js"
REACT_SHELL = (
    HEAD + '<script type="importmap">{imports}</script>'
    '<script src="https://cdn.tailwindcss.com"></script>'
    f'<script src="{BABEL}"></script>'
    "<script>if(window.Babel){{"
    'Babel.registerPreset("jsx",{{presets:[[Babel.availablePresets.react,{{runtime:"automatic"}}]]}});'
    'Babel.registerPreset("tsx",{{presets:[[Babel.availablePresets.typescript,{{isTSX:true,allExtensions:true}}]]}})'
    "}}</script>"
    "<style>body{{margin:0;font-family:system-ui,sans-serif;color:#1a1a1a;background:#fff}}</style></head>"
    '<body><div id="root"></div>'
    '<script type="text/babel" data-type="module" data-presets="{presets}">\n{body}\n</script>'
    "</body></html>"
)
TRAILER = (
    '\nimport {createElement as __h} from "react";\nimport {createRoot as __root} from "react-dom/client";\n'
    '__root(document.getElementById("root")).render(__h(__App));\n'
)
NAME = r"[A-Za-z_$][\w$]*"
DEFAULT_DECL = re.compile(rf"^export\s+default\s+(?P<decl>(?:async\s+)?function\s*\*?\s*(?P<name>{NAME})\b)", re.M)
DEFAULT_CLASS = re.compile(rf"^export\s+default\s+(?P<decl>class\s+(?P<name>{NAME})\b)", re.M)
DEFAULT_AS = re.compile(rf"^export\s*\{{\s*(?P<name>{NAME})\s+as\s+default\s*,?\s*\}}\s*;?", re.M)
DEFAULT_EXPR = re.compile(r"^export\s+default\s+", re.M)
EXPORT_DECL = re.compile(r"^export\s+(?=(?:const|let|var|function|async|class|type|interface|enum|declare)\b)", re.M)
EXPORT_LIST = re.compile(r"^export\s*\{[^}]*\}\s*(?:from\s*['\"][^'\"]*['\"])?\s*;?[ \t]*$", re.M)
SCRIPT_END = re.compile(r"</(?=script)", re.I)
REFUSED = (
    HEAD + "<style>body{{max-width:40rem;margin:4rem auto;padding:0 1rem;font-family:system-ui,sans-serif;"
    "color:#1a1a1a}}</style></head><body><h1>{title}</h1><p>{message}</p>{action}</body></html>"
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
    """The app's files as sent: relative paths to text, or {base64}; 64 files and 16 MiB in all at most."""
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
            raise BadRequest(f"{path} is rrotor's own: give the app's files other names.", code="E_INPUTS")
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


def manifest_of(data: dict[str, Any], name: str, files: dict[str, bytes]) -> dict[str, Any]:
    """The app's glyphh.json from what app_publish was sent: name, title, description, roles, entry (default
    index.html, or the one file sent), type (default the entry's), functions."""
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
    entry = data.get("entry") or (next(iter(files)) if len(files) == 1 else "index.html")
    if not isinstance(entry, str) or manifests.entry_type(entry) is None:
        raise BadRequest(manifests.ENTRY_REFUSED, code="E_INPUTS")
    kind = data.get("type") or manifests.entry_type(entry)
    if kind not in manifests.ENTRIES.values() and kind not in ("dashboard", "deck"):
        raise BadRequest("type is one of page, dashboard, doc, diagram, deck, design.", code="E_INPUTS")
    manifest: dict[str, Any] = {
        "name": name,
        "kind": "app",
        "description": description.strip(),
        "app": {
            "entry": entry,
            "type": kind,
            "title": title.strip(),
            "roles": roles,
            "functions": list(functions),
        },
        "functions": functions,
    }
    for key in ("hosts", "secrets", "clis", "limits", "data", "databases", "schedules", "graph"):
        if key in data:
            manifest[key] = data[key]
    return manifest


def content_type(path: str) -> str:
    return TYPES.get(PurePosixPath(path).suffix.lower(), DEFAULT_TYPE)


def script_text(source: str) -> str:
    """Source placed inside a <script>: `</script` (which would end the element) and `<!--` (which opens the
    tokeniser's escaped state) are broken with a backslash, which JavaScript reads through."""
    return SCRIPT_END.sub(r"<\\/", source).replace("<!--", r"<\!--")


def mounted(source: str) -> str:
    """A React file's source as the shell's module script: its default export bound to `__App` (the module
    docstring), its other exports plain declarations, and the trailer that mounts `__App`."""
    found = DEFAULT_DECL.search(source) or DEFAULT_CLASS.search(source)
    tail = ""
    if found is not None:
        out = source[: found.start()] + found.group("decl") + source[found.end() :]
        tail = f"\nconst __App = {found.group('name')};"
    else:
        out, n = DEFAULT_EXPR.subn("const __App = ", source, count=1)
        if not n:
            out = DEFAULT_AS.sub(lambda m: f"const __App = {m.group('name')};", source, count=1)
    out = EXPORT_LIST.sub("", EXPORT_DECL.sub("", out))
    return out + tail + TRAILER


def shell(app: manifests.App, relative: str, data: bytes) -> tuple[bytes, str] | None:
    """The file inside the shell its kind has (None: served as it is), as a page."""
    suffix = PurePosixPath(relative).suffix.lower()
    kind = SHELLS.get(suffix)
    if kind is None:
        return None
    title = html.escape(app.title)
    if kind == "image":
        page = IMAGE_SHELL.format(title=title, src=html.escape(quote(relative)))
    else:
        text = data.decode("utf-8", "replace")
        if kind == "doc":
            page = DOC_SHELL.format(title=title, source=html.escape(text))
        elif kind == "diagram":
            page = DIAGRAM_SHELL.format(title=title, source=html.escape(text))
        else:
            presets = "jsx,tsx" if suffix == ".tsx" else "jsx"
            page = REACT_SHELL.format(title=title, imports=IMPORT_MAP, presets=presets, body=script_text(mounted(text)))
    return page.encode(), "text/html"


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
    def __init__(self, programs: Programs, catalogue: Catalogue, address: str) -> None:
        self.programs, self.catalogue, self.address = programs, catalogue, address.rstrip("/")

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
        return self.catalogue.state(name, version)

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
                    "type": app.type,
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
        reviewer = self.catalogue.is_reviewer(roles)  # reviews every app: opens any version, whatever its roles
        if asked is not None and asked != found.version:
            if not reviewer:
                raise AppError("Only a reviewer may open a version under review.", code="E_APP_FORBIDDEN", status=403)
            try:
                return self.programs.get(name, asked)
            except RrotorError:
                raise AppError(f"{name} has no version {asked} here.", code="E_APP_MISSING", status=404) from None
        if reviewer:
            return found
        if not self.catalogue.approved(name, found.version):
            raise AppError(f"{app.title} is awaiting review by your organisation.", code="E_APP_UNREVIEWED", status=409)
        if not app.allows(roles):
            raise AppError("Your role does not open this app.", code="E_APP_FORBIDDEN", status=403)
        return found

    @staticmethod
    def file(version: Version, path: str) -> tuple[bytes, str]:
        """A file of the version's src/ by its path under /apps/<name>/ (`` or `/`: the entry, inside its shell
        when it has one; a `.md` is shelled by its path too), its content type. AppError E_APP_MISSING for a path
        not there, or not to be served."""
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
        if not path.strip("/") or relative.lower().endswith(".md"):
            shelled = shell(app, relative, data)
            if shelled is not None:
                return shelled
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
