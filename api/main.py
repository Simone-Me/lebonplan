"""
Urban Data Explorer — API FastAPI
Expose les KPIs Gold (PostgreSQL) au frontend.
"""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))

from fastapi import FastAPI, Request, Response
from fastapi import Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routers import auth, geo, kpis, timeline, compare, streaming
from api.security import get_cors_origins, require_auth

app = FastAPI(
    title="Urban Data Explorer API",
    description="KPIs logement & qualité de vie par arrondissement parisien",
    version="0.4.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(auth.router,     prefix="/api")
app.include_router(geo.router,      prefix="/api", tags=["Géo"], dependencies=[Depends(require_auth)])
app.include_router(kpis.router,     prefix="/api", tags=["KPIs"], dependencies=[Depends(require_auth)])
app.include_router(timeline.router, prefix="/api", tags=["Timeline"], dependencies=[Depends(require_auth)])
app.include_router(compare.router,   prefix="/api", tags=["Comparaison"],  dependencies=[Depends(require_auth)])
app.include_router(streaming.router, prefix="/api", tags=["Streaming"],   dependencies=[Depends(require_auth)])


@app.get("/", tags=["Accueil"])
def root():
    return {
        "name": "Urban Data Explorer API",
        "status": "ok",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/api/health",
    }


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "trace": traceback.format_exc()},
    )


@app.get("/api/health", tags=["Santé"])
def health():
    return {"status": "ok"}
