"""
recalc_check.py — Prove the generated workbook actually recalculates, with zero errors.

openpyxl writes formulas but never evaluates them, so a workbook can look right and
still be full of #REF!/#DIV/0!/#NAME?. This script recalculates a workbook with a
real spreadsheet engine and fails loudly if anything is broken.

Two independent engines are supported:

  * LibreOffice headless (authoritative — the same engine class as Excel). The file
    is converted to a new .xlsx, which forces a full recalculation of every formula,
    and the cached results are then read back.
  * `formulas` (pure Python fallback) — used when LibreOffice is unavailable.

Exit status is non-zero if any cell evaluates to an Excel error, so this can gate a
build. Optionally verifies key cells against expected values with --expect.

Usage
-----
    python recalc_check.py models/AAPL_DCF_Model.xlsx
    python recalc_check.py models/AAPL_DCF_Model.xlsx --engine formulas
    python recalc_check.py models/AAPL_DCF_Model.xlsx --show "DCF!B22" "Assumptions!B28"
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

ERROR_TOKENS = ("#REF!", "#DIV/0!", "#VALUE!", "#NAME?", "#NUM!", "#N/A", "#NULL!")

SOFFICE_CANDIDATES = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
]


def find_soffice() -> Optional[str]:
    for p in SOFFICE_CANDIDATES:
        if os.path.exists(p):
            return p
    return shutil.which("soffice") or shutil.which("libreoffice")


# --------------------------------------------------------------------------- #
# Engine: LibreOffice headless
# --------------------------------------------------------------------------- #

def recalc_libreoffice(path: str, timeout: int = 180) -> Tuple[Dict[str, object], str]:
    """
    Recalculate via LibreOffice headless. Returns ({'Sheet!A1': value}, engine_name).

    Converting to xlsx forces LibreOffice to evaluate every formula (openpyxl files
    carry no cached results), and it writes the computed values into the output file.
    """
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError("LibreOffice not found")

    outdir = tempfile.mkdtemp(prefix="recalc_")
    try:
        # Work on a copy inside the temp dir: LibreOffice can hold a lock on its
        # input for a moment after exiting, and we must never risk the original.
        work = os.path.join(outdir, "in", os.path.basename(path))
        os.makedirs(os.path.dirname(work), exist_ok=True)
        shutil.copy2(path, work)

        produced: List[str] = []
        last: Optional[str] = None
        for attempt in range(3):
            # A fresh profile per attempt avoids clashing with any other instance.
            profile = os.path.join(outdir, f"profile{attempt}")
            dest = os.path.join(outdir, f"out{attempt}")
            os.makedirs(dest, exist_ok=True)
            cmd = [
                soffice, "--headless", "--norestore", "--invisible", "--nolockcheck",
                f"-env:UserInstallation=file:///{profile.replace(os.sep, '/')}",
                "--convert-to", "xlsx:Calc MS Excel 2007 XML",
                "--outdir", dest, work,
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True,
                                      timeout=timeout)
                last = f"rc={proc.returncode} {proc.stdout.strip()} {proc.stderr.strip()}"
            except subprocess.TimeoutExpired:
                last = "timed out"
            produced = glob.glob(os.path.join(dest, "*.xlsx"))
            if produced:
                break
        if not produced:
            raise RuntimeError(f"LibreOffice produced no output ({last})")

        import openpyxl
        wb = openpyxl.load_workbook(produced[0], data_only=True)
        values: Dict[str, object] = {}
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for cell in row:
                    if cell.value is not None:
                        values[f"{ws.title}!{cell.coordinate}"] = cell.value
        wb.close()
        return values, "LibreOffice headless"
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Engine: formulas (pure Python)
# --------------------------------------------------------------------------- #

def recalc_formulas(path: str) -> Tuple[Dict[str, object], str]:
    import logging
    import warnings
    logging.disable(logging.WARNING)
    warnings.filterwarnings("ignore")
    import formulas

    xl = formulas.ExcelModel().loads(path).finish()
    sol = xl.calculate()
    values: Dict[str, object] = {}
    for key, node in sol.items():
        # keys look like "'[FILE.XLSX]SHEET'!A1"
        if "]" not in key or "!" not in key:
            continue
        sheet_cell = key.split("]", 1)[1]
        sheet, _, cell = sheet_cell.partition("!")
        sheet = sheet.strip("'")
        try:
            val = node.value[0, 0]
        except Exception:  # noqa: BLE001 — ranges / non-scalar nodes
            val = getattr(node, "value", node)
        values[f"{sheet}!{cell}"] = val
    return values, "formulas (pure Python)"


# --------------------------------------------------------------------------- #
# Checking
# --------------------------------------------------------------------------- #

def scan_errors(values: Dict[str, object]) -> List[Tuple[str, str]]:
    errs = []
    for ref, val in values.items():
        s = str(val)
        if any(tok in s for tok in ERROR_TOKENS):
            errs.append((ref, s))
    return errs


def _norm(ref: str) -> str:
    sheet, _, cell = ref.partition("!")
    return f"{sheet.strip().strip(chr(39)).upper()}!{cell.strip().upper().replace('$','')}"


def lookup(values: Dict[str, object], ref: str):
    target = _norm(ref)
    for k, v in values.items():
        if _norm(k) == target:
            return v
    return None


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Recalculate a workbook and check for errors.")
    p.add_argument("path")
    p.add_argument("--engine", choices=["auto", "libreoffice", "formulas"], default="auto")
    p.add_argument("--show", nargs="*", default=[],
                   help="Cell refs to print, e.g. 'DCF!B22'")
    p.add_argument("--expect", nargs="*", default=[],
                   help="Checks like 'DCF!B22=111.25' (2dp tolerance by default)")
    p.add_argument("--tol", type=float, default=0.02)
    args = p.parse_args(argv)

    if not os.path.exists(args.path):
        print(f"ERROR: {args.path} not found", file=sys.stderr)
        return 2

    engines = []
    if args.engine in ("auto", "libreoffice"):
        engines.append(recalc_libreoffice)
    if args.engine in ("auto", "formulas"):
        engines.append(recalc_formulas)

    values, engine_name, last_err = None, "", None
    for fn in engines:
        try:
            values, engine_name = fn(args.path)
            break
        except Exception as exc:  # noqa: BLE001 — try the next engine
            last_err = exc
            print(f"  ({fn.__name__} unavailable: {exc})", file=sys.stderr)
    if values is None:
        print(f"ERROR: no recalculation engine worked. Last error: {last_err}",
              file=sys.stderr)
        return 2

    print(f"Recalculated {args.path}")
    print(f"  engine        : {engine_name}")
    print(f"  cells with a value: {len(values)}")

    errors = scan_errors(values)
    print(f"  formula errors: {len(errors)}")
    for ref, val in errors[:25]:
        print(f"      {ref} -> {val}")

    for ref in args.show:
        print(f"  {ref} = {lookup(values, ref)}")

    failed = list(errors)
    for spec in args.expect:
        ref, _, want = spec.partition("=")
        got = lookup(values, ref)
        try:
            ok = got is not None and abs(float(got) - float(want)) <= args.tol
        except (TypeError, ValueError):
            ok = str(got) == want
        status = "OK " if ok else "FAIL"
        print(f"  [{status}] {ref}: expected {want}, got {got}")
        if not ok:
            failed.append((ref, f"expected {want}, got {got}"))

    if failed:
        print("\nRESULT: FAILED")
        return 1
    print("\nRESULT: PASS — zero formula errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
