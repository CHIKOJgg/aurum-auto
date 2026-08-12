from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Iterable
from urllib.parse import quote

from .config import GoogleSheetsConfig
from .models import Direction, ExecutionResult, Signal


SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)
TRADE_SHEET = "Сделки"
WEEK_SHEET = "Недели"
SETTINGS_SHEET = "Настройки"
FIRST_DATA_ROW = 2
LAST_DATA_ROW = 1000

TRADE_HEADERS = (
    "ID сделки",
    "Счёт",
    "Открытие",
    "Закрытие",
    "Инструмент",
    "Направление",
    "Объём",
    "Цена входа",
    "Цена выхода",
    "Stop Loss",
    "Take Profit",
    "Комиссия",
    "Своп",
    "Валовый P&L",
    "Чистый P&L",
    "Неделя",
    "Моя доля P&L",
    "risk_base_usd",
    "Риск, USD",
    "Результат, R",
    "Комментарий",
    "Изм. моего капитала, %",
    "Статус",
)
WEEK_HEADERS = (
    "Неделя с",
    "Неделя по",
    "Мой капитал (начало)",
    "Пополнение / вывод",
    "P&L недели",
    "Доходность",
    "Моя доля P&L",
    "Мой капитал (конец)",
    "risk_base (начало)",
    "Новый risk_base (ввод)",
    "risk_base (конец)",
    "Статус",
    "Изм. моего капитала, %",
)
SETTINGS_HEADERS = ("Параметр", "Значение", "Как заполнять")
SETTINGS_LABELS = (
    ("Аккаунт", "Фиксировано: в учёт попадает только этот аккаунт"),
    ("Мой стартовый капитал, USD", "Введи свой стартовый капитал"),
    ("Стартовый risk_base_usd", "Введи текущее значение вручную"),
    ("Первая неделя", "Укажи понедельник первой недели учёта"),
    (
        "Правило risk_base",
        "Новое значение из листа Недели применяется со следующей недели",
    ),
    (
        "Комиссия и своп",
        "Вводятся отрицательными; чистый P&L считается автоматически",
    ),
)


@dataclass(frozen=True)
class TradeSnapshot:
    message_id: int
    account: str
    symbol: str
    direction: str
    volume: float
    open_time: str
    open_price: float
    close_time: str | None
    close_price: float | None
    commission: float
    swap: float
    gross_pnl: float | None
    status: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TradeSnapshot":
        return cls(
            message_id=int(data["message_id"]),
            account=str(data["account"]),
            symbol=str(data["symbol"]),
            direction=str(data["direction"]),
            volume=float(data["volume"]),
            open_time=str(data["open_time"]),
            open_price=float(data["open_price"]),
            close_time=(
                None if data.get("close_time") in (None, "") else str(data["close_time"])
            ),
            close_price=(
                None
                if data.get("close_price") in (None, "")
                else float(data["close_price"])
            ),
            commission=float(data.get("commission", 0)),
            swap=float(data.get("swap", 0)),
            gross_pnl=(
                None if data.get("gross_pnl") is None else float(data["gross_pnl"])
            ),
            status=str(data["status"]),
        )


