"""
Excel spreadsheet writer for cell counting results.
"""

import math
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
HEADER_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
BORDER = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"), bottom=Side(style="thin"),
)
CENTER = Alignment(horizontal="center", vertical="center")


def _safe_val(v, default=0):
    """Ensure a value is safe for Excel (no NaN, no Inf)."""
    if v is None:
        return default
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return default
    return v


def write_excel(filepath, results):
    """Write cell counting results to an Excel file.

    Args:
        filepath: Output .xlsx file path
        results: list of dicts, each with:
            filename, scene, total, live, dead, viability, has_dead, cell_details (optional)
    """
    wb = openpyxl.Workbook()

    # Sheet 1: Summary
    ws = wb.active
    ws.title = "Summary"
    headers = ["文件名", "Scene", "总细胞数", "活细胞数", "死细胞数", "存活率(%)"]
    _write_header(ws, headers)

    for ri, r in enumerate(results, start=2):
        vals = [
            r["filename"], r["scene"],
            _safe_val(r["total"], 0), _safe_val(r["live"], 0),
            _safe_val(r["dead"], 0), round(_safe_val(r["viability"], 0), 1),
        ]
        for ci, v in enumerate(vals, start=1):
            cell = ws.cell(row=ri, column=ci, value=v)
            cell.border = BORDER
            cell.alignment = CENTER

    _auto_fit(ws, headers)

    # Sheet 2: Detail
    has_detail = any(r.get("cell_details") for r in results)
    if has_detail:
        ws2 = wb.create_sheet("Detail")
        dheaders = ["文件名", "Scene", "细胞编号", "面积(px\xb2)", "圆形度", "平均强度", "是否死细胞"]
        _write_header(ws2, dheaders)

        ri = 2
        for r in results:
            for cd in r.get("cell_details", []):
                vals = [
                    r["filename"], r["scene"],
                    _safe_val(cd.get("label"), ""),
                    _safe_val(cd.get("area"), ""),
                    round(_safe_val(cd.get("circularity"), 0), 3),
                    round(_safe_val(cd.get("mean_intensity"), 0), 1),
                    "是" if cd.get("is_dead") else "否",
                ]
                for ci, v in enumerate(vals, start=1):
                    cell = ws2.cell(row=ri, column=ci, value=v)
                    cell.border = BORDER
                    cell.alignment = CENTER
                ri += 1

        _auto_fit(ws2, dheaders)

    wb.save(filepath)


def _write_header(ws, headers):
    for ci, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=ci, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = CENTER
        cell.border = BORDER


def _auto_fit(ws, headers):
    for ci, h in enumerate(headers, start=1):
        width = sum(2 if ord(c) > 127 else 1 for c in h) + 4
        ws.column_dimensions[get_column_letter(ci)].width = max(width, 12)
