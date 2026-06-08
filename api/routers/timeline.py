from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text

from api.database import get_db
from api.models import TimelineResponse, TimelinePoint

router = APIRouter()


@router.get("/timeline/{arrondissement}", response_model=TimelineResponse)
def get_timeline(arrondissement: int, db: Session = Depends(get_db)):
    if not (1 <= arrondissement <= 20):
        raise HTTPException(status_code=400, detail="Arrondissement doit être entre 1 et 20")

    sql = text("""
        SELECT annee, prix_m2_median, score_qualite_vie, score_transports,
               score_loisirs, score_services, score_global, pct_logements_sociaux
        FROM gold.arrondissement_kpis
        WHERE arrondissement = :arr
        ORDER BY annee
    """)
    rows = db.execute(sql, {"arr": arrondissement}).fetchall()

    points = [TimelinePoint(**dict(row._mapping)) for row in rows]
    return TimelineResponse(arrondissement=arrondissement, points=points)
