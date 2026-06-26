"""
PDF Parser
- Text PDF: pdfplumber (table extraction) + PyMuPDF fallback
- Scanned PDF: Tesseract OCR with Russian language support
"""
import re
import logging
from pathlib import Path
from typing import List, Tuple, Optional
from decimal import Decimal, InvalidOperation

import pdfplumber
import fitz  # PyMuPDF
import pytesseract
from PIL import Image
import io

logger = logging.getLogger(__name__)

# Columns that likely contain prices (Russian keywords)
PRICE_KEYWORDS = [
    "цена", "стоимость", "прайс", "тариф", "сумма",
    "резидент", "нерезидент", "kzt", "тенге", "руб", "usd"
]
SERVICE_KEYWORDS = [
    "услуга", "наименование", "название", "код", "процедура",
    "исследование", "анализ", "прием"
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean_price(raw: str) -> Optional[Decimal]:
    """Extract numeric price from a raw cell string."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.,]", "", str(raw)).replace(",", ".")
    try:
        val = Decimal(cleaned)
        return val if val > 0 else None
    except InvalidOperation:
        return None


def _is_price_col(header: str) -> str | None:
    """Return 'resident', 'nonresident', or None."""
    h = header.lower()
    if "нерезидент" in h or "non" in h:
        return "nonresident"
    if any(k in h for k in ["цена", "стоимость", "резидент", "тариф", "сумма", "kzt"]):
        return "resident"
    return None


def _is_service_col(header: str) -> bool:
    h = header.lower()
    return any(k in h for k in SERVICE_KEYWORDS)


def _rows_to_items(rows: List[dict]) -> List[dict]:
    """Convert list-of-dicts (from table) → normalized price items."""
    results = []
    for row in rows:
        name = None
        price_r = None
        price_nr = None
        code = None

        for key, val in row.items():
            if val is None:
                continue
            kl = key.lower()
            if _is_service_col(kl) and not name:
                name = str(val).strip()
            elif "код" in kl and not code:
                code = str(val).strip()
            elif _is_price_col(kl) == "nonresident" and not price_nr:
                price_nr = _clean_price(str(val))
            elif _is_price_col(kl) == "resident" and not price_r:
                price_r = _clean_price(str(val))

        if name and len(name) > 2:
            results.append({
                "service_name_raw": name,
                "service_code_source": code,
                "price_resident_kzt": price_r,
                "price_nonresident_kzt": price_nr,
                "currency_original": "KZT",
            })
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Text PDF
# ─────────────────────────────────────────────────────────────────────────────

def parse_text_pdf(file_path: str) -> Tuple[List[dict], str]:
    """Returns (items, raw_text)."""
    items: List[dict] = []
    raw_parts: List[str] = []

    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                raw_parts.append(text)

                tables = page.extract_tables()
                for table in tables:
                    if not table or len(table) < 2:
                        continue

                    # First non-empty row = headers
                    header_row = None
                    data_start = 0
                    for i, row in enumerate(table):
                        if row and any(c for c in row if c):
                            header_row = [str(c or "").strip() for c in row]
                            data_start = i + 1
                            break

                    if not header_row:
                        continue

                    for row in table[data_start:]:
                        if not row or all(c is None or str(c).strip() == "" for c in row):
                            continue
                        row_dict = {
                            header_row[j]: row[j]
                            for j in range(min(len(header_row), len(row)))
                        }
                        items.extend(_rows_to_items([row_dict]))

    except Exception as e:
        logger.warning(f"pdfplumber failed: {e}, falling back to PyMuPDF")
        items, raw_text = _parse_pdf_pymupdf(file_path)
        return items, raw_text

    # If no table items found, attempt line-by-line heuristic parse
    raw_text = "\n".join(raw_parts)
    if not items:
        items = _heuristic_line_parse(raw_text)

    return items, raw_text


def _parse_pdf_pymupdf(file_path: str) -> Tuple[List[dict], str]:
    """PyMuPDF fallback: extract plain text and parse heuristically."""
    doc = fitz.open(file_path)
    raw_parts = []
    for page in doc:
        raw_parts.append(page.get_text())
    raw_text = "\n".join(raw_parts)
    return _heuristic_line_parse(raw_text), raw_text


# ─────────────────────────────────────────────────────────────────────────────
# Scanned PDF → OCR
# ─────────────────────────────────────────────────────────────────────────────

def parse_scan_pdf(file_path: str, dpi: int = 200) -> Tuple[List[dict], str]:
    """Run Tesseract on every page rendered as image."""
    doc = fitz.open(file_path)
    raw_parts: List[str] = []

    for page in doc:
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

        text = pytesseract.image_to_string(img, lang="rus+eng", config="--psm 6")
        raw_parts.append(text)

    raw_text = "\n".join(raw_parts)
    items = _heuristic_line_parse(raw_text)
    return items, raw_text


# ─────────────────────────────────────────────────────────────────────────────
# Heuristic line parser (for unstructured text)
# ─────────────────────────────────────────────────────────────────────────────

# Patterns: "Service name    12 000" or "Service name - 12000 тг"
_PRICE_LINE_RE = re.compile(
    r"^(.+?)\s{2,}(\d[\d\s.,]+)(?:\s+(\d[\d\s.,]+))?",
    re.MULTILINE
)
_PRICE_DASH_RE = re.compile(
    r"^(.+?)\s*[-–—]\s*(\d[\d\s.,]+)(?:\s*/\s*(\d[\d\s.,]+))?",
    re.MULTILINE
)


def _heuristic_line_parse(text: str) -> List[dict]:
    items: List[dict] = []
    seen: set = set()

    for pattern in [_PRICE_LINE_RE, _PRICE_DASH_RE]:
        for m in pattern.finditer(text):
            name = m.group(1).strip()
            if len(name) < 4 or name in seen:
                continue

            price_r = _clean_price(m.group(2).replace(" ", ""))
            price_nr = None
            if m.lastindex and m.lastindex >= 3 and m.group(3):
                price_nr = _clean_price(m.group(3).replace(" ", ""))

            if not price_r or price_r > Decimal("10000000"):
                continue

            seen.add(name)
            items.append({
                "service_name_raw": name,
                "service_code_source": None,
                "price_resident_kzt": price_r,
                "price_nonresident_kzt": price_nr,
                "currency_original": "KZT",
            })

    return items


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def is_scanned_pdf(file_path: str) -> bool:
    """Return True if the PDF likely has no text layer (scanned)."""
    try:
        doc = fitz.open(file_path)
        total_chars = sum(len(p.get_text()) for p in doc)
        return total_chars < 100  # virtually empty text
    except Exception:
        return False


def parse_pdf(file_path: str) -> Tuple[List[dict], str, str]:
    """
    Returns (items, raw_text, detected_format).
    detected_format: 'pdf' | 'scan_pdf'
    """
    if is_scanned_pdf(file_path):
        items, raw = parse_scan_pdf(file_path)
        return items, raw, "scan_pdf"
    else:
        items, raw = parse_text_pdf(file_path)
        return items, raw, "pdf"
