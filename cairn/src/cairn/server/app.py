from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cairn import __version__
from cairn.server import db
from cairn.server.middleware import RequestIdMiddleware
from cairn.server.routers import (
    context,
    documents,
    export,
    findings,
    goals,
    health,
    hints,
    metrics,
    projects,
    questions,
    recall,
    sessions,
    settings,
    steps,
    tool_guard,
    usage,
)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure(db.DEFAULT_DB)
    yield


app = FastAPI(
    title="Cairn",
    description="Fact-Goal-Step graph based collaborative exploration protocol",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(metrics.router)
app.include_router(settings.router)
app.include_router(projects.router)
app.include_router(hints.router)
app.include_router(goals.router)
app.include_router(steps.router)
app.include_router(sessions.router)
app.include_router(questions.router)
app.include_router(context.router)
app.include_router(documents.router)
app.include_router(recall.router)
app.include_router(findings.router)
app.include_router(usage.router)
app.include_router(export.router)
app.include_router(tool_guard.router)

app.add_middleware(RequestIdMiddleware)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
