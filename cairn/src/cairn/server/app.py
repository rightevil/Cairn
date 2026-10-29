from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from cairn import __version__
from cairn.server import db
from cairn.server.design import router as design_router
from cairn.server.collaboration import router as collaboration_router
from cairn.server.terminal import router as terminal_router
from cairn.server.routers import development, export, hints, intents, projects, settings, workspace
from cairn.server.access import OPERATOR_COOKIE, browser_session, local_browser, same_origin

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure(db.DEFAULT_DB)
    yield


app = FastAPI(
    title="Cairn",
    description="Fact-graph based collaborative exploration protocol",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(development.router)
app.include_router(collaboration_router)
app.include_router(design_router)
app.include_router(terminal_router)
app.include_router(settings.router)
app.include_router(projects.router)
app.include_router(hints.router)
app.include_router(intents.router)
app.include_router(export.router)
app.include_router(workspace.router)


@app.get("/", include_in_schema=False)
def index(request: Request):
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("workspace.css", "agent-output.js", "development.js", "design-terminal.js", "design.js", "workspace.js"):
        version = sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:16]
        html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={version}"')
    response = HTMLResponse(html, headers={"Cache-Control": "no-store"})
    if local_browser(request) and same_origin(request):
        response.set_cookie(OPERATOR_COOKIE, browser_session(request), httponly=True,
                            samesite="strict", secure=request.url.scheme == "https")
    return response


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
