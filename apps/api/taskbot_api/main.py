from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from taskbot_api.config import Settings, get_settings
from taskbot_api.db import build_engine, database_is_ready, session_factory
from taskbot_api.routes import dev_router, router
from taskbot_api.task_routes import router as task_router
from taskbot_api.notification_routes import router as notification_router


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    database: str


def create_app(settings: Settings | None = None, *, engine_override: AsyncEngine | None = None) -> FastAPI:
    config = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        engine = engine_override if engine_override is not None else build_engine(config)
        application.state.db_engine = engine
        application.state.session_factory = session_factory(engine) if engine is not None else None
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()

    application = FastAPI(title="Taskbot API", version="0.1.0", lifespan=lifespan)
    application.state.db_engine = None
    application.state.session_factory = None
    application.state.settings = config
    if config.web_origin:
        application.add_middleware(CORSMiddleware, allow_origins=[config.web_origin],
                                   allow_credentials=True, allow_methods=["GET", "POST", "PATCH", "PUT"],
                                   allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key"])
    application.include_router(router)
    if config.dev_login_enabled:
        application.include_router(dev_router)
    application.include_router(task_router)
    application.include_router(notification_router)

    @application.get("/api/v1/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @application.get(
        "/api/v1/health/ready",
        response_model=ReadinessResponse,
        responses={503: {"model": ReadinessResponse}},
    )
    async def ready(request: Request) -> ReadinessResponse | JSONResponse:
        engine: AsyncEngine | None = request.app.state.db_engine
        if engine is None:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "database": "not_configured"},
            )
        if not await database_is_ready(engine):
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "database": "unavailable"},
            )
        return ReadinessResponse(status="ready", database="available")

    return application


app = create_app()

