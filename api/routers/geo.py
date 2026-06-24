import json
import os
import traceback
from io import BytesIO

import boto3
import pandas as pd
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import text

from api.database import get_db
from api.models import KPIs
from api.mongo import get_mongo_db

router = APIRouter()

# Mapping type → config de lecture des couches Silver ponctuelles
_POINT_COLLECTIONS = {
    "gares": {
        "collection": "silver_gares",
        "fields": ["nom", "nomlong", "mode_transport", "ligne", "arrondissement"],
        "silver_prefix": "transports/gares/",
        "label_fields": ["nom", "nomlong"],
    },
    "velib": {
        "collection": "silver_velib",
        "fields": ["nom", "capacity", "arrondissement"],
        "silver_prefix": "transports/velib/",
        "label_fields": ["nom"],
    },
    "espaces_verts": {
        "collection": "silver_espaces_verts",
        "fields": ["nom", "arrondissement"],
        "silver_prefix": "qualite_vie/ilots_fraicheur_espaces_verts/",
        "label_fields": ["nom"],
    },
    "musees": {
        "collection": "silver_musees",
        "fields": ["nom", "arrondissement"],
        "silver_prefix": "loisirs/musees_idf/",
        "label_fields": ["nom", "nom_officiel_du_musee"],
    },
    "cinemas": {
        "collection": "silver_cinemas",
        "fields": ["nom", "arrondissement"],
        "silver_prefix": "loisirs/cinemas_idf/",
        "label_fields": ["nom"],
    },
    "bibliotheques": {
        "collection": "silver_bibliotheques",
        "fields": ["nom", "arrondissement"],
        "silver_prefix": "services_publics/bibliotheques/",
        "label_fields": ["nom", "localisation"],
    },
}

_POINT_LOCATION_CANDIDATES = [
    ("geo.lon", "geo.lat"),
    ("geolocalisation.lon", "geolocalisation.lat"),
    ("position.lon", "position.lat"),
    ("coordonnees_geo.lon", "coordonnees_geo.lat"),
    ("longitude", "latitude"),
]

_s3_client = None


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


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        endpoint = os.getenv("MINIO_ENDPOINT", "http://localhost:9000")
        access_key = os.getenv("MINIO_ACCESS_KEY", "admin")
        secret_key = os.getenv("MINIO_SECRET_KEY", "password123")
        _s3_client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
    return _s3_client


def _parse_location(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return None
    return None


def _safe_float(value):
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _extract_arrondissement(row: dict):
    value = row.get("arrondissement")
    if value is not None and not pd.isna(value):
        try:
            value = int(float(value))
            return value if 1 <= value <= 20 else None
        except Exception:
            pass

    for key in ("commune", "situation_geographique", "adresse", "localisation", "nom"):
        text_value = row.get(key)
        if not isinstance(text_value, str):
            continue
        for token in ("Paris 1", "Paris 2", "Paris 3", "Paris 4", "Paris 5", "Paris 6", "Paris 7", "Paris 8", "Paris 9",
                      "Paris 10", "Paris 11", "Paris 12", "Paris 13", "Paris 14", "Paris 15", "Paris 16", "Paris 17",
                      "Paris 18", "Paris 19", "Paris 20"):
            if token in text_value:
                try:
                    return int(token.split()[-1])
                except Exception:
                    return None
    return None


def _extract_point_geometry(row: dict):
    loc = _parse_location(row.get("location"))
    if isinstance(loc, dict) and loc.get("type") == "Point":
        coords = loc.get("coordinates")
        if isinstance(coords, list) and len(coords) >= 2:
            lon = _safe_float(coords[0])
            lat = _safe_float(coords[1])
            if lon is not None and lat is not None:
                return {"type": "Point", "coordinates": [lon, lat]}

    for lon_key, lat_key in _POINT_LOCATION_CANDIDATES:
        lon = _safe_float(row.get(lon_key))
        lat = _safe_float(row.get(lat_key))
        if lon is not None and lat is not None:
            return {"type": "Point", "coordinates": [lon, lat]}

    return None


def _build_point_feature(row: dict, point_type: str, fields: list[str], label_fields: list[str]):
    geometry = _extract_point_geometry(row)
    if geometry is None:
        return None

    arrondissement = _extract_arrondissement(row)
    label = next(
        (row.get(field) for field in label_fields if isinstance(row.get(field), str) and row.get(field)),
        point_type,
    )

    props = {
        "nom": label,
        "type": point_type,
        "arrondissement": arrondissement,
    }
    for field in fields:
        if field not in props and row.get(field) is not None and not pd.isna(row.get(field)):
            props[field] = row.get(field)

    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": props,
    }


