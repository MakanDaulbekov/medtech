"""
Normalizer
1. Exact match against service names + synonyms
2. RapidFuzz token_sort_ratio  → auto-match if score ≥ threshold
3. Sentence-transformers cosine similarity  → second pass
Returns (service_id | None, score)
"""
import logging
from typing import List, Tuple, Optional
from uuid import UUID

from rapidfuzz import fuzz, process as rfprocess

logger = logging.getLogger(__name__)

# We lazy-load the embedding model (heavy, ~500MB)
_embedding_model = None
_service_embeddings = None   # np.ndarray
_service_index: List[dict] = []  # [{service_id, service_name}, ...]


def _get_model():
    global _embedding_model
    if _embedding_model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _embedding_model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
            logger.info("Embedding model loaded")
        except Exception as e:
            logger.warning(f"Could not load embedding model: {e}")
    return _embedding_model


# ─────────────────────────────────────────────────────────────────────────────
# Build index from DB services
# ─────────────────────────────────────────────────────────────────────────────

def build_index(services: List[dict]):
    """
    services: list of dicts with keys: service_id, service_name, synonyms (list[str])
    Call this once at startup (or after seeding).
    """
    global _service_index, _service_embeddings

    _service_index = []
    texts = []

    for svc in services:
        # Add main name
        _service_index.append({
            "service_id": str(svc["service_id"]),
            "service_name": svc["service_name"],
            "text": svc["service_name"].lower(),
        })
        texts.append(svc["service_name"])

        # Add synonyms
        for syn in (svc.get("synonyms") or []):
            _service_index.append({
                "service_id": str(svc["service_id"]),
                "service_name": svc["service_name"],
                "text": syn.lower(),
            })
            texts.append(syn)

    # Optionally build embeddings
    model = _get_model()
    if model and texts:
        try:
            import numpy as np
            embs = model.encode(texts, batch_size=128, show_progress_bar=False, normalize_embeddings=True)
            _service_embeddings = embs
            logger.info(f"Built embeddings for {len(texts)} service entries")
        except Exception as e:
            logger.warning(f"Embedding build failed: {e}")
            _service_embeddings = None

    logger.info(f"Normalizer index: {len(_service_index)} entries for {len(services)} services")


# ─────────────────────────────────────────────────────────────────────────────
# Match a single service name
# ─────────────────────────────────────────────────────────────────────────────

def match_service(
    raw_name: str,
    auto_threshold: float = 85.0,
    review_threshold: float = 60.0,
) -> Tuple[Optional[str], float]:
    """
    Returns (service_id | None, score 0-100).
    - score >= auto_threshold  → confident match
    - review_threshold <= score < auto_threshold → needs review (return match but flag)
    - score < review_threshold → unmatched (return None)
    """
    if not _service_index:
        return None, 0.0

    query = raw_name.strip().lower()

    # ── 1. Exact match ────────────────────────────────────────────────────────
    for entry in _service_index:
        if entry["text"] == query:
            return entry["service_id"], 100.0

    # ── 2. RapidFuzz ─────────────────────────────────────────────────────────
    texts = [e["text"] for e in _service_index]
    result = rfprocess.extractOne(
        query,
        texts,
        scorer=fuzz.token_sort_ratio,
        score_cutoff=0,
    )

    if result:
        best_text, rf_score, idx = result
        if rf_score >= review_threshold:
            service_id = _service_index[idx]["service_id"]
            # ── 3. Optional embedding reranking for borderline cases ───────────
            if _service_embeddings is not None and rf_score < auto_threshold:
                service_id, rf_score = _embedding_rerank(query, service_id, rf_score)
            return service_id, float(rf_score)

    # ── Embedding-only fallback ───────────────────────────────────────────────
    if _service_embeddings is not None:
        service_id, emb_score = _embedding_search(query)
        # Map cosine similarity [0,1] → [0,100]
        scaled = emb_score * 100
        if scaled >= review_threshold:
            return service_id, scaled

    return None, 0.0


def _embedding_search(query: str) -> Tuple[Optional[str], float]:
    """Find best match by cosine similarity."""
    import numpy as np
    model = _get_model()
    if model is None or _service_embeddings is None:
        return None, 0.0

    q_emb = model.encode([query], normalize_embeddings=True)[0]
    sims = _service_embeddings @ q_emb
    best_idx = int(np.argmax(sims))
    return _service_index[best_idx]["service_id"], float(sims[best_idx])


def _embedding_rerank(query: str, rf_service_id: str, rf_score: float) -> Tuple[str, float]:
    """Use embeddings to verify or override RapidFuzz choice."""
    emb_id, emb_cos = _embedding_search(query)
    emb_score = emb_cos * 100

    if emb_id and emb_score > rf_score + 5:
        return emb_id, emb_score
    return rf_service_id, rf_score


# ─────────────────────────────────────────────────────────────────────────────
# Batch match
# ─────────────────────────────────────────────────────────────────────────────

def batch_match(
    raw_names: List[str],
    auto_threshold: float = 85.0,
    review_threshold: float = 60.0,
) -> List[Tuple[Optional[str], float]]:
    return [match_service(n, auto_threshold, review_threshold) for n in raw_names]
