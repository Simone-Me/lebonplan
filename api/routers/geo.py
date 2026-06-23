import traceback
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import text

from api.database import get_db
from api.models import KPIs
from api.mongo import get_mongo_db

router = APIRouter()

# Mapping type → (collection Silver, champs à renvoyer)
_POINT_COLLECTIONS = {
    "gares":         ("silver_gares",        ["nom", "nomlong", "mode_transport", "ligne", "arrondissement"]),
    "velib":         ("silver_velib",         ["nom", "capacity", "arrondissement"]),
    "espaces_verts": ("silver_espaces_verts", ["nom", "arrondissement"]),
    "musees":        ("silver_musees",        ["nom", "arrondissement"]),
    "cinemas":       ("silver_cinemas",       ["nom", "arrondissement"]),
    "bibliotheques": ("silver_bibliotheques", ["nom", "arrondissement"]),
}


@router.get("/geo/points")
def get_points_geojson(
    type: str = Query(..., description="Type de point : gares|velib|espaces_verts|musees|cinemas|bibliotheques"),
    arrondissement: int = Query(default=None, description="Filtrer par arrondissement (1-20)"),
):
    """GeoJSON FeatureCollection de points depuis la couche Silver MongoDB."""
    if type not in _POINT_COLLECTIONS:
        return JSONResponse(
            status_code=400,
            content={"error": f"Type inconnu. Valeurs acceptées : {', '.join(_POINT_COLLECTIONS)}"},
        )

    collection_name, fields = _POINT_COLLECTIONS[type]
    try:
        db = get_mongo_db()
        coll = db[collection_name]
        query: dict = {"location": {"$exists": True, "$ne": None}}
        if arrondissement and 1 <= arrondissement <= 20:
            query["arrondissement"] = arrondissement

        projection = {f: 1 for f in fields}
        projection["location"] = 1

        features = []
        for doc in coll.find(query, projection).limit(2000):
            loc = doc.get("location")
            if not isinstance(loc, dict) or loc.get("type") != "Point":
                continue
            coords = loc.get("coordinates")
            if not coords or len(coords) < 2:
                continue
            label = doc.get("nom") or doc.get("nomlong") or type
            props = {
                "nom": label,
                "type": type,
                "arrondissement": doc.get("arrondissement"),
            }
            for f in fields:
                if f not in props and doc.get(f) is not None:
                    props[f] = doc[f]
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": coords},
                "properties": props,
            })

        return {"type": "FeatureCollection", "features": features}

    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": str(exc), "trace": traceback.format_exc()})


@router.get("/geo/arrondissements")
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
            k.score_transport_offre,
            k.score_transport_intensite,
            k.nb_gares,
            k.nb_stations_velib,
            k.capacite_velib_totale,
            k.nb_lignes_transport,
            k.lignes_par_gare_moyen,
            k.nb_modes_lourds,
            k.nb_arrets_bus,
            k.pct_arrets_accessibles,
            k.flux_multimodal,
            k.flux_velo_trott,
            k.flux_bus,
            k.flux_motorise,
            k.pct_flux_velo_trott,
            k.pct_flux_motorise,
            k.pct_flux_voie_cyclable,
            k.score_loisirs,
            k.nb_evenements,
            k.nb_cinemas,
            k.nb_terrasses,
            k.nb_musees,
            k.score_services,
            k.nb_ecoles,
            k.nb_maternelles,
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

    try:
        rows = db.execute(sql, {"annee": annee}).fetchall()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": "SQL failed", "detail": str(e)})

    features = []
    for row in rows:
        try:
            row_dict = dict(row._mapping)
            geometry = row_dict.pop("geometry")
            nom = row_dict.pop("nom", "")
            kpis_data = {k: v for k, v in row_dict.items() if k not in ("arrondissement", "annee")}
            kpis = KPIs(
                arrondissement=row_dict["arrondissement"],
                annee=row_dict.get("annee") or annee,
                **kpis_data,
            )
            kpis_dict = kpis.model_dump()
            kpis_dict["nom"] = nom
            features.append({
                "type": "Feature",
                "geometry": geometry,
                "properties": kpis_dict,
            })
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": f"Row processing failed", "detail": str(e), "trace": traceback.format_exc()})

    return {"type": "FeatureCollection", "features": features}


@router.get("/geo/quartiers")
def get_quartiers_geojson(
    annee: int = Query(default=None, description="Année des KPIs (défaut : dernière disponible)"),
    indicateur: str = Query(default="score_global", description="Indicateur à inclure"),
    db: Session = Depends(get_db),
):
    """GeoJSON FeatureCollection des 80 quartiers administratifs avec KPIs."""
    if annee is None:
        row = db.execute(text("SELECT MAX(annee) FROM gold.quartier_kpis")).fetchone()
        annee = row[0] if row and row[0] else 2024

    sql = text("""
        SELECT
            g.quartier_id,
            g.quartier_code,
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
            k.score_transport_offre,
            k.score_transport_intensite,
            k.nb_gares,
            k.nb_stations_velib,
            k.capacite_velib_totale,
            k.nb_lignes_transport,
            k.lignes_par_gare_moyen,
            k.nb_modes_lourds,
            k.nb_arrets_bus,
            k.pct_arrets_accessibles,
            k.flux_multimodal,
            k.flux_velo_trott,
            k.flux_bus,
            k.flux_motorise,
            k.pct_flux_velo_trott,
            k.pct_flux_motorise,
            k.pct_flux_voie_cyclable,
            k.score_loisirs,
            k.nb_evenements,
            k.nb_cinemas,
            k.nb_terrasses,
            k.nb_musees,
            k.score_services,
            k.nb_ecoles,
            k.nb_maternelles,
            k.nb_colleges,
            k.nb_bibliotheques,
            k.nb_bureaux_poste,
            k.nb_ensup,
            k.score_global
        FROM gold.quartiers_geo g
        LEFT JOIN gold.quartier_kpis k
            ON g.quartier_id = k.quartier_id AND k.annee = :annee
        ORDER BY g.arrondissement NULLS LAST, g.nom
    """)

    try:
        rows = db.execute(sql, {"annee": annee}).fetchall()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": "SQL failed", "detail": str(e)})

    features = []
    for row in rows:
        row_dict = dict(row._mapping)
        geometry = row_dict.pop("geometry")
        nom = row_dict.get("nom")
        kpis_data = {k: v for k, v in row_dict.items() if k not in ("annee",)}
        kpis = KPIs(
            annee=row_dict.get("annee") or annee,
            **kpis_data,
        )
        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": {**kpis.model_dump(), "nom": nom},
        })

    return {"type": "FeatureCollection", "features": features}
