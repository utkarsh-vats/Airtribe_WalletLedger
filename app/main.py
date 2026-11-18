from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.auth.router import router as auth_router
from app.clock import Clock, SystemClock
from app.config import Settings, get_settings
from app.db import create_engine
from app.errors import register_error_handlers
from app.middleware import RequestIdMiddleware
from app.wallet.router import admin_router as wallet_admin_router
from app.wallet.router import router as wallet_router


def create_app(settings: Settings | None = None, clock: Clock | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = create_engine(settings)
        try:
            yield
        finally:
            await app.state.engine.dispose()

    app = FastAPI(
        title="Airtribe_WalletLedger",
        version="0.1.0",
        # Never debug: Starlette's debug mode replaces the 500 handler with an HTML traceback.
        debug=False,
        lifespan=lifespan,
    )
    # Set here, not in lifespan, so they exist even when a test client skips lifespan.
    app.state.settings = settings
    app.state.clock = clock or SystemClock()
    app.add_middleware(RequestIdMiddleware)
    register_error_handlers(app)
    app.include_router(auth_router)
    app.include_router(wallet_router)
    app.include_router(wallet_admin_router)

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        """Liveness probe: the process is up and serving requests."""
        return {"status": "ok"}

    return app


app = create_app()
