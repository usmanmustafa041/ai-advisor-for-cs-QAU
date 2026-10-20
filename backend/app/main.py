import asyncio
import logging
import re

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings


log = logging.getLogger("app.main")
_pending: set[asyncio.Task] = set()  # keeps fire-and-forget tasks alive until done


async def _notify_chatbot(url: str, authorization: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers={"Authorization": authorization})
        if resp.status_code >= 400:
            log.warning("chatbot sync request failed: HTTP %s %s", resp.status_code, resp.text[:200])
    except httpx.HTTPError as exc:
        log.warning("chatbot sync request failed: %s", exc)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="2.0.0",
        debug=settings.app_debug,
        description=(
            "Professional QAU CS Academic Advisor - RAG-Powered Intelligent Chatbot. "
            "Source-grounded responses about courses, programmes, and academic guidance."
        ),
    )

    # CORS Configuration
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.exception_handler(IntegrityError)
    async def integrity_conflict(request, exc):
        # Duplicate codes/emails or broken references are user input problems, not server faults.
        return JSONResponse(
            {"detail": "This record conflicts with existing data (duplicate or invalid reference)"},
            status_code=409,
        )

    chatbot_sections = re.compile(
        rf"^{re.escape(settings.api_v1_prefix)}/admin/(courses|prerequisites|fees|policies|knowledge|cms)(/|$)"
    )

    @app.middleware("http")
    async def sync_chatbot(request, call_next):
        response = await call_next(request)
        if (
            settings.chatbot_sync_url
            and request.method in ("POST", "PUT", "PATCH", "DELETE")
            and response.status_code < 400
            and chatbot_sections.match(request.url.path)
            and request.headers.get("authorization")
        ):
            # Fire and forget: the engine re-reads the export with this admin's session.
            task = asyncio.create_task(_notify_chatbot(settings.chatbot_sync_url, request.headers["authorization"]))
            _pending.add(task)
            task.add_done_callback(_pending.discard)
        return response

    @app.middleware("http")
    async def chat_timeout(request, call_next):
        import asyncio

        if request.url.path != settings.api_v1_prefix + "/chat":
            return await call_next(request)
        if (
            request.headers.get("content-length", "0").isdigit()
            and int(request.headers.get("content-length", "0")) > 65536
        ):
            return JSONResponse({"detail": "Chat request is too large"}, status_code=413)
        try:
            return await asyncio.wait_for(call_next(request), settings.chat_request_timeout_seconds)
        except TimeoutError:
            return JSONResponse(
                {"detail": "The advisor timed out. Please try again."}, status_code=504
            )

    # Import intelligent chat router with LLM and RAG
    from app.api.advisor import router as chat_router

    app.include_router(chat_router, prefix=settings.api_v1_prefix)

    # Import authentication router
    from app.api.auth import router as auth_router

    app.include_router(auth_router, prefix=settings.api_v1_prefix, tags=["auth"])

    # Import programs router
    # The database-backed academic router is the single programme authority.
    # The old programs router contained a conflicting hard-coded study plan.

    # Import admin router
    from app.api.admin import router as admin_router

    app.include_router(admin_router, prefix=settings.api_v1_prefix)

    # Import CMS router (Knowledge Base Management)
    from app.api.cms import router as cms_router

    app.include_router(cms_router, prefix=settings.api_v1_prefix)

    # Import academic data router
    from app.api.academic import router as academic_router

    app.include_router(academic_router, prefix=settings.api_v1_prefix)

    # Import query history router
    from app.api.history import router as history_router

    app.include_router(history_router, prefix=settings.api_v1_prefix)

    # Import timetable router
    from app.api.timetable import router as timetable_router

    app.include_router(timetable_router, prefix=settings.api_v1_prefix)

    from app.api import entities, feedback, health, knowledge_review, nlp, rag, rules

    for module in (entities, feedback, health, knowledge_review, nlp, rag, rules):
        app.include_router(module.router, prefix=settings.api_v1_prefix)

    @app.get("/", include_in_schema=False)
    def root() -> dict:
        return {
            "service": "QAU CS Academic Advisor",
            "version": "2.0.0",
            "status": "operational",
            "type": "RAG-Powered Intelligent Chatbot",
            "docs": "/docs",
        }

    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok", "service": "qau-cs-academic-advisor-api"}

    return app


app = create_app()
