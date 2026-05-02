"""
MeatHead — Content Generation API.

Wires the AI engine (services/ai_service.py) to the dashboard:
    POST   /v1/content/generate     -> generate a fresh draft
    POST   /v1/content/improve      -> rewrite an existing draft with an instruction
    GET    /v1/content/drafts       -> list saved drafts
    DELETE /v1/content/drafts/{id}  -> discard a draft
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from server.db.engine import get_db
from server.db.models import ContentDraft

logger = logging.getLogger("meathead.api.content")

router = APIRouter()

# Single-user MVP: every draft is owned by the internal user.
INTERNAL_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_engine = None


def _get_engine():
    """Lazy singleton — avoids initializing Groq client at import time."""
    global _engine
    if _engine is None:
        from server.services.ai_service import MeatHeadEngine

        _engine = MeatHeadEngine()
    return _engine


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    platform: str = Field(default="reddit", max_length=32)
    product_context: str = Field(default="", max_length=2000)
    topic: str = Field(default="", max_length=2000)
    tone: str = Field(default="casual", max_length=32)


class ImproveRequest(BaseModel):
    original_text: str = Field(..., min_length=1, max_length=10000)
    instruction: str = Field(..., min_length=1, max_length=1000)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/generate")
async def generate(req: GenerateRequest):
    """Generate a new draft and persist it as a ContentDraft row."""
    engine = _get_engine()
    if not engine.configured:
        raise HTTPException(
            status_code=503,
            detail="MeatHead AI engine not configured. Set GROQ_API_KEY.",
        )

    try:
        result = await engine.generate_content(
            platform=req.platform,
            product=req.product_context,
            topic=req.topic,
            tone=req.tone,
        )
    except Exception as exc:
        logger.exception("Content generation failed")
        raise HTTPException(status_code=500, detail=f"Generation failed: {exc}")

    body = result.get("body") or ""
    raw = result.get("raw_body") or body
    title = result.get("title")

    async with get_db() as db:
        draft = ContentDraft(
            user_id=INTERNAL_USER_ID,
            platform=req.platform,
            title=title,
            body=body,
            raw_body=raw,
            product_context=req.product_context or None,
            topic=req.topic or None,
            tone=req.tone or None,
            status="draft",
        )
        db.add(draft)
        await db.flush()
        draft_id = str(draft.id)

    return {
        "draft_id": draft_id,
        "platform": req.platform,
        "title": title,
        "draft": body,
        "raw_draft": raw,
    }


@router.post("/improve")
async def improve(req: ImproveRequest):
    """Rewrite a draft according to a user instruction."""
    engine = _get_engine()
    if not engine.configured:
        raise HTTPException(status_code=503, detail="MeatHead AI engine not configured.")

    try:
        improved = await engine.improve_draft(req.original_text, req.instruction)
    except Exception as exc:
        logger.exception("Improve draft failed")
        raise HTTPException(status_code=500, detail=f"Improve failed: {exc}")

    return {"draft": improved}


@router.get("/drafts")
async def list_drafts(limit: int = 20, platform: Optional[str] = None):
    """Return the most recent drafts, optionally filtered by platform."""
    limit = max(1, min(limit, 100))

    async with get_db() as db:
        stmt = select(ContentDraft).order_by(ContentDraft.created_at.desc()).limit(limit)
        if platform:
            stmt = stmt.where(ContentDraft.platform == platform)
        result = await db.execute(stmt)
        drafts = result.scalars().all()

    return {
        "drafts": [
            {
                "id": str(d.id),
                "platform": d.platform,
                "title": d.title,
                "body": d.body,
                "topic": d.topic,
                "tone": d.tone,
                "status": d.status,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in drafts
        ]
    }


@router.delete("/drafts/{draft_id}")
async def delete_draft(draft_id: str):
    """Hard-delete a draft."""
    try:
        target_id = uuid.UUID(draft_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid draft id")

    async with get_db() as db:
        result = await db.execute(select(ContentDraft).where(ContentDraft.id == target_id))
        draft = result.scalar_one_or_none()
        if not draft:
            raise HTTPException(status_code=404, detail="Draft not found")
        await db.delete(draft)

    return {"deleted": draft_id}
