"""What a page may do outside its cell, its keys and clicks, and its icons: the Workspaces helper's pure modules
(`popups.mjs`, `input.mjs`, `favicon.mjs`), the same rules in Python. rrotor has no desktop, so a native app's link
is never opened here: kept in panel it becomes its web client, otherwise it is ignored."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

MIN_CHROME_MAJOR, SPOOF_CHROME = 137, "140.0.0.0"
NATIVE_BINDING = "__workspacesNative"
NATIVE_LINK_SCRIPT = f"""addEventListener("click", e => {{
  const a = e.target && e.target.closest && e.target.closest("a[href]")
  if (!a || /^(https?|about|javascript|blob|data):/i.test(a.href)) return
  e.preventDefault()
  e.stopImmediatePropagation()
  {NATIVE_BINDING}(a.href)
}}, true)"""


def is_web(url: str) -> bool:
    return url.startswith(("http://", "https://", "about:"))


def origin(url: str) -> str | None:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme in ("http", "https") and parts.netloc else None


def native_to_web(native: str, parent: str) -> str | None:
    parts = urlsplit(native)
    query = parse_qs(parts.query)
    first = {k: v[0] for k, v in query.items()}
    scheme = parts.scheme.lower()
    if scheme == "slack":
        team = first.get("team", "")
        return f"https://app.slack.com/client/{team}" if team else "https://app.slack.com/client"
    if scheme == "discord":
        return "https://discord.com/app"
    if scheme == "msteams":
        return "https://teams.microsoft.com"
    if scheme in ("zoommtg", "zoomus"):
        confno = first.get("confno", "")
        return f"https://app.zoom.us/wc/{confno}/join" if confno else "https://app.zoom.us"
    if scheme == "notion":
        return "https://notion.so"
    return origin(parent)


def popup_decision(url: str, keep_in_panel: bool, parent: str) -> dict[str, str]:
    """`same` loads the address in the page itself, `swallow` and `deny` close the popup, `own` keeps it over the
    cell."""
    if keep_in_panel:
        if not url or url.startswith("about:"):
            return {"action": "swallow"}
        target = url if url.startswith(("http://", "https://")) else native_to_web(url, parent)
        return {"action": "same", "url": target} if target else {"action": "swallow"}
    return {"action": "own"} if url == "" or is_web(url) else {"action": "deny"}


def native_decision(url: str, keep_in_panel: bool, parent: str) -> dict[str, str] | None:
    if is_web(url):
        return None
    if keep_in_panel:
        web = native_to_web(url, parent)
        return {"action": "same", "url": web} if web else {"action": "ignore"}
    return {"action": "ignore"}  # no desktop here: a native app is never opened by rrotor


def clean_user_agent(ua: str) -> str:
    out = re.sub(
        r"\s+",
        " ",
        re.sub(r"\S+-client/\S+\s*", "", re.sub(r"Electron/\S+\s*", "", ua.replace("HeadlessChrome", "Chrome"))),
    ).strip()
    m = re.search(r"Chrome/(\d+)", out)
    if m and int(m.group(1)) < MIN_CHROME_MAJOR:
        out = re.sub(r"Chrome/[\d.]+", f"Chrome/{SPOOF_CHROME}", out)
    return out


def user_agent_metadata(ua: str, arm: bool) -> dict[str, Any]:
    m = re.search(r"Chrome/([\d.]+)", ua)
    full = m.group(1) if m else SPOOF_CHROME
    major = full.split(".")[0]

    def brands(v: str) -> list[dict[str, str]]:
        return [
            {"brand": "Google Chrome", "version": v},
            {"brand": "Chromium", "version": v},
            {"brand": "Not=A?Brand", "version": "24" if major == v else "24.0.0.0"},
        ]

    platform = "macOS" if "Macintosh" in ua else "Windows" if "Windows" in ua else "Linux"
    return {
        "brands": brands(major),
        "fullVersionList": brands(full),
        "fullVersion": full,
        "platform": platform,
        "platformVersion": "",
        "architecture": "arm" if arm else "x86",
        "model": "",
        "mobile": False,
        "bitness": "64",
    }


KEYS: dict[str, tuple[str, int, str | None]] = {
    "return": ("Enter", 13, "\r"),
    "enter": ("Enter", 13, "\r"),
    "tab": ("Tab", 9, "\t"),
    "backspace": ("Backspace", 8, None),
    "delete": ("Delete", 46, None),
    "up": ("ArrowUp", 38, None),
    "down": ("ArrowDown", 40, None),
    "left": ("ArrowLeft", 37, None),
    "right": ("ArrowRight", 39, None),
    "pageup": ("PageUp", 33, None),
    "pagedown": ("PageDown", 34, None),
    "home": ("Home", 36, None),
    "end": ("End", 35, None),
    "escape": ("Escape", 27, None),
}


def key_calls(cmd: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    key = str(cmd.get("key", ""))
    ctrl, shift, meta, alt = (bool(cmd.get(k)) for k in ("ctrl", "shift", "meta", "alt"))
    named = KEYS.get(key.lower())
    if not named and len(key) == 1 and not ctrl and not meta and not alt:
        return [("Input.insertText", {"text": key})]
    modifiers = (1 if alt else 0) | (2 if ctrl else 0) | (4 if meta else 0) | (8 if shift else 0)
    k, code, text = named or (key, ord(key.upper()[0]) if key else 0, None)
    base = {"key": k, "windowsVirtualKeyCode": code, "modifiers": modifiers}
    down = {"type": "keyDown" if text else "rawKeyDown", **base, **({"text": text} if text else {})}
    return [("Input.dispatchKeyEvent", down), ("Input.dispatchKeyEvent", {"type": "keyUp", **base})]


def address_of(text: str) -> str:
    return text if re.match(r"^[a-z][a-z0-9+.-]*:", text, re.I) else f"https://{text}"


def mouse_call(cmd: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    action = cmd.get("action")
    x, y = cmd.get("x"), cmd.get("y")
    return (
        "Input.dispatchMouseEvent",
        {
            "type": {"down": "mousePressed", "up": "mouseReleased", "move": "mouseMoved"}.get(
                str(action), "mouseMoved"
            ),
            "x": x if isinstance(x, int | float) else 0,
            "y": y if isinstance(y, int | float) else 0,
            "button": cmd.get("button") or "none",
            "clickCount": 0 if action == "move" else (cmd.get("clicks") or 1),
            "modifiers": cmd.get("mods") or 0,
        },
    )


def favicon_target(raw: str | None) -> str | None:
    parts = urlsplit(str(raw or ""))
    return parts.geturl() if parts.scheme in ("http", "https") and parts.netloc else None


def favicon_candidates(page: str, html: str = "") -> list[str]:
    base = favicon_target(page)
    if not base:
        return []
    found: list[tuple[str, int]] = []
    for tag in re.findall(r"<link\b[^>]*>", html, re.I):
        rel_m = re.search(r"""\brel\s*=\s*["']?([^"'>]+)""", tag, re.I)
        href_m = re.search(r"""\bhref\s*=\s*["']?([^"'\s>]+)""", tag, re.I)
        rel = rel_m.group(1).lower() if rel_m else ""
        if not href_m or not re.search(r"(^|\s)(icon|apple-touch-icon)(\s|$)", rel):
            continue
        size_m = re.search(r"""\bsizes\s*=\s*["']?(\d+)""", tag, re.I)
        size = int(size_m.group(1)) if size_m else (180 if "apple" in rel else 16)
        found.append((urljoin(base, href_m.group(1)), size))
    found.sort(key=lambda f: -f[1])
    return list(dict.fromkeys([u for u, _ in found] + [urljoin(base, "/favicon.ico")]))


def is_icon(content_type: str, size: int) -> bool:
    return content_type.lower().startswith("image/") and 0 < size <= 512 * 1024
