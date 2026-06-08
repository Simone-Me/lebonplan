"""
Urban Data Explorer — API FastAPI
Expose les KPIs Gold (PostgreSQL) au frontend.
"""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routers import geo, kpis, timeline, compare

app = FastAPI(
    title="Urban Data Explorer API",
    description="KPIs logement & qualité de vie par arrondissement parisien",
    version="0.4.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(geo.router,      prefix="/api", tags=["Géo"])
app.include_router(kpis.router,     prefix="/api", tags=["KPIs"])
app.include_router(timeline.router, prefix="/api", tags=["Timeline"])
app.include_router(compare.router,  prefix="/api", tags=["Comparaison"])


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "trace": traceback.format_exc()},
    )


@app.get("/api/health", tags=["Santé"])
def health():
    return {"status": "ok"}
