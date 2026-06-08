from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import text

from api.database import get_db
from api.models import KPIs, CompareResponse

router = APIRouter()


@router.get("/compare", response_model=CompareResponse)
def compare_arrondissements(
    arr1: int = Query(..., ge=1, le=20),
    arr2: int = Query(..., ge=1, le=20),
    annee: int = Query(default=None),
    db: Session = Depends(get_db),
):
    if arr1 == arr2:
        raise HTTPException(status_code=400, detail="Choisissez deux arrondissements différents")

    if annee is None:
        row = db.execute(text("SELECT MAX(annee) FROM gold.arrondissement_kpis")).fetchone()
        annee = row[0] if row and row[0] else 2024

    sql = text("""
        SELECT * FROM gold.arrondissement_kpis
        WHERE arrondissement = :arr AND annee = :annee
    """)

    r1 = db.execute(sql, {"arr": arr1, "annee": annee}).fetchone()
    r2 = db.execute(sql, {"arr": arr2, "annee": annee}).fetchone()

    if not r1:
        raise HTTPException(status_code=404, detail=f"Pas de données pour arrondissement {arr1}")
    if not r2:
        raise HTTPException(status_code=404, detail=f"Pas de données pour arrondissement {arr2}")

    return CompareResponse(
        arrondissement_1=KPIs(**dict(r1._mapping)),
        arrondissement_2=KPIs(**dict(r2._mapping)),
    )
