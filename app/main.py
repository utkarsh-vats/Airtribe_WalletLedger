from fastapi import FastAPI

from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Airtribe_WalletLedger", version="0.1.0", debug=settings.app_env == "dev")

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        """Liveness probe: the process is up and serving requests."""
        return {"status": "ok"}

    return app


app = create_app()
