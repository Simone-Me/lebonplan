from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import text

from api.database import get_db
from api.models import KPIs

router = APIRouter()


def _resolve_available_year(db: Session, table_name: str, requested_year: int | None, fallback_year: int = 2024) -> int:
    if requested_year is None:
        row = db.execute(text(f"SELECT MAX(annee) FROM {table_name}")).fetchone()
        return row[0] if row and row[0] else fallback_year

    row = db.execute(
        text(f"SELECT MAX(annee) FROM {table_name} WHERE annee <= :annee"),
        {"annee": requested_year},
    ).fetchone()
    if row and row[0]:
        return row[0]

    row = db.execute(text(f"SELECT MIN(annee) FROM {table_name}")).fetchone()
    return row[0] if row and row[0] else requested_year


def _fetch_kpis(db: Session, arrondissement: int, annee: int) -> dict | None:
    sql = text("""
        SELECT * FROM gold.arrondissement_kpis
        WHERE arrondissement = :arr AND annee = :annee
    """)
    row = db.execute(sql, {"arr": arrondissement, "annee": annee}).fetchone()
    return dict(row._mapping) if row else None


def _fetch_quartier_kpis(db: Session, quartier_id: str, annee: int) -> dict | None:
    sql = text("""
        SELECT * FROM gold.quartier_kpis
        WHERE quartier_id = :quartier_id AND annee = :annee
    """)
    row = db.execute(sql, {"quartier_id": quartier_id, "annee": annee}).fetchone()
    return dict(row._mapping) if row else None


def _fetch_iris_kpis(db: Session, iris_id: str, annee: int) -> dict | None:
    sql = text("""
        SELECT * FROM gold.iris_kpis
        WHERE iris_id = :iris_id AND annee = :annee
    """)
    row = db.execute(sql, {"iris_id": iris_id, "annee": annee}).fetchone()
    return dict(row._mapping) if row else None


@router.get("/kpis/{arrondissement}", response_model=KPIs)
def get_kpis(
    arrondissement: int,
    annee: int = Query(default=None),
    db: Session = Depends(get_db),
):
    if not (1 <= arrondissement <= 20):
        raise HTTPException(status_code=400, detail="Arrondissement doit être entre 1 et 20")

    annee = _resolve_available_year(db, "gold.arrondissement_kpis", annee)

    data = _fetch_kpis(db, arrondissement, annee)
    if not data:
        raise HTTPException(status_code=404, detail=f"Aucune donnée pour arrondissement={arrondissement} annee={annee}")

    return KPIs(**data)


@router.get("/kpis/quartier/{quartier_id}", response_model=KPIs)
def get_quartier_kpis(
    quartier_id: str,
    annee: int = Query(default=None),
    db: Session = Depends(get_db),
):
    annee = _resolve_available_year(db, "gold.quartier_kpis", annee)

    data = _fetch_quartier_kpis(db, quartier_id, annee)
    if not data:
        raise HTTPException(status_code=404, detail=f"Aucune donnée pour quartier_id={quartier_id} annee={annee}")

    return KPIs(**data)


@router.get("/kpis/iris/{iris_id}", response_model=KPIs)
def get_iris_kpis(
    iris_id: str,
    annee: int = Query(default=None),
    db: Session = Depends(get_db),
):
    annee = _resolve_available_year(db, "gold.iris_kpis", annee)

    data = _fetch_iris_kpis(db, iris_id, annee)
    if not data:
        raise HTTPException(status_code=404, detail=f"Aucune donnée pour iris_id={iris_id} annee={annee}")

    return KPIs(**data)
