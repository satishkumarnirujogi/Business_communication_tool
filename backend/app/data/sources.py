"""Spreadsheet sources: an Excel file or a Google Sheet. They only move cell values;
parsing and merging live in tabular.py and synced.py."""

from __future__ import annotations

import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

Grid = list[list[Any]]


class SourceError(RuntimeError):
    """Readable problem with the spreadsheet (missing file, no access, file locked...)."""


class TabularSource(ABC):
    label: str = "spreadsheet"

    @abstractmethod
    def read(self, tabs: list[str]) -> dict[str, Grid]:
        """Header + rows per tab. A missing tab returns []."""

    @abstractmethod
    def write(self, tabs: dict[str, Grid]) -> None:
        """Replace the full contents of each given tab (creating it if needed)."""


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------
class ExcelSource(TabularSource):
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.label = f"Excel file {self.path.name}"

    def read(self, tabs: list[str]) -> dict[str, Grid]:
        import openpyxl

        if not self.path.is_file():
            raise SourceError(
                f"Excel file not found at {self.path}. Copy templates/clinic_template.xlsx there, "
                "or set EXCEL_PATH in .env."
            )
        try:
            wb = openpyxl.load_workbook(self.path, data_only=True)  # formulas -> their values
        except Exception as e:
            raise SourceError(f"Could not open {self.path.name}: {e}. Is it a valid .xlsx file?") from None
        try:
            return {t: [list(r) for r in wb[t].iter_rows(values_only=True)] if t in wb.sheetnames else [] for t in tabs}
        finally:
            wb.close()

    def write(self, tabs: dict[str, Grid]) -> None:
        import openpyxl
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter

        if self.path.is_file():
            wb = openpyxl.load_workbook(self.path)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            wb = openpyxl.Workbook()
            wb.remove(wb.active)
        for tab, grid in tabs.items():
            index = None
            if tab in wb.sheetnames:
                index = wb.sheetnames.index(tab)
                wb.remove(wb[tab])
            ws = wb.create_sheet(tab, index)
            for row in grid:
                ws.append(row)
            if grid and tab != "README":
                for cell in ws[1]:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill("solid", fgColor="0F9D94")
                ws.freeze_panes = "A2"
            for i, _ in enumerate(grid[0] if grid else [], start=1):
                width = max((len(str(r[i - 1])) for r in grid if len(r) >= i and r[i - 1] is not None), default=8)
                ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 8), 60)
        fd, tmp = tempfile.mkstemp(suffix=".xlsx", dir=self.path.parent)
        os.close(fd)
        try:
            wb.save(tmp)
            os.replace(tmp, self.path)
        except PermissionError:
            raise SourceError(
                f"Can't save {self.path.name}: it is open in another program (probably Excel). "
                "Close it; the assistant will retry on the next sync."
            ) from None
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


# --------------------------------------------------------------------------
# Google Sheets
# --------------------------------------------------------------------------
class GoogleSheetsSource(TabularSource):
    """Uses a Google service account. Share the sheet with the service account's email (Editor)."""

    def __init__(self, sheet_id: str, credentials_file: Path | str | None, book: Any = None):
        self.sheet_id = sheet_id
        self.credentials_file = Path(credentials_file) if credentials_file else None
        self._book = book  # injected in tests
        self.label = "Google Sheet"

    def book(self):
        if self._book is not None:
            return self._book
        import gspread

        if not self.sheet_id:
            raise SourceError("GOOGLE_SHEETS_ID is empty. Copy the id from the sheet's URL into .env.")
        if not self.credentials_file or not self.credentials_file.is_file():
            raise SourceError(
                f"Google service account key not found at {self.credentials_file}. "
                "Download the JSON key and set GOOGLE_SERVICE_ACCOUNT_FILE in .env (see docs/google-sheets.md)."
            )
        try:
            client = gspread.service_account(filename=str(self.credentials_file))
            self._book = client.open_by_key(self.sheet_id)
        except gspread.exceptions.SpreadsheetNotFound:
            email = _service_account_email(self.credentials_file)
            raise SourceError(
                f"Can't open the Google Sheet. Share it with {email or 'the service account email'} as Editor, "
                "and check GOOGLE_SHEETS_ID."
            ) from None
        except gspread.exceptions.APIError as e:
            raise SourceError(f"Google Sheets refused the request: {_api_message(e)}") from None
        return self._book

    def read(self, tabs: list[str]) -> dict[str, Grid]:
        book = self.book()
        try:
            existing = {ws.title for ws in book.worksheets()}
            wanted = [t for t in tabs if t in existing]
            out: dict[str, Grid] = {t: [] for t in tabs}
            if wanted:
                resp = book.values_batch_get(
                    [f"'{t}'" for t in wanted],
                    params={"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "SERIAL_NUMBER"},
                )
                for tab, vr in zip(wanted, resp.get("valueRanges", []), strict=False):
                    out[tab] = vr.get("values", [])
            return out
        except SourceError:
            raise
        except Exception as e:
            raise SourceError(f"Couldn't read the Google Sheet: {_api_message(e)}") from None

    def write(self, tabs: dict[str, Grid]) -> None:
        book = self.book()
        try:
            existing = {ws.title: ws for ws in book.worksheets()}
            for tab, grid in tabs.items():
                ws = existing.get(tab) or book.add_worksheet(
                    title=tab, rows=max(len(grid) + 50, 100), cols=max(len(grid[0]) if grid else 1, 5)
                )
                ws.clear()
                if grid:
                    ws.update(grid, "A1", value_input_option="RAW")
        except Exception as e:
            raise SourceError(f"Couldn't write to the Google Sheet: {_api_message(e)}") from None


def _api_message(e: Exception) -> str:
    resp = getattr(e, "response", None)
    try:
        return resp.json()["error"]["message"]  # type: ignore[union-attr]
    except Exception:
        return str(e)[:200]


def _service_account_email(path: Path) -> str | None:
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8")).get("client_email")
    except Exception:
        return None
