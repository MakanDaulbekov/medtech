"""
REST API — all endpoints from spec:
GET  /services
GET  /services/{id}/partners
GET  /partners
GET  /partners/{id}/services
GET  /search?q=
GET  /unmatched
POST /match
POST /upload          (ZIP archive upload)
GET  /documents       (status tracking)
GET  /dashboard       (metrics)
"""
import os
import uuid
import zipfile
import shutil
import logging
from pathlib import Path
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_, update
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Partner, PriceDocument, PriceItem, Service, ParseStatus, FileFormat
from app.schemas import (
    PartnerOut, PartnerCreate, ServiceOut, PriceItemOut,
    PartnerWithPrice, DocumentOut, MatchRequest, MatchResponse,
    UploadResponse, DashboardStats
)
from app.config import get_settings
from app.tasks import process_document

router = APIRouter()
logger = logging.getLogger(__name__)
settings = get_settings()


def _detect_format(filename: str) -> FileFormat:
    ext = Path(filename).suffix.lower()
    return {
        ".pdf": FileFormat.pdf,
        ".docx": FileFormat.docx,
        ".doc": FileFormat.docx,
        ".xlsx": FileFormat.xlsx,
        ".xls": FileFormat.xlsx,
    }.get(ext, FileFormat.pdf)


# ─── /upload ─────────────────────────────────────────────────────────────────

