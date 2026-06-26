#!/usr/bin/env python3
"""
Seed the services table from the provided XLSX.

Usage:
    python scripts/seed_services.py path/to/Справочник_услуг.xlsx

The XLSX columns expected:
  ID | Специальность | Code | Name_ru | TarificatrCode
"""
import sys
import uuid
import asyncio
import logging
from pathlib import Path

import openpyxl

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seeder")

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import AsyncSessionLocal, init_db
from app.models import Service


async def seed(xlsx_path: str):
    await init_db()

    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        logger.error("Empty sheet")
        return

    # Detect header row
    header = None
    data_start = 0
    for i, row in enumerate(rows[:10]):
        if row and any(str(v or "").strip() in ("ID", "Code", "Name_ru") for v in row):
            header = [str(v or "").strip() for v in row]
            data_start = i + 1
            break

    if not header:
        # Assume first row
        header = [str(v or "").strip() for v in rows[0]]
        data_start = 1

    logger.info(f"Header: {header}")

    # Map columns
    def col(name):
        try:
            return header.index(name)
        except ValueError:
            return None

    id_col = col("ID")
    spec_col = col("Специальность")
    code_col = col("Code")
    name_col = col("Name_ru")
    tarif_col = col("TarificatrCode")

    async with AsyncSessionLocal() as session:
        # Track inserted codes to avoid duplicates within the xlsx
        seen_codes: set = set()
        inserted = 0
        skipped = 0

        for row in rows[data_start:]:
            if not row or all(v is None for v in row):
                continue

            name = str(row[name_col]).strip() if name_col is not None and row[name_col] else None
            if not name or name.startswith("#"):
                skipped += 1
                continue

            code_val = row[code_col] if code_col is not None else None
            try:
                code_int = int(code_val) if code_val is not None else None
            except (ValueError, TypeError):
                code_int = None

            tarif = str(row[tarif_col]).strip() if tarif_col is not None and row[tarif_col] else None
            category = str(row[spec_col]).strip() if spec_col is not None and row[spec_col] else None

            # Dedup by (code, name) pair
            dedup_key = (code_int, name.lower())
            if dedup_key in seen_codes:
                skipped += 1
                continue
            seen_codes.add(dedup_key)

            svc = Service(
                service_id=uuid.uuid4(),
                external_code=code_int,
                service_name=name,
                category=category,
                tarification_code=tarif,
                synonyms=[],  # can be enriched later
                is_active=True,
            )
            session.add(svc)
            inserted += 1

            if inserted % 200 == 0:
                await session.flush()
                logger.info(f"  Flushed {inserted} services…")

        await session.commit()
        logger.info(f"✓ Seeded {inserted} services, skipped {skipped}")

    wb.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/seed_services.py <path_to_xlsx>")
        sys.exit(1)
    asyncio.run(seed(sys.argv[1]))
