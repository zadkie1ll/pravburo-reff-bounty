from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response, status
from fastapi.staticfiles import StaticFiles
from pravburo_ref_common.database import close_database, database_is_ready
from starlette.middleware.sessions import SessionMiddleware

from src.config import get_settings
from src.routes import router

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await close_database()


app = FastAPI(title="pravburo-reff-bounty", debug=settings.app_debug, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=settings.app_env == "production",
)
app.mount(
    "/admin/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="bounty_static",
)
app.include_router(router)


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok", "service": "pravburo-reff-bounty"}


@app.get("/health/ready")
async def ready(response: Response) -> dict[str, str]:
    available = await database_is_ready()
    if not available:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if available else "unavailable",
        "service": "pravburo-reff-bounty",
    }
