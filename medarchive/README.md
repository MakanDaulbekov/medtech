# MedArchive — Parser & API

**Hackathon solution for MedPartners / MedArchive case.**

## Architecture

```
ZIP upload → FastAPI → Celery task queue (Redis)
                ↓
         Parser pipeline
          ├─ PDF (text)   → pdfplumber tables
          ├─ PDF (scan)   → Tesseract OCR
          ├─ DOCX         → python-docx (tracked changes aware)
          └─ XLSX         → openpyxl (multi-sheet, auto header detection)
                ↓
         Validator        (price checks, dedup, anomaly detection)
                ↓
         Normalizer       (RapidFuzz + sentence-transformers embeddings)
                ↓
         PostgreSQL       (Partners → PriceDocuments → PriceItems → Services)
```

## Quick Start

### 1. Prerequisites
```bash
docker compose up -d db redis
```

### 2. Run the API (dev)
```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

### 3. Seed the service directory
```bash
python scripts/seed_services.py Справочник_услуг.xlsx
```

### 4. Start the Celery worker
```bash
celery -A app.tasks.celery_app worker --loglevel=info
```

### Full Docker stack
```bash
docker compose up --build
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/upload` | Upload ZIP archive for a partner |
| GET | `/api/v1/services` | List services (filter by category, search) |
| GET | `/api/v1/services/{id}/partners` | Partners offering a service with prices |
| GET | `/api/v1/partners` | List partners (filter by city, status) |
| GET | `/api/v1/partners/{id}/services` | All prices for a partner |
| GET | `/api/v1/search?q=` | Full-text search services + partners |
| GET | `/api/v1/unmatched` | Unmatched items queue for operators |
| POST | `/api/v1/match` | Manual service assignment |
| GET | `/api/v1/documents` | Document processing status |
| GET | `/api/v1/dashboard` | Processing metrics |

**Swagger UI:** http://localhost:8000/docs

---

## Normalization thresholds (configurable via .env)

```
AUTO_MATCH_THRESHOLD=85    # rapidfuzz score ≥ 85 → auto-matched
REVIEW_THRESHOLD=60        # score 60-84 → matched but needs review
                           # score < 60 → goes to /unmatched queue
PRICE_ANOMALY_PCT=50       # flag if price changed > 50% vs previous
```

---

## Validation rules (from spec §4.4)

| Rule | Action |
|------|--------|
| Price > 0 and numeric | else → needs_review |
| NonResident price ≥ Resident | else → warning |
| Service name not empty | else → skip row |
| Effective date not in future | else → warning |
| Duplicate (partner+service+date) | archive old, keep new |
| Price change > 50% | anomaly flag, manual confirm |
| Non-KZT currency | convert at stub rate (hook for real API) |

---

## Project structure

```
medarchive/
├── app/
│   ├── main.py             # FastAPI app + lifespan startup
│   ├── config.py           # Settings (pydantic-settings)
│   ├── database.py         # Async SQLAlchemy engine + session
│   ├── models/             # SQLAlchemy ORM models
│   ├── schemas/            # Pydantic request/response schemas
│   ├── api/
│   │   └── routes.py       # All REST endpoints
│   ├── parsers/
│   │   ├── pdf_parser.py   # PDF text + OCR
│   │   ├── docx_parser.py  # DOCX (tracked changes)
│   │   ├── xlsx_parser.py  # XLSX multi-sheet
│   │   └── validator.py    # Price validation rules
│   ├── normalizer/
│   │   └── __init__.py     # RapidFuzz + embeddings
│   └── tasks/
│       └── __init__.py     # Celery task definitions
├── scripts/
│   └── seed_services.py    # Load xlsx service directory to DB
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```