class SheetsTradeJournal:
    """Failure-isolated Google Sheets writer for the fxpro trade journal."""

    def __init__(
        self,
        config: GoogleSheetsConfig,
        *,
        session: Any | None = None,
    ):
        self.config = config
        self._session = session
        self._operation_lock = RLock()

    def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session
        credentials_path = Path(self.config.credentials_file)
        if not credentials_path.is_file():
            raise FileNotFoundError(
                f"Google service-account key not found: {credentials_path}"
            )
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials

        credentials = Credentials.from_service_account_file(
            str(credentials_path),
            scopes=SCOPES,
        )
        self._session = AuthorizedSession(credentials)
        return self._session

    def _values_url(self, range_name: str | None = None) -> str:
        base = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            f"{self.config.spreadsheet_id}/values"
        )
        if range_name is None:
            return base
        return f"{base}/{quote(range_name, safe='')}"

    def _spreadsheet_url(self) -> str:
        return (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            f"{self.config.spreadsheet_id}"
        )

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._ensure_session().request(
            method,
            url,
            params=params,
            json=body,
            timeout=20,
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def check_access(self) -> None:
        self._request_json(
            "GET",
            self._values_url(f"'{TRADE_SHEET}'!A1:B2"),
        )

    def _read_values(self, range_name: str) -> list[list[Any]]:
        payload = self._request_json(
            "GET",
            self._values_url(range_name),
            params={"valueRenderOption": "FORMULA"},
        )
        return list(payload.get("values", []))

    def _spreadsheet_metadata(self) -> dict[str, Any]:
        return self._request_json(
            "GET",
            self._spreadsheet_url(),
            params={
                "fields": "spreadsheetId,spreadsheetUrl,sheets.properties"
            },
        )

    def _batch_update_spreadsheet(self, requests: list[dict[str, Any]]) -> None:
        if not requests:
            return
        self._request_json(
            "POST",
            self._spreadsheet_url() + ":batchUpdate",
            body={"requests": requests},
        )

    @staticmethod
    def _sheet_properties(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(sheet["properties"]["title"]): dict(sheet["properties"])
            for sheet in metadata.get("sheets", [])
        }

    def _template_headers_match(self) -> bool:
        expected = {
            TRADE_SHEET: list(TRADE_HEADERS),
            WEEK_SHEET: list(WEEK_HEADERS),
            SETTINGS_SHEET: list(SETTINGS_HEADERS),
        }
        for title, headers in expected.items():
            end_column = {22: "V", 13: "M", 3: "C"}[len(headers)]
            rows = self._read_values(f"'{title}'!A1:{end_column}1")
            if not rows or rows[0] != headers:
                return False
        return True

    def ensure_template(self) -> None:
        """Validate the owner-only template or initialize a completely blank sheet."""
        try:
            if self._template_headers_match():
                return
        except Exception:
            pass
        self.setup_new_template()

    def setup_new_template(self) -> None:
        """Initialize an empty spreadsheet; never rewrite a populated workbook."""
        metadata = self._spreadsheet_metadata()
        properties = self._sheet_properties(metadata)
        for title, sheet in properties.items():
            grid = sheet.get("gridProperties", {})
            row_count = max(1, int(grid.get("rowCount", 1000)))
            column_count = max(1, int(grid.get("columnCount", 26)))
            end_column_index = min(column_count, 26)
            end_column = chr(ord("A") + end_column_index - 1)
            rows = self._read_values(f"'{title}'!A1:{end_column}{row_count}")
            if any(any(value not in ("", None) for value in row) for row in rows):
                raise RuntimeError(
                    "Automatic Google Sheets template setup is allowed only for a "
                    "completely empty spreadsheet. The existing workbook was not changed."
                )

        requests: list[dict[str, Any]] = []
        reusable_title: str | None = None
        if len(properties) == 1:
            only_title = next(iter(properties))
            if only_title not in {TRADE_SHEET, WEEK_SHEET, SETTINGS_SHEET}:
                reusable_title = only_title
                requests.append(
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": int(properties[only_title]["sheetId"]),
                                "title": TRADE_SHEET,
                                "gridProperties": {
                                    "rowCount": LAST_DATA_ROW,
                                    "columnCount": len(TRADE_HEADERS),
                                },
                            },
                            "fields": "title,gridProperties(rowCount,columnCount)",
                        }
                    }
                )
        existing_after_rename = set(properties)
        if reusable_title is not None:
            existing_after_rename.remove(reusable_title)
            existing_after_rename.add(TRADE_SHEET)
        for title, rows, columns in (
            (TRADE_SHEET, LAST_DATA_ROW, len(TRADE_HEADERS)),
            (WEEK_SHEET, 300, len(WEEK_HEADERS)),
            (SETTINGS_SHEET, 100, len(SETTINGS_HEADERS)),
        ):
            if title not in existing_after_rename:
                requests.append(
                    {
                        "addSheet": {
                            "properties": {
                                "title": title,
                                "gridProperties": {
                                    "rowCount": rows,
                                    "columnCount": columns,
                                },
                            }
                        }
                    }
                )
        self._batch_update_spreadsheet(requests)
        metadata = self._spreadsheet_metadata()
        properties = self._sheet_properties(metadata)
        self._write_template_values()
        self._format_and_prefill_template(properties)

    def _write_template_values(self) -> None:
        settings_data = [
            self._range_for_sheet(SETTINGS_SHEET, 1, "A:C", list(SETTINGS_HEADERS)),
            self._range_for_sheet(
                SETTINGS_SHEET,
                2,
                "A:A",
                [item[0] for item in SETTINGS_LABELS],
                major_dimension="COLUMNS",
            ),
            self._range_for_sheet(
                SETTINGS_SHEET,
                2,
                "C:C",
                [item[1] for item in SETTINGS_LABELS],
                major_dimension="COLUMNS",
            ),
            self._range_for_sheet(SETTINGS_SHEET, 2, "B", [self.config.account]),
        ]
        self._batch_write(
            [
                self._range_for_sheet(TRADE_SHEET, 1, "A:V", list(TRADE_HEADERS)),
                self._range_for_sheet(WEEK_SHEET, 1, "A:M", list(WEEK_HEADERS)),
                *settings_data,
                *self._trade_formula_updates(2),
                *self._week_formula_updates(2),
                *self._week_formula_updates(3),
            ]
        )

    def _format_and_prefill_template(
        self,
        properties: dict[str, dict[str, Any]],
    ) -> None:
        requests: list[dict[str, Any]] = []
        for title, column_count in (
            (TRADE_SHEET, len(TRADE_HEADERS)),
            (WEEK_SHEET, len(WEEK_HEADERS)),
            (SETTINGS_SHEET, len(SETTINGS_HEADERS)),
        ):
            sheet_id = int(properties[title]["sheetId"])
            requests.extend(
                [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                                "startColumnIndex": 0,
                                "endColumnIndex": column_count,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": {
                                        "red": 0.93,
                                        "green": 0.93,
                                        "blue": 0.93,
                                    },
                                    "textFormat": {"bold": True},
                                    "horizontalAlignment": "CENTER",
                                    "wrapStrategy": "WRAP",
                                }
                            },
                            "fields": (
                                "userEnteredFormat(backgroundColor,textFormat,"
                                "horizontalAlignment,wrapStrategy)"
                            ),
                        }
                    },
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": sheet_id,
                                "gridProperties": {"frozenRowCount": 1},
                            },
                            "fields": "gridProperties.frozenRowCount",
                        }
                    },
                ]
            )

        trade_id = int(properties[TRADE_SHEET]["sheetId"])
        week_id = int(properties[WEEK_SHEET]["sheetId"])
        for column in (14, 15, 16, 19, 21):
            requests.append(
                self._copy_formula_request(trade_id, 1, 2, LAST_DATA_ROW, column)
            )
        for column in (0, 1, 2, 4, 5, 6, 7, 8, 10, 11, 12):
            requests.append(
                self._copy_formula_request(week_id, 2, 3, 300, column)
            )
        requests.append(
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": trade_id,
                        "startRowIndex": 1,
                        "endRowIndex": LAST_DATA_ROW,
                        "startColumnIndex": 5,
                        "endColumnIndex": 6,
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_LIST",
                            "values": [
                                {"userEnteredValue": "BUY"},
                                {"userEnteredValue": "SELL"},
                            ],
                        },
                        "strict": True,
                        "showCustomUi": True,
                    },
                }
            }
        )
        self._batch_update_spreadsheet(requests)

    @staticmethod
    def _copy_formula_request(
        sheet_id: int,
        source_row: int,
        start_row: int,
        end_row: int,
        column: int,
    ) -> dict[str, Any]:
        return {
            "copyPaste": {
                "source": {
                    "sheetId": sheet_id,
                    "startRowIndex": source_row,
                    "endRowIndex": source_row + 1,
                    "startColumnIndex": column,
                    "endColumnIndex": column + 1,
                },
                "destination": {
                    "sheetId": sheet_id,
                    "startRowIndex": start_row,
                    "endRowIndex": end_row,
                    "startColumnIndex": column,
                    "endColumnIndex": column + 1,
                },
                "pasteType": "PASTE_FORMULA",
                "pasteOrientation": "NORMAL",
            }
        }

    def _trade_rows(self) -> tuple[dict[int, int], list[int]]:
        payload = self._request_json(
            "GET",
            self._values_url(
                f"'{TRADE_SHEET}'!A{FIRST_DATA_ROW}:A{LAST_DATA_ROW}"
            ),
            params={"valueRenderOption": "UNFORMATTED_VALUE"},
        )
        rows = payload.get("values", [])
        existing: dict[int, int] = {}
        free: list[int] = []
        for offset in range(LAST_DATA_ROW - FIRST_DATA_ROW + 1):
            row_number = FIRST_DATA_ROW + offset
            value = rows[offset][0] if offset < len(rows) and rows[offset] else ""
            if value in ("", None):
                free.append(row_number)
                continue
            try:
                existing[int(value)] = row_number
            except (TypeError, ValueError):
                continue
        return existing, free

    def _batch_write(self, data: list[dict[str, Any]]) -> None:
        if not data:
            return
        self._request_json(
            "POST",
            self._values_url() + ":batchUpdate",
            body={
                "valueInputOption": "USER_ENTERED",
                "data": data,
            },
        )

    @staticmethod
    def _range_for_sheet(
        sheet: str,
        row: int,
        columns: str,
        values: list[Any],
        *,
        major_dimension: str = "ROWS",
    ) -> dict[str, Any]:
        if ":" in columns:
            start_column, end_column = columns.split(":", 1)
            end_row = row + len(values) - 1 if major_dimension == "COLUMNS" else row
            a1_range = f"{start_column}{row}:{end_column}{end_row}"
        else:
            a1_range = f"{columns}{row}"
        return {
            "range": f"'{sheet}'!{a1_range}",
            "majorDimension": major_dimension,
            "values": [values],
        }

    @staticmethod
    def _range(row: int, columns: str, values: list[Any]) -> dict[str, Any]:
        return SheetsTradeJournal._range_for_sheet(
            TRADE_SHEET,
            row,
            columns,
            values,
        )

    @classmethod
    def _trade_formula_updates(cls, row: int) -> list[dict[str, Any]]:
        return [
            cls._range_for_sheet(
                TRADE_SHEET,
                row,
                "O",
                [f'=IF(N{row}="";"";N{row}+IF(L{row}="";0;L{row})+IF(M{row}="";0;M{row}))'],
            ),
            cls._range_for_sheet(
                TRADE_SHEET,
                row,
                "P",
                [f'=IF(D{row}="";"";INT(D{row})-WEEKDAY(INT(D{row});2)+1)'],
            ),
            cls._range_for_sheet(
                TRADE_SHEET,
                row,
                "Q",
                [f'=IF(O{row}="";"";O{row})'],
            ),
            cls._range_for_sheet(
                TRADE_SHEET,
                row,
                "T",
                [f'=IF(OR(O{row}="";S{row}="";S{row}=0);"";O{row}/S{row})'],
            ),
            cls._range_for_sheet(
                TRADE_SHEET,
                row,
                "V",
                [
                    f'=IF(OR(Q{row}="";P{row}="");"";IFERROR('
                    f'Q{row}/XLOOKUP(P{row};\'{WEEK_SHEET}\'!$A$2:$A$300;'
                    f'\'{WEEK_SHEET}\'!$C$2:$C$300);""))'
                ],
            ),
        ]

    @classmethod
    def _week_formula_updates(cls, row: int) -> list[dict[str, Any]]:
        previous = row - 1
        if row == 2:
            week_start = f"='{SETTINGS_SHEET}'!B5"
            capital_start = f"='{SETTINGS_SHEET}'!B3"
            risk_start = f"='{SETTINGS_SHEET}'!B4"
        else:
            week_start = (
                f'=IF(OR(A{previous}="";\'{SETTINGS_SHEET}\'!$B$5="");'
                f'"";A{previous}+7)'
            )
            capital_start = f'=IF(A{row}="";"";H{previous})'
            risk_start = f'=IF(A{row}="";"";K{previous})'
        formulas = {
            "A": week_start,
            "B": f'=IF(A{row}="";"";A{row}+6)',
            "C": capital_start,
            "E": (
                f'=IF(OR(A{row}="";C{row}="");"";SUMIFS('
                f"'{TRADE_SHEET}'!$O$2:$O$1000;"
                f"'{TRADE_SHEET}'!$D$2:$D$1000;\">=\"&A{row};"
                f"'{TRADE_SHEET}'!$D$2:$D$1000;\"<\"&A{row}+7))"
            ),
            "F": f'=IFERROR(E{row}/C{row};"")',
            "G": f'=IF(E{row}="";"";E{row})',
            "H": (
                f'=IF(C{row}="";"";C{row}+IF(D{row}="";0;D{row})+'
                f'IF(G{row}="";0;G{row}))'
            ),
            "I": risk_start,
            "K": f'=IF(OR(A{row}="";C{row}="");"";IF(J{row}="";I{row};J{row}))',
            "L": (
                f'=IF(OR(A{row}="";C{row}="");"";IF(J{row}="";'
                '"Без изменений";"Применится со следующей недели"))'
            ),
            "M": f'=IF(OR(H{row}="";C{row}="";C{row}=0);"";H{row}/C{row}-1)',
        }
        return [
            cls._range_for_sheet(WEEK_SHEET, row, column, [formula])
            for column, formula in formulas.items()
        ]

    def record_execution(
        self,
        signal: Signal,
        result: ExecutionResult,
        *,
        risk_base_usd: float,
        risk_percent: float,
        recorded_at: datetime,
    ) -> None:
        with self._operation_lock:
            self._record_execution(
                signal,
                result,
                risk_base_usd=risk_base_usd,
                risk_percent=risk_percent,
                recorded_at=recorded_at,
            )

    def _record_execution(
        self,
        signal: Signal,
        result: ExecutionResult,
        *,
        risk_base_usd: float,
        risk_percent: float,
        recorded_at: datetime,
    ) -> None:
        if result.account != self.config.account or result.status != "executed":
            return
        existing, free = self._trade_rows()
        row = existing.get(signal.message_id)
        is_new = row is None
        if row is None:
            if not free:
                raise RuntimeError("No free rows remain in Сделки!A2:A1000")
            row = free[0]

        side = "BUY" if signal.direction is Direction.LONG else "SELL"
        detail = (
            f"{result.execution_kind or 'executed'}; "
            f"ticket={result.ticket or ''}; {result.detail}"
        )
        updates = [
            self._range(row, "A:B", [signal.message_id, result.account]),
            self._range(
                row,
                "E:H",
                [signal.symbol, side, result.volume or "", signal.entry],
            ),
            self._range(row, "J:K", [signal.stop_loss, signal.take_profit]),
            self._range(
                row,
                "R:S",
                [
                    risk_base_usd,
                    risk_base_usd * risk_percent / 100,
                ],
            ),
            self._range(row, "U", [detail]),
            *self._trade_formula_updates(row),
        ]
        if is_new and result.execution_kind == "market":
            updates.append(
                self._range(
                    row,
                    "C",
                    [recorded_at.astimezone().strftime("%d.%m.%Y %H:%M:%S")],
                )
            )
        self._batch_write(updates)

    def upsert_snapshots(self, snapshots: Iterable[TradeSnapshot]) -> int:
        with self._operation_lock:
            return self._upsert_snapshots(snapshots)

    @staticmethod
    def _sheet_datetime(value: str | None) -> str:
        if not value:
            return ""
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%d.%m.%Y %H:%M:%S")

    def _upsert_snapshots(self, snapshots: Iterable[TradeSnapshot]) -> int:
        filtered = [
            snapshot
            for snapshot in snapshots
            if snapshot.account == self.config.account
        ]
        if not filtered:
            return 0
        existing, free = self._trade_rows()
        next_free = iter(free)
        updates: list[dict[str, Any]] = []
        count = 0
        for snapshot in sorted(filtered, key=lambda item: item.message_id):
            row = existing.get(snapshot.message_id)
            if row is None:
                try:
                    row = next(next_free)
                except StopIteration as exc:
                    raise RuntimeError(
                        "No free rows remain in Сделки!A2:A1000"
                    ) from exc
                existing[snapshot.message_id] = row
            updates.extend(
                [
                    self._range(
                        row,
                        "A:B",
                        [snapshot.message_id, snapshot.account],
                    ),
                    self._range(
                        row,
                        "C:D",
                        [
                            self._sheet_datetime(snapshot.open_time),
                            self._sheet_datetime(snapshot.close_time),
                        ],
                    ),
                    self._range(
                        row,
                        "E:I",
                        [
                            snapshot.symbol,
                            snapshot.direction,
                            snapshot.volume,
                            snapshot.open_price,
                            snapshot.close_price or "",
                        ],
                    ),
                    self._range(
                        row,
                        "L:N",
                        [
                            snapshot.commission,
                            snapshot.swap,
                            (
                                ""
                                if snapshot.gross_pnl is None
                                else snapshot.gross_pnl
                            ),
                        ],
                    ),
                    self._range(row, "W", [snapshot.status]),
                    *self._trade_formula_updates(row),
                ]
            )
            count += 1
        self._batch_write(updates)
        return count
