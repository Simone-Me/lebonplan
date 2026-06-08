import json
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import text

from api.database import get_db
from api.models import GeoFeatureCollection, GeoFeature, KPIs

router = APIRouter()


@router.get("/geo/arrondissements", response_model=GeoFeatureCollection)
def get_arrondissements_geojson(
    annee: int = Query(default=None, description="Année des KPIs (défaut : dernière disponible)"),
    indicateur: str = Query(default="score_global", description="Indicateur à inclure"),
    db: Session = Depends(get_db),
):
    """GeoJSON FeatureCollection des 20 arrondissements avec KPIs."""
    if annee is None:
        row = db.execute(text("SELECT MAX(annee) FROM gold.arrondissement_kpis")).fetchone()
        annee = row[0] if row and row[0] else 2024

    sql = text("""
        SELECT
            g.arrondissement,
            g.nom,
            ST_AsGeoJSON(g.geom)::json AS geometry,
            k.annee,
            k.prix_m2_median,
            k.pct_logements_sociaux,
            k.nb_logements_sociaux,
            k.score_qualite_vie,
            k.nb_espaces_verts,
            k.nb_arbres,
            k.score_air_no2,
            k.score_air_pm25,
            k.pct_fibre,
            k.nb_sanisettes,
            k.nb_chantiers_actifs,
            k.nb_anomalies,
            k.score_transports,
            k.nb_gares,
            k.nb_stations_velib,
            k.flux_multimodal,
            k.score_loisirs,
            k.nb_evenements,
            k.nb_cinemas,
            k.nb_terrasses,
            k.nb_musees,
            k.score_services,
            k.nb_ecoles,
            k.nb_colleges,
            k.nb_bibliotheques,
            k.nb_bureaux_poste,
            k.nb_ensup,
            k.score_global
        FROM gold.arrondissements_geo g
        LEFT JOIN gold.arrondissement_kpis k
            ON g.arrondissement = k.arrondissement AND k.annee = :annee
        ORDER BY g.arrondissement
    """)

    rows = db.execute(sql, {"annee": annee}).fetchall()
    features = []
    for row in rows:
        row_dict = dict(row._mapping)
        geometry = row_dict.pop("geometry")
        nom = row_dict.pop("nom", "")
        kpis = KPIs(
            arrondissement=row_dict["arrondissement"],
            annee=row_dict.get("annee") or annee,
            **{k: v for k, v in row_dict.items() if k != "arrondissement"},
        )
        kpis_dict = kpis.model_dump()
        kpis_dict["nom"] = nom
        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": kpis_dict,
        })

    return {"type": "FeatureCollection", "features": features}
