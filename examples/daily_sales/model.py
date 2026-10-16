"""daily_sales, the base edition (docs/architecture.md, section 2): a day's sales export from the model's own
folder, added up, converted to euros with a public exchange-rate API (its one declared host), and written to its
ledger once the person approves the `write` step."""

from __future__ import annotations

import csv
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from rrotor import Ctx, Result

RATES = "https://api.frankfurter.dev/v1/latest"


def _ledger(ctx: Ctx) -> Path:
    return ctx.files / "ledger.json"


def _read_ledger(ctx: Ctx) -> dict[str, Any]:
    path = _ledger(ctx)
    return json.loads(path.read_text()) if path.is_file() else {}


def _write_ledger(ctx: Ctx, ledger: dict[str, Any]) -> None:
    path = _ledger(ctx)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True))
    os.replace(tmp, path)


def _rows(export: Path) -> list[dict[str, str]]:
    with export.open(newline="") as f:
        return [{"order": r["order"], "amount": r["amount"]} for r in csv.DictReader(f)]


def _usd_to_eur(ctx: Ctx) -> str:
    answer = ctx.http.get(RATES, params={"from": "USD", "to": "EUR"}).raise_for_status().json()
    return str(answer["rates"]["EUR"])


def start(ctx: Ctx, inputs: dict[str, Any]) -> Result:
    day = inputs.get("day") or ctx.yesterday()
    export = ctx.files / "sales" / f"{day}.csv"
    if not export.is_file():
        return Result.failed("read", f"There is no sales export for {day} (sales/{day}.csv in the model's folder).")
    rows = _rows(export)
    total = sum((Decimal(r["amount"]) for r in rows), Decimal("0"))
    rate = Decimal(ctx.checkpoint(f"rate:{day}", lambda: _usd_to_eur(ctx)))
    euros = (total * rate).quantize(Decimal("0.01"))
    summary = {"day": day, "orders": len(rows), "total_usd": str(total), "total_eur": str(euros), "rows": rows}
    if not ctx.ask_approval("write", f"Write {day}'s total, ${total} (€{euros}), to the sales ledger?"):
        ctx.log("not written: declined", day=day)
        return Result.done({**summary, "written": False})
    ledger = _read_ledger(ctx)
    ledger[day] = {"orders": len(rows), "total_usd": str(total), "total_eur": str(euros), "usd_to_eur": str(rate)}
    _write_ledger(ctx, ledger)
    ctx.log("written", day=day, total_usd=str(total))
    return Result.done({**summary, "written": True})


def summary(ctx: Ctx, inputs: dict[str, Any]) -> Result:
    return Result.done({"days": _read_ledger(ctx)})
