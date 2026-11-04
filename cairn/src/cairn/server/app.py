from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cairn import __version__
from cairn.server import db
from cairn.server.routers import (
    context,
    export,
    goals,
    hints,
    projects,
    questions,
    sessions,
    settings,
    steps,
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

app.include_router(settings.router)
app.include_router(projects.router)
app.include_router(hints.router)
app.include_router(goals.router)
app.include_router(steps.router)
app.include_router(sessions.router)
app.include_router(questions.router)
app.include_router(context.router)
app.include_router(export.router)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
