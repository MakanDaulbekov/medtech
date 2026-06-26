import uuid
from datetime import datetime, date
from sqlalchemy import (
    Column, String, Boolean, DateTime, Date, Text,
    Numeric, ForeignKey, Enum as SAEnum, JSON, Integer
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship
import enum


class Base(DeclarativeBase):
    pass


class FileFormat(str, enum.Enum):
    pdf = "pdf"
    scan_pdf = "scan_pdf"
    docx = "docx"
    xlsx = "xlsx"


class ParseStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    done = "done"
    error = "error"
    needs_review = "needs_review"


class Currency(str, enum.Enum):
    KZT = "KZT"
    USD = "USD"
    RUB = "RUB"


class Partner(Base):
    __tablename__ = "partners"

    partner_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(500), nullable=False, index=True)
    city = Column(String(200))
    address = Column(String(500))
    bin = Column(String(12), unique=True, nullable=True)
    contact_email = Column(String(200))
    contact_phone = Column(String(50))
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    documents = relationship("PriceDocument", back_populates="partner")
    price_items = relationship("PriceItem", back_populates="partner")


class PriceDocument(Base):
    __tablename__ = "price_documents"

    doc_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    partner_id = Column(UUID(as_uuid=True), ForeignKey("partners.partner_id"), nullable=False)
    file_name = Column(String(500), nullable=False)
    file_path = Column(String(1000), nullable=False)
    file_format = Column(SAEnum(FileFormat), nullable=False)
    effective_date = Column(Date, nullable=True)
    parsed_at = Column(DateTime, nullable=True)
    parse_status = Column(SAEnum(ParseStatus), default=ParseStatus.pending, nullable=False)
    parse_log = Column(Text, nullable=True)
    raw_content = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    partner = relationship("Partner", back_populates="documents")
    price_items = relationship("PriceItem", back_populates="document")


class Service(Base):
    """Canonical service directory loaded from xlsx."""
    __tablename__ = "services"

    service_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    external_code = Column(Integer, nullable=True)          # Code column from xlsx
    service_name = Column(String(500), nullable=False, index=True)
    category = Column(String(200), nullable=True, index=True)   # Специальность
    tarification_code = Column(String(50), nullable=True)   # TarificatrCode
    synonyms = Column(JSON, default=list)                   # for fuzzy matching
    icd_code = Column(String(20), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    price_items = relationship("PriceItem", back_populates="service")


class PriceItem(Base):
    __tablename__ = "price_items"

    item_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    doc_id = Column(UUID(as_uuid=True), ForeignKey("price_documents.doc_id"), nullable=False)
    partner_id = Column(UUID(as_uuid=True), ForeignKey("partners.partner_id"), nullable=False)

    # Raw extracted data
    service_name_raw = Column(String(500), nullable=False)
    service_code_source = Column(String(100), nullable=True)

    # Normalized link (nullable until matched)
    service_id = Column(UUID(as_uuid=True), ForeignKey("services.service_id"), nullable=True)
    match_score = Column(Numeric(5, 2), nullable=True)   # rapidfuzz score

    # Prices
    price_resident_kzt = Column(Numeric(12, 2), nullable=True)
    price_nonresident_kzt = Column(Numeric(12, 2), nullable=True)
    price_original = Column(Numeric(12, 2), nullable=True)
    currency_original = Column(SAEnum(Currency), default=Currency.KZT)

    # Verification
    is_verified = Column(Boolean, default=False, nullable=False)
    verification_note = Column(String(500), nullable=True)
    is_anomaly = Column(Boolean, default=False, nullable=False)

    effective_date = Column(Date, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    document = relationship("PriceDocument", back_populates="price_items")
    partner = relationship("Partner", back_populates="price_items")
    service = relationship("Service", back_populates="price_items")
