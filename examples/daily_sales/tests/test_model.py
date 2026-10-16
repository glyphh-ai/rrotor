"""daily_sales through rrotor.testing: the same calls, waits and Results as in a sandbox, with no network."""

from __future__ import annotations

import importlib.util
import json
import shutil
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

from rrotor.http import Response
from rrotor.testing import FakeHttp, Harness

HERE = Path(__file__).resolve().parent.parent
RATES = "https://api.frankfurter.dev/v1/latest"


def model() -> ModuleType:
    spec = importlib.util.spec_from_file_location("daily_sales_model", HERE / "model.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    shutil.copytree(HERE / "files", tmp_path / "files")
    return tmp_path / "files"


def harness(folder: Path, http: FakeHttp | None = None) -> Harness:
    manifest = json.loads((HERE / "glyphh.json").read_text())
    approvals = {name: set(f.get("approvals", [])) for name, f in manifest["functions"].items()}
    rates = http or FakeHttp({RATES: {"amount": 1.0, "base": "USD", "rates": {"EUR": 0.9}}})
    return Harness(model(), folder, approvals=approvals, http=rates, today=lambda: date(2026, 10, 15))


def test_a_day_waits_for_approval_then_is_written(folder: Path) -> None:
    h = harness(folder)
    first = h.call("start", {})
    assert (first.status, first.step) == ("needs_approval", "write")
    assert first.prompt == "Write 2026-10-14's total, $465.74 (€419.17), to the sales ledger?"
    assert not (folder / "ledger.json").exists()
    done = h.resume({"approved": True})
    assert done.status == "done"
    assert done.data["written"] is True and done.data["orders"] == 4
    ledger = json.loads((folder / "ledger.json").read_text())
    assert ledger["2026-10-14"] == {"orders": 4, "total_usd": "465.74", "total_eur": "419.17", "usd_to_eur": "0.9"}
    assert len(h.http.calls) == 1  # the rate was checkpointed: the resumed call did not ask again
    assert h.logs[-1]["message"] == "written"
    assert h.ended


def test_declined_writes_nothing(folder: Path) -> None:
    h = harness(folder)
    h.call("start", {"day": "2026-10-13"})
    done = h.resume({"approved": False})
    assert done.status == "done" and done.data["written"] is False and done.data["total_usd"] == "352.10"
    assert not (folder / "ledger.json").exists()


def test_a_missing_export_fails_with_why(folder: Path) -> None:
    result = harness(folder).call("start", {"day": "2026-01-01"})
    assert (result.status, result.step) == ("failed", "read")
    assert "sales/2026-01-01.csv" in result.data["reason"]


def test_the_rate_service_down_is_a_failed_result(folder: Path) -> None:
    down = FakeHttp({RATES: Response(503, b"down")})
    result = harness(folder, down).call("start", {})
    assert result.status == "failed" and result.data["code"] == "E_HTTP"


def test_summary_reads_the_ledger(folder: Path) -> None:
    h = harness(folder)
    assert h.call("summary").data == {"days": {}}
    h.call("start", {})
    h.resume({"approved": True})
    assert list(harness(folder).call("summary").data["days"]) == ["2026-10-14"]
