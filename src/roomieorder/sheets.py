"""Google Sheets logging — one appended row per buy attempt (PLAN §3.5).

Auth is a service account: create one in Google Cloud, download its JSON key,
and share the target sheet with the account's email (editor). The sheet id and
key path come from config; when either is unset, logging degrades to a no-op
logger so the rest of the pipeline still runs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional, Protocol

from roomieorder.config import Config

_logger = logging.getLogger(__name__)

# Column order for the worksheet header + every appended row.
COLUMNS = [
    "timestamp",
    "item_key",
    "title",
    "item_number",
    "qty",
    "unit_price",
    "order_total",
    "order_id",
    "status",
    "requester",
    "notes",
]

# gspread needs the Sheets + Drive scopes to open a sheet by key and append.
_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


class SheetRow(Protocol):
    timestamp: str
    item_key: str
    title: str
    item_number: str
    qty: int
    unit_price: Optional[float]
    order_total: Optional[float]
    order_id: Optional[str]
    status: str
    requester: str
    notes: str


def row_to_values(row: dict[str, object]) -> list[object]:
    """Project a record dict onto COLUMNS, defaulting missing keys to ''."""
    return [row.get(col, "") if row.get(col) is not None else "" for col in COLUMNS]


class SheetsLogger:
    """Append-only client for the orders worksheet.

    The gspread client is built lazily on first append so importing this module
    (and constructing the logger) never touches the network or the key file —
    handy for tests and for booting the service before Sheets is configured.
    """

    def __init__(self, service_account_json: str, sheet_id: str, tab: str) -> None:
        self.service_account_json = service_account_json
        self.sheet_id = sheet_id
        self.tab = tab
        self._worksheet: object | None = None

    def _open(self) -> object:
        if self._worksheet is not None:
            return self._worksheet

        import gspread  # imported lazily; heavy + optional
        from google.oauth2.service_account import Credentials
        from gspread.utils import ValueInputOption

        key_path = Path(self.service_account_json)
        info = json.loads(key_path.read_text())
        creds = Credentials.from_service_account_info(info, scopes=_SCOPES)  # type: ignore[no-untyped-call]
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(self.sheet_id)
        try:
            ws = spreadsheet.worksheet(self.tab)
            # A hand-created tab won't have our header row, which leaves the
            # append table unanchored — write it once so row 1 is always the
            # header and appends stay in columns A:K.
            if ws.row_values(1) != COLUMNS:
                ws.update(
                    [COLUMNS], range_name="A1", value_input_option=ValueInputOption.raw
                )
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=self.tab, rows=1000, cols=len(COLUMNS))
            ws.append_row(COLUMNS, value_input_option=ValueInputOption.raw)
        self._worksheet = ws
        return ws

    def append(self, row: dict[str, object]) -> bool:
        """Append one attempt row. Returns False on any failure (never raises).

        Logging must not be able to undo a real purchase, so a Sheets outage is
        swallowed here and surfaced via the notifier/journal instead.
        """
        try:
            ws = self._open()
            # Pin appends to the header table at A1. Without table_range, gspread
            # auto-detects the "table" from the used range; because each row we
            # append leaves trailing empty cells (order_total/order_id are often
            # blank), that range creeps rightward and every subsequent row lands
            # further right in a diagonal staircase — data ends up off-screen
            # instead of under the headers. Anchoring at A1 keeps every row in
            # columns A:K.
            ws.append_row(  # type: ignore[attr-defined]
                row_to_values(row),
                value_input_option="USER_ENTERED",
                table_range="A1",
            )
        except Exception as exc:  # noqa: BLE001 — best-effort logging
            _logger.error("sheets append failed: %s", exc)
            return False
        return True


class NullSheets:
    """No-op logger used when Sheets is not configured."""

    def append(self, row: dict[str, object]) -> bool:
        _logger.info("sheets disabled; would log: %s", row)
        return True


SheetsClient = SheetsLogger | NullSheets


def build_sheets(config: Config) -> SheetsClient:
    if config.sheets_enabled:
        return SheetsLogger(
            service_account_json=config.google_service_account_json,
            sheet_id=config.sheet_id,
            tab=config.sheet_tab,
        )
    return NullSheets()