def _fetch_points_from_mongo(point_type: str, config: dict, arrondissement: int | None):
    db = get_mongo_db()
    coll = db[config["collection"]]
    query: dict = {"location": {"$exists": True, "$ne": None}}
    if arrondissement and 1 <= arrondissement <= 20:
        query["arrondissement"] = arrondissement

    projection = {field: 1 for field in config["fields"]}
    projection["location"] = 1

    features = []
    for doc in coll.find(query, projection).limit(2000):
        feature = _build_point_feature(doc, point_type, config["fields"], config["label_fields"])
        if feature is not None:
            features.append(feature)
    return features


def _list_matching_silver_keys(silver_prefix: str):
    s3 = _get_s3_client()
    bucket = "silver"
    continuation_token = None
    matches: list[str] = []

    while True:
        kwargs = {"Bucket": bucket}
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token
        response = s3.list_objects_v2(**kwargs)
        for obj in response.get("Contents", []):
            key = obj["Key"]
            if silver_prefix in key and key.endswith("/clean.parquet"):
                matches.append(key)
        if not response.get("IsTruncated"):
            break
        continuation_token = response.get("NextContinuationToken")

    return sorted(matches)


def _fetch_points_from_silver_parquet(point_type: str, config: dict, arrondissement: int | None):
    matches = _list_matching_silver_keys(config["silver_prefix"])
    if not matches:
        return []

    latest_key = matches[-1]
    s3 = _get_s3_client()
    obj = s3.get_object(Bucket="silver", Key=latest_key)
    df = pd.read_parquet(BytesIO(obj["Body"].read()))

    features = []
    for row in df.where(pd.notna(df), None).to_dict("records"):
        feature = _build_point_feature(row, point_type, config["fields"], config["label_fields"])
        if feature is None:
            continue
        arr = feature["properties"].get("arrondissement")
        if arrondissement and 1 <= arrondissement <= 20 and arr != arrondissement:
            continue
        features.append(feature)
        if len(features) >= 2000:
            break

    return features


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

    try:
        config = _POINT_COLLECTIONS[type]
        features = _fetch_points_from_mongo(type, config, arrondissement)
        if not features:
            features = _fetch_points_from_silver_parquet(type, config, arrondissement)

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
    annee = _resolve_available_year(db, "gold.arrondissement_kpis", annee)

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
    annee = _resolve_available_year(db, "gold.quartier_kpis", annee)

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


@router.get("/geo/iris")
def get_iris_geojson(
    annee: int = Query(default=None, description="Année des KPIs (défaut : dernière disponible)"),
    indicateur: str = Query(default="score_global", description="Indicateur à inclure"),
    db: Session = Depends(get_db),
):
    """GeoJSON FeatureCollection des IRIS de Paris avec KPIs."""
    annee = _resolve_available_year(db, "gold.iris_kpis", annee)

    sql = text("""
        SELECT
            g.iris_id,
            g.iris_code,
            g.quartier_id,
            g.quartier_code,
            g.arrondissement,
            g.nom,
            g.iris_type,
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
        FROM gold.iris_geo g
        LEFT JOIN gold.iris_kpis k
            ON g.iris_id = k.iris_id AND k.annee = :annee
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
        row_dict["iris_nom"] = nom
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
