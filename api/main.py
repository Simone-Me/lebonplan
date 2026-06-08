"""
Urban Data Explorer — API FastAPI
Expose les KPIs Gold (PostgreSQL) au frontend.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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


@app.get("/api/health", tags=["Santé"])
def health():
    return {"status": "ok"}
