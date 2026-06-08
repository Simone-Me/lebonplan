from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import text

from api.database import get_db
from api.models import KPIs

router = APIRouter()


def _fetch_kpis(db: Session, arrondissement: int, annee: int) -> dict | None:
    sql = text("""
        SELECT * FROM gold.arrondissement_kpis
        WHERE arrondissement = :arr AND annee = :annee
    """)
    row = db.execute(sql, {"arr": arrondissement, "annee": annee}).fetchone()
    return dict(row._mapping) if row else None


@router.get("/kpis/{arrondissement}", response_model=KPIs)
def get_kpis(
    arrondissement: int,
    annee: int = Query(default=None),
    db: Session = Depends(get_db),
):
    if not (1 <= arrondissement <= 20):
        raise HTTPException(status_code=400, detail="Arrondissement doit être entre 1 et 20")

    if annee is None:
        row = db.execute(text("SELECT MAX(annee) FROM gold.arrondissement_kpis")).fetchone()
        annee = row[0] if row and row[0] else 2024

    data = _fetch_kpis(db, arrondissement, annee)
    if not data:
        raise HTTPException(status_code=404, detail=f"Aucune donnée pour arrondissement={arrondissement} annee={annee}")

    return KPIs(**data)
