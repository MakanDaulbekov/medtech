import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db
from app.api.routes import router
from app.config import get_settings

settings = get_settings()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing database…")
    await init_db()

    # Load normalizer index from DB
    from app.database import AsyncSessionLocal
    from app.models import Service
    from sqlalchemy import select
    from app.normalizer import build_index

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Service).where(Service.is_active == True)
        )
        services = result.scalars().all()
        svc_dicts = [
            {
                "service_id": str(s.service_id),
                "service_name": s.service_name,
                "synonyms": s.synonyms or [],
            }
            for s in services
        ]
        if svc_dicts:
            build_index(svc_dicts)
            logger.info(f"Normalizer loaded {len(svc_dicts)} services")
        else:
            logger.warning("No services in DB — run the seeder first!")

    yield  # app runs

    # Shutdown (nothing to clean up)


app = FastAPI(
    title="MedArchive API",
    description="Medical price archive parser and search API",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok"}
