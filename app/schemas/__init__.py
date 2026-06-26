from pydantic import BaseModel, UUID4, Field
from typing import Optional, List
from datetime import datetime, date
from app.models import FileFormat, ParseStatus, Currency
import uuid


# ── Partner ──────────────────────────────────────────────────────────────────

class PartnerCreate(BaseModel):
    name: str
    city: Optional[str] = None
    address: Optional[str] = None
    bin: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None


class PartnerOut(BaseModel):
    partner_id: UUID4
    name: str
    city: Optional[str]
    address: Optional[str]
    contact_email: Optional[str]
    contact_phone: Optional[str]
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


# ── Service ───────────────────────────────────────────────────────────────────

class ServiceOut(BaseModel):
    service_id: UUID4
    external_code: Optional[int]
    service_name: str
    category: Optional[str]
    tarification_code: Optional[str]
    synonyms: List[str] = []
    is_active: bool

    class Config:
        from_attributes = True


# ── PriceItem ─────────────────────────────────────────────────────────────────

class PriceItemOut(BaseModel):
    item_id: UUID4
    service_name_raw: str
    service_id: Optional[UUID4]
    match_score: Optional[float]
    price_resident_kzt: Optional[float]
    price_nonresident_kzt: Optional[float]
    currency_original: Currency
    is_verified: bool
    is_anomaly: bool
    effective_date: Optional[date]

    class Config:
        from_attributes = True


class PartnerWithPrice(BaseModel):
    partner_id: UUID4
    partner_name: str
    city: Optional[str]
    price_resident_kzt: Optional[float]
    price_nonresident_kzt: Optional[float]
    effective_date: Optional[date]
    is_verified: bool

    class Config:
        from_attributes = True


# ── PriceDocument ─────────────────────────────────────────────────────────────

class DocumentOut(BaseModel):
    doc_id: UUID4
    partner_id: UUID4
    file_name: str
    file_format: FileFormat
    parse_status: ParseStatus
    effective_date: Optional[date]
    parsed_at: Optional[datetime]
    parse_log: Optional[str]

    class Config:
        from_attributes = True


# ── Manual match ──────────────────────────────────────────────────────────────

class MatchRequest(BaseModel):
    item_id: UUID4
    service_id: UUID4
    verification_note: Optional[str] = None


class MatchResponse(BaseModel):
    item_id: UUID4
    service_id: UUID4
    message: str


# ── Upload response ───────────────────────────────────────────────────────────

class UploadResponse(BaseModel):
    partner_id: UUID4
    partner_name: str
    docs_queued: int
    doc_ids: List[UUID4]


# ── Dashboard ─────────────────────────────────────────────────────────────────

class DashboardStats(BaseModel):
    total_documents: int
    done: int
    error: int
    needs_review: int
    pending: int
    total_price_items: int
    matched_items: int
    unmatched_items: int
    normalization_pct: float
    total_partners: int