@router.post("/upload", response_model=UploadResponse, tags=["Upload"])
async def upload_archive(
    partner_name: str = Form(...),
    city: Optional[str] = Form(None),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Upload a ZIP archive of price documents for a partner."""
    if not file.filename.endswith(".zip"):
        raise HTTPException(400, "Only ZIP archives accepted")

    # Upsert partner
    result = await db.execute(
        select(Partner).where(Partner.name == partner_name)
    )
    partner = result.scalar_one_or_none()
    if not partner:
        partner = Partner(
            partner_id=uuid.uuid4(),
            name=partner_name,
            city=city,
        )
        db.add(partner)
        await db.flush()

    # Save ZIP to disk
    partner_dir = Path(settings.upload_dir) / str(partner.partner_id)
    partner_dir.mkdir(parents=True, exist_ok=True)

    zip_path = partner_dir / f"{uuid.uuid4()}.zip"
    content = await file.read()
    zip_path.write_bytes(content)

    # Extract ZIP
    extract_dir = partner_dir / "extracted"
    extract_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    # Create PriceDocument records + queue tasks
    doc_ids = []
    for fpath in extract_dir.rglob("*"):
        if not fpath.is_file():
            continue
        if fpath.suffix.lower() not in (".pdf", ".docx", ".doc", ".xlsx", ".xls"):
            continue

        fmt = _detect_format(fpath.name)
        doc = PriceDocument(
            doc_id=uuid.uuid4(),
            partner_id=partner.partner_id,
            file_name=fpath.name,
            file_path=str(fpath),
            file_format=fmt,
            parse_status=ParseStatus.pending,
        )
        db.add(doc)
        await db.flush()
        doc_ids.append(doc.doc_id)

        # Queue Celery task
        process_document.delay(str(doc.doc_id))

    await db.commit()

    return UploadResponse(
        partner_id=partner.partner_id,
        partner_name=partner.name,
        docs_queued=len(doc_ids),
        doc_ids=doc_ids,
    )


# ─── /services ───────────────────────────────────────────────────────────────

@router.get("/services", response_model=List[ServiceOut], tags=["Services"])
async def list_services(
    category: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Service).where(Service.is_active == True)
    if category:
        stmt = stmt.where(Service.category.ilike(f"%{category}%"))
    if q:
        stmt = stmt.where(
            or_(
                Service.service_name.ilike(f"%{q}%"),
                Service.tarification_code.ilike(f"%{q}%"),
            )
        )
    stmt = stmt.offset(offset).limit(limit).order_by(Service.category, Service.service_name)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/services/{service_id}/partners", response_model=List[PartnerWithPrice], tags=["Services"])
async def service_partners(
    service_id: uuid.UUID,
    city: Optional[str] = Query(None),
    verified_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """List partners offering a given service with prices."""
    stmt = (
        select(
            PriceItem.item_id,
            Partner.partner_id,
            Partner.name.label("partner_name"),
            Partner.city,
            PriceItem.price_resident_kzt,
            PriceItem.price_nonresident_kzt,
            PriceItem.effective_date,
            PriceItem.is_verified,
        )
        .join(Partner, PriceItem.partner_id == Partner.partner_id)
        .where(
            PriceItem.service_id == service_id,
            PriceItem.is_active == True,
            Partner.is_active == True,
        )
    )
    if city:
        stmt = stmt.where(Partner.city.ilike(f"%{city}%"))
    if verified_only:
        stmt = stmt.where(PriceItem.is_verified == True)

    stmt = stmt.order_by(PriceItem.price_resident_kzt.asc().nullslast())
    result = await db.execute(stmt)
    rows = result.all()

    return [
        PartnerWithPrice(
            partner_id=r.partner_id,
            partner_name=r.partner_name,
            city=r.city,
            price_resident_kzt=float(r.price_resident_kzt) if r.price_resident_kzt else None,
            price_nonresident_kzt=float(r.price_nonresident_kzt) if r.price_nonresident_kzt else None,
            effective_date=r.effective_date,
            is_verified=r.is_verified,
        )
        for r in rows
    ]


# ─── /partners ────────────────────────────────────────────────────────────────

@router.get("/partners", response_model=List[PartnerOut], tags=["Partners"])
async def list_partners(
    city: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Partner)
    if city:
        stmt = stmt.where(Partner.city.ilike(f"%{city}%"))
    if is_active is not None:
        stmt = stmt.where(Partner.is_active == is_active)
    stmt = stmt.offset(offset).limit(limit).order_by(Partner.name)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/partners/{partner_id}/services", response_model=List[PriceItemOut], tags=["Partners"])
async def partner_services(
    partner_id: uuid.UUID,
    active_only: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    """All price items for a given partner."""
    stmt = (
        select(PriceItem)
        .where(PriceItem.partner_id == partner_id)
    )
    if active_only:
        stmt = stmt.where(PriceItem.is_active == True)
    stmt = stmt.order_by(PriceItem.service_name_raw)
    result = await db.execute(stmt)
    items = result.scalars().all()

    return [
        PriceItemOut(
            item_id=i.item_id,
            service_name_raw=i.service_name_raw,
            service_id=i.service_id,
            match_score=float(i.match_score) if i.match_score else None,
            price_resident_kzt=float(i.price_resident_kzt) if i.price_resident_kzt else None,
            price_nonresident_kzt=float(i.price_nonresident_kzt) if i.price_nonresident_kzt else None,
            currency_original=i.currency_original,
            is_verified=i.is_verified,
            is_anomaly=i.is_anomaly,
            effective_date=i.effective_date,
        )
        for i in items
    ]


# ─── /search ─────────────────────────────────────────────────────────────────

@router.get("/search", tags=["Search"])
async def full_text_search(
    q: str = Query(..., min_length=2),
    limit: int = Query(30, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Full-text search across services and partners."""
    pattern = f"%{q}%"

    svc_stmt = (
        select(Service)
        .where(
            and_(
                Service.is_active == True,
                or_(
                    Service.service_name.ilike(pattern),
                    Service.category.ilike(pattern),
                    Service.tarification_code.ilike(pattern),
                )
            )
        )
        .limit(limit)
    )
    svc_result = await db.execute(svc_stmt)
    services = svc_result.scalars().all()

    partner_stmt = (
        select(Partner)
        .where(
            and_(
                Partner.is_active == True,
                or_(
                    Partner.name.ilike(pattern),
                    Partner.city.ilike(pattern),
                )
            )
        )
        .limit(limit)
    )
    p_result = await db.execute(partner_stmt)
    partners = p_result.scalars().all()

    return {
        "query": q,
        "services": [
            {"service_id": str(s.service_id), "name": s.service_name, "category": s.category}
            for s in services
        ],
        "partners": [
            {"partner_id": str(p.partner_id), "name": p.name, "city": p.city}
            for p in partners
        ],
    }


# ─── /unmatched ───────────────────────────────────────────────────────────────

@router.get("/unmatched", response_model=List[PriceItemOut], tags=["Admin"])
async def list_unmatched(
    limit: int = Query(100, le=500),
    offset: int = Query(0),
    db: AsyncSession = Depends(get_db),
):
    """Unmatched price items waiting for manual review."""
    stmt = (
        select(PriceItem)
        .where(
            PriceItem.service_id == None,
            PriceItem.is_active == True,
        )
        .offset(offset)
        .limit(limit)
        .order_by(PriceItem.created_at.desc())
    )
    result = await db.execute(stmt)
    items = result.scalars().all()

    return [
        PriceItemOut(
            item_id=i.item_id,
            service_name_raw=i.service_name_raw,
            service_id=i.service_id,
            match_score=float(i.match_score) if i.match_score else None,
            price_resident_kzt=float(i.price_resident_kzt) if i.price_resident_kzt else None,
            price_nonresident_kzt=float(i.price_nonresident_kzt) if i.price_nonresident_kzt else None,
            currency_original=i.currency_original,
            is_verified=i.is_verified,
            is_anomaly=i.is_anomaly,
            effective_date=i.effective_date,
        )
        for i in items
    ]


# ─── /match ───────────────────────────────────────────────────────────────────

@router.post("/match", response_model=MatchResponse, tags=["Admin"])
async def manual_match(
    req: MatchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Manually assign a service to an unmatched price item."""
    item = await db.get(PriceItem, req.item_id)
    if not item:
        raise HTTPException(404, "PriceItem not found")

    svc = await db.get(Service, req.service_id)
    if not svc:
        raise HTTPException(404, "Service not found")

    item.service_id = req.service_id
    item.is_verified = True
    item.match_score = 100
    if req.verification_note:
        item.verification_note = req.verification_note

    db.add(item)
    await db.commit()

    return MatchResponse(
        item_id=req.item_id,
        service_id=req.service_id,
        message="Matched and verified successfully",
    )


# ─── /documents ───────────────────────────────────────────────────────────────

@router.get("/documents", response_model=List[DocumentOut], tags=["Admin"])
async def list_documents(
    partner_id: Optional[uuid.UUID] = Query(None),
    status: Optional[ParseStatus] = Query(None),
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(PriceDocument)
    if partner_id:
        stmt = stmt.where(PriceDocument.partner_id == partner_id)
    if status:
        stmt = stmt.where(PriceDocument.parse_status == status)
    stmt = stmt.order_by(PriceDocument.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()


# ─── /dashboard ───────────────────────────────────────────────────────────────

@router.get("/dashboard", response_model=DashboardStats, tags=["Admin"])
async def dashboard(db: AsyncSession = Depends(get_db)):
    # Document counts by status
    doc_stats_result = await db.execute(
        select(PriceDocument.parse_status, func.count().label("cnt"))
        .group_by(PriceDocument.parse_status)
    )
    doc_stats = {row.parse_status: row.cnt for row in doc_stats_result}

    total_docs = sum(doc_stats.values())
    done = doc_stats.get(ParseStatus.done, 0)
    error = doc_stats.get(ParseStatus.error, 0)
    needs_review = doc_stats.get(ParseStatus.needs_review, 0)
    pending = doc_stats.get(ParseStatus.pending, 0) + doc_stats.get(ParseStatus.processing, 0)

    # Price item stats
    total_items = (await db.execute(select(func.count()).select_from(PriceItem))).scalar()
    matched = (await db.execute(
        select(func.count()).select_from(PriceItem).where(PriceItem.service_id != None)
    )).scalar()
    unmatched = total_items - matched

    norm_pct = round(matched / total_items * 100, 1) if total_items else 0.0

    total_partners = (await db.execute(select(func.count()).select_from(Partner))).scalar()

    return DashboardStats(
        total_documents=total_docs,
        done=done,
        error=error,
        needs_review=needs_review,
        pending=pending,
        total_price_items=total_items,
        matched_items=matched,
        unmatched_items=unmatched,
        normalization_pct=norm_pct,
        total_partners=total_partners,
    )
