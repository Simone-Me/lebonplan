from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text

from api.database import get_db
from api.models import TimelineResponse, TimelinePoint, QuartierTimelineResponse

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


@router.get("/timeline/quartier/{quartier_id}", response_model=QuartierTimelineResponse)
def get_quartier_timeline(quartier_id: str, db: Session = Depends(get_db)):
    meta_sql = text("""
        SELECT quartier_id, quartier_code, arrondissement, nom
        FROM gold.quartiers_geo
        WHERE quartier_id = :quartier_id
    """)
    meta = db.execute(meta_sql, {"quartier_id": quartier_id}).fetchone()
    if not meta:
        raise HTTPException(status_code=404, detail="Quartier introuvable")

    sql = text("""
        SELECT annee, prix_m2_median, score_qualite_vie, score_transports,
               score_loisirs, score_services, score_global, pct_logements_sociaux
        FROM gold.quartier_kpis
        WHERE quartier_id = :quartier_id
        ORDER BY annee
    """)
    rows = db.execute(sql, {"quartier_id": quartier_id}).fetchall()
    points = [TimelinePoint(**dict(row._mapping)) for row in rows]

    meta_dict = dict(meta._mapping)
    return QuartierTimelineResponse(points=points, **meta_dict)
