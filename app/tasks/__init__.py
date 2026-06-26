"""
Celery tasks for background document parsing.
Each task:
  1. Updates doc status → processing
  2. Dispatches to correct parser
  3. Validates each price item
  4. Normalizes against service directory
  5. Deduplicates
  6. Saves to DB
  7. Updates doc status → done | error
"""
import logging
import os
from celery import Celery
from pathlib import Path
from datetime import datetime
from decimal import Decimal

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

celery_app = Celery(
    "medarchive",
    broker=settings.redis_url,
    backend=settings.redis_url,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)


def _get_sync_db():
    """Synchronous DB session for Celery workers (psycopg2)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    sync_url = settings.database_url.replace("+asyncpg", "")
    engine = create_engine(sync_url)
    Session = sessionmaker(bind=engine)
    return Session()


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def process_document(self, doc_id: str):
    """Main parsing task."""
    from app.models import PriceDocument, PriceItem, ParseStatus, FileFormat, Currency
    from app.parsers.pdf_parser import parse_pdf
    from app.parsers.docx_parser import parse_docx
    from app.parsers.xlsx_parser import parse_xlsx
    from app.parsers.validator import validate_price_item, check_price_anomaly
    from app.normalizer import match_service
    import uuid

    db = _get_sync_db()
    log_lines = []

    try:
        doc = db.query(PriceDocument).filter(PriceDocument.doc_id == doc_id).first()
        if not doc:
            logger.error(f"Doc {doc_id} not found")
            return

        doc.parse_status = ParseStatus.processing
        db.commit()

        file_path = doc.file_path
        fmt = doc.file_format

        # ── Parse ─────────────────────────────────────────────────────────────
        raw_items = []
        raw_text = ""

        if fmt in (FileFormat.pdf, FileFormat.scan_pdf):
            raw_items, raw_text, detected = parse_pdf(file_path)
            if detected == "scan_pdf" and fmt == FileFormat.pdf:
                doc.file_format = FileFormat.scan_pdf
            log_lines.append(f"Parsed {len(raw_items)} items from {detected}")

        elif fmt == FileFormat.docx:
            raw_items, raw_text = parse_docx(file_path)
            log_lines.append(f"Parsed {len(raw_items)} items from DOCX")

        elif fmt == FileFormat.xlsx:
            raw_items, raw_text = parse_xlsx(file_path)
            log_lines.append(f"Parsed {len(raw_items)} items from XLSX")

        doc.raw_content = raw_text[:50000]  # cap to 50KB

        if not raw_items:
            doc.parse_status = ParseStatus.error
            doc.parse_log = "No items extracted"
            db.commit()
            return

        # ── Validate + Normalize + Save ───────────────────────────────────────
        saved = 0
        skipped = 0
        anomalies = 0

        for raw in raw_items:
            validated = validate_price_item(raw, doc.effective_date)
            if not validated.get("is_valid"):
                skipped += 1
                log_lines.extend(validated.get("validation_warnings", []))
                continue

            name_raw = validated["service_name_raw"]
            price_r = validated.get("price_resident_kzt")
            price_nr = validated.get("price_nonresident_kzt")
            currency = validated.get("currency_original", "KZT")

            # Normalize
            from app.config import get_settings as gs
            s = gs()
            svc_id, score = match_service(
                name_raw,
                auto_threshold=s.auto_match_threshold,
                review_threshold=s.review_threshold,
            )

            # Dedup: archive old active item for same partner+service+date
            if svc_id:
                existing = db.query(PriceItem).filter(
                    PriceItem.partner_id == doc.partner_id,
                    PriceItem.service_id == svc_id,
                    PriceItem.effective_date == doc.effective_date,
                    PriceItem.is_active == True,
                ).first()
                if existing:
                    is_anomaly = check_price_anomaly(
                        Decimal(str(price_r)) if price_r else None,
                        existing.price_resident_kzt,
                        s.price_anomaly_pct,
                    )
                    if is_anomaly:
                        anomalies += 1
                    existing.is_active = False
                    db.add(existing)

            item = PriceItem(
                item_id=uuid.uuid4(),
                doc_id=doc.doc_id,
                partner_id=doc.partner_id,
                service_name_raw=name_raw,
                service_code_source=validated.get("service_code_source"),
                service_id=uuid.UUID(svc_id) if svc_id else None,
                match_score=Decimal(str(round(score, 2))) if score else None,
                price_resident_kzt=Decimal(str(price_r)) if price_r else None,
                price_nonresident_kzt=Decimal(str(price_nr)) if price_nr else None,
                price_original=Decimal(str(validated.get("price_original") or price_r or 0)),
                currency_original=currency,
                is_anomaly=False,
                effective_date=doc.effective_date,
                is_active=True,
            )

            # Flag warnings
            warns = validated.get("validation_warnings", [])
            if warns:
                item.verification_note = "; ".join(warns)

            db.add(item)
            saved += 1

        doc.parse_status = ParseStatus.done
        doc.parsed_at = datetime.utcnow()
        doc.parse_log = "\n".join(log_lines + [
            f"Saved: {saved}, Skipped: {skipped}, Anomalies: {anomalies}"
        ])
        db.commit()
        logger.info(f"Doc {doc_id} done: {saved} items saved")

    except Exception as exc:
        logger.exception(f"Error processing doc {doc_id}: {exc}")
        db.rollback()
        try:
            doc = db.query(PriceDocument).filter(PriceDocument.doc_id == doc_id).first()
            if doc:
                doc.parse_status = ParseStatus.error
                doc.parse_log = str(exc)[:2000]
                db.commit()
        except Exception:
            pass
        raise self.retry(exc=exc)
    finally:
        db.close()
