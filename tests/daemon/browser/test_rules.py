from __future__ import annotations

import pytest

from rrotord.browser import rules


def test_is_web_and_origin():
    assert rules.is_web("about:blank") and not rules.is_web("slack://x")
    assert rules.origin("https://a.example.com/x?y") == "https://a.example.com"
    assert rules.origin("file:///x") is None


@pytest.mark.parametrize(
    ("native", "web"),
    [
        ("slack://open?team=T1", "https://app.slack.com/client/T1"),
        ("slack://open", "https://app.slack.com/client"),
        ("discord://x", "https://discord.com/app"),
        ("msteams://x", "https://teams.microsoft.com"),
        ("zoommtg://j?confno=123", "https://app.zoom.us/wc/123/join"),
        ("zoomus://j", "https://app.zoom.us"),
        ("notion://x", "https://notion.so"),
        ("other://x", "https://parent.example.com"),
    ],
)
def test_native_to_web(native, web):
    assert rules.native_to_web(native, "https://parent.example.com/page") == web


def test_popup_decisions_kept_in_panel():
    parent = "https://p.example.com/"
    assert rules.popup_decision("", True, parent) == {"action": "swallow"}
    assert rules.popup_decision("about:blank", True, parent) == {"action": "swallow"}
    assert rules.popup_decision("https://x.example.com/", True, parent) == {
        "action": "same",
        "url": "https://x.example.com/",
    }
    assert rules.popup_decision("discord://x", True, parent) == {"action": "same", "url": "https://discord.com/app"}
    assert rules.popup_decision("weird://x", True, "about:blank") == {"action": "swallow"}


def test_popup_decisions_not_kept():
    assert rules.popup_decision("", False, "") == {"action": "own"}
    assert rules.popup_decision("https://x/", False, "") == {"action": "own"}
    assert rules.popup_decision("slack://x", False, "") == {"action": "deny"}


def test_native_decisions():
    assert rules.native_decision("https://x/", True, "") is None
    assert rules.native_decision("notion://x", True, "") == {"action": "same", "url": "https://notion.so"}
    assert rules.native_decision("weird://x", True, "about:blank") == {"action": "ignore"}
    assert rules.native_decision("notion://x", False, "") == {"action": "ignore"}


def test_clean_user_agent():
    ua = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 HeadlessChrome/120.0.1 Electron/30 foo-client/1 "
        "Safari/537.36"
    )
    assert (
        rules.clean_user_agent(ua)
        == "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36"
    )
    assert rules.clean_user_agent("Chrome/141.0.1 X") == "Chrome/141.0.1 X"
    assert rules.clean_user_agent("NoChrome") == "NoChrome"


def test_user_agent_metadata():
    meta = rules.user_agent_metadata("(Macintosh) Chrome/141.0.2.3", True)
    assert (meta["platform"], meta["architecture"], meta["fullVersion"]) == ("macOS", "arm", "141.0.2.3")
    assert meta["brands"][2] == {"brand": "Not=A?Brand", "version": "24"}
    assert meta["fullVersionList"][2]["version"] == "24.0.0.0"
    assert rules.user_agent_metadata("(Windows)", False)["platform"] == "Windows"
    other = rules.user_agent_metadata("(X11)", False)
    assert (other["platform"], other["architecture"], other["fullVersion"]) == ("Linux", "x86", "140.0.0.0")


def test_key_calls():
    assert rules.key_calls({"key": "a"}) == [("Input.insertText", {"text": "a"})]
    enter = rules.key_calls({"key": "Return", "shift": True})
    assert enter[0] == (
        "Input.dispatchKeyEvent",
        {"type": "keyDown", "key": "Enter", "windowsVirtualKeyCode": 13, "modifiers": 8, "text": "\r"},
    )
    assert enter[1][1]["type"] == "keyUp"
    copy = rules.key_calls({"key": "c", "meta": True, "ctrl": True, "alt": True})
    assert copy[0][1] == {"type": "rawKeyDown", "key": "c", "windowsVirtualKeyCode": 67, "modifiers": 7}
    assert rules.key_calls({"key": "Backspace"})[0][1]["type"] == "rawKeyDown"
    assert rules.key_calls({})[0][1]["windowsVirtualKeyCode"] == 0


def test_address_of():
    assert rules.address_of("example.com") == "https://example.com"
    assert rules.address_of("http://x") == "http://x"


def test_mouse_call():
    assert rules.mouse_call({"action": "down", "x": 1, "y": 2.5, "button": "left", "clicks": 2, "mods": 1}) == (
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": 1, "y": 2.5, "button": "left", "clickCount": 2, "modifiers": 1},
    )
    moved = rules.mouse_call({"action": "move", "x": "bad"})[1]
    assert (moved["type"], moved["x"], moved["y"], moved["button"], moved["clickCount"]) == (
        "mouseMoved",
        0,
        0,
        "none",
        0,
    )
    assert rules.mouse_call({"action": "up"})[1]["clickCount"] == 1
    assert rules.mouse_call({"action": "spin"})[1]["type"] == "mouseMoved"


def test_favicon_target():
    assert rules.favicon_target("https://a.example.com/x") == "https://a.example.com/x"
    assert rules.favicon_target(None) is None and rules.favicon_target("javascript:x") is None


def test_favicon_candidates():
    html = """
    <link rel="icon" href="/small.png">
    <link rel="apple-touch-icon" href="apple.png">
    <link rel="icon" sizes="64x64" href='/big.png'>
    <link rel="stylesheet" href="/s.css">
    <link rel="icon">
    <link href="/norel.png">
    """
    assert rules.favicon_candidates("https://a.example.com/p/", html) == [
        "https://a.example.com/p/apple.png",
        "https://a.example.com/big.png",
        "https://a.example.com/small.png",
        "https://a.example.com/favicon.ico",
    ]
    assert rules.favicon_candidates("nope") == []


def test_is_icon():
    assert rules.is_icon("Image/png", 10)
    assert not rules.is_icon("text/html", 10) and not rules.is_icon("image/png", 0)
    assert not rules.is_icon("image/png", 512 * 1024 + 1)
