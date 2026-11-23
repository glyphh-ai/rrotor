"""A bot's plugins on this machine (docs/architecture.md, 5.10): Claude plugin packages the server sends with the
call (`[{name, files: {path: text}, code?: [{skill, scripts: [path]}]}]`), written into the run's folder each go for
the Agent SDK to load (`plugins=[{"type": "local", "path"}]`), and the scripts of them the bot may run.

A script is a file of a plugin that the plugin's `code` names, by its path in the plugin
(`skills/<skill>/scripts/<file>`). Nothing else is ever run: `declared` is the whole list."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from rrotor import ProgramError

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MANIFEST = ".claude-plugin/plugin.json"


class PluginRefused(ProgramError):
    code = "E_PLUGIN"


def _inside(path: Any) -> PurePosixPath:
    """A file's path in its plugin: relative, no way up or out."""
    way = PurePosixPath(str(path))
    if not way.parts or way.is_absolute() or any(p in ("..", ".") or "\\" in p or "\0" in p for p in way.parts):
        raise PluginRefused(f"A plugin's file is named by its path inside the plugin, not {path!r}.")
    return way


def write(root: Path, plugins: list[dict[str, Any]]) -> list[Path]:
    """Each plugin's files written under `root/<name>/` (what was there before is dropped): the folders, in order."""
    shutil.rmtree(root, ignore_errors=True)
    made = []
    for plugin in plugins:
        name = str(plugin.get("name") or "")
        if not NAME.match(name):
            raise PluginRefused(f"A plugin's name is letters, digits, dots, dashes and underscores, not {name!r}.")
        files = {MANIFEST: json.dumps({"name": name}), **(plugin.get("files") or {})}
        for path, text in files.items():
            target = root / name / _inside(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text if isinstance(text, str) else json.dumps(text))
        made.append(root / name)
    return made


def declared(root: Path, plugins: list[dict[str, Any]]) -> dict[tuple[str, str], Path]:
    """The scripts the bot may run, `{(plugin, script): its file}`: those a plugin's `code` names that are files of
    that plugin."""
    found: dict[tuple[str, str], Path] = {}
    for plugin in plugins:
        name, files = str(plugin.get("name") or ""), plugin.get("files") or {}
        for entry in plugin.get("code") or []:
            for script in entry.get("scripts") or []:
                if script in files:
                    found[(name, str(script))] = root / name / _inside(script)
    return found
