"""
Gold Layer Aggregator — Urban Data Explorer Paris
Phase 1 : Silver (MinIO Parquet) → MongoDB Gold (documents géospatiaux)
Phase 2 : MongoDB Gold → PostgreSQL/PostGIS (KPIs agrégés)

Pour chaque arrondissement (1-20) :
  1. COUNT/AVG des entités MongoDB Gold par collection
  2. Normalisation min-max → score 0-100
  3. Score composite par indicateur (moyenne pondérée)
  4. Score global = moyenne des 4 indicateurs composites
  5. Upsert dans gold.arrondissement_kpis
"""

import logging
import math
import re
import json
import statistics
import sys
import time
import requests
import pandas as pd
import boto3
from io import BytesIO
from pathlib import Path
from botocore.exceptions import ClientError
from pymongo import MongoClient, UpdateOne
from sqlalchemy import create_engine, text

# Permet l'execution directe via `python pipeline/gold_aggregator.py`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.config import (
    MONGO_URI, MONGO_DB, POSTGRES_DSN, BAN_API,
    MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, BUCKET_SILVER,
)
from pipeline.progress_utils import tqdm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("gold_aggregator")

ARRONDISSEMENTS = list(range(1, 21))
ARRONDISSEMENTS_GEO_URL = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/arrondissements/exports/geojson?lang=fr&timezone=Europe%2FParis"
QUARTIERS_GEO_URL = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/quartier_paris/exports/geojson?lang=fr&timezone=Europe%2FParis"
IRIS_GEO_URL = 'https://opendata.iledefrance.fr/api/explore/v2.1/catalog/datasets/iris/exports/geojson?where=startswith(code_iris,"751")'
_QUARTIER_SHAPES = None
_QUARTIER_BY_ID = None
_QUARTIER_POINT_CACHE = {}
_IRIS_SHAPES = None
_IRIS_BY_ID = None
_IRIS_POINT_CACHE = {}

# ─── Connexions ───────────────────────────────────────────────────────────────

def get_mongo():
    client = MongoClient(MONGO_URI)
    return client[MONGO_DB]


def get_engine():
    return create_engine(POSTGRES_DSN)


def _get_s3():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
    )


# ─── Phase 1 : Silver Parquet → MongoDB Gold ──────────────────────────────────

# (collection_mongo, id_col, indicateur)
_MONGO_LOAD_CONFIG = {
    "ilots_fraicheur_espaces_verts": ("silver_espaces_verts",      "identifiant",    "qualite_vie"),
    "ilots_fraicheur_equipements":   ("silver_equipements",        "identifiant",    "qualite_vie"),
    "arbres":                        ("silver_arbres",             "idbase",         "qualite_vie"),
    "qualite_air":                   ("silver_qualite_air",        "annee",          "qualite_vie"),
    "fibre_actuel":                  ("silver_fibre_actuel",       None,             "qualite_vie"),
    "fibre_base_imb":                ("silver_fibre_imb",          "imb_id",         "qualite_vie"),
    "fibre_base_imb_fc":             ("silver_fibre_imb_fc",       "immeuble_id",    "qualite_vie"),
    "fibre_debit_filaire":           ("silver_fibre_debit",        "code_dep",       "qualite_vie"),
    "fibre_operateur":               ("silver_fibre_operateur",    "code",           "qualite_vie"),
    "zones_touristiques":            ("silver_zones_touristiques", None,             "qualite_vie"),
    "gares":                         ("silver_gares",              None,             "transports"),
    "bus":                           ("silver_bus",                None,             "transports"),
    "evenements_paris":              ("silver_evenements",         None,             "loisirs"),
    "terrasses":                     ("silver_terrasses",          None,             "loisirs"),
    "cinemas_idf":                   ("silver_cinemas",            None,             "loisirs"),
    "musees_idf":                    ("silver_musees",             None,             "loisirs"),
    "ecoles_elementaires":           ("silver_ecoles_elem",        None,             "services_publics"),
    "maternelles_secteurs":          ("silver_maternelles",        None,             "services_publics"),
    "colleges_secteurs":             ("silver_colleges",           None,             "services_publics"),
    "bibliotheques":                 ("silver_bibliotheques",      None,             "services_publics"),
    "enseignement_superieur":        ("silver_ensup",              None,             "services_publics"),
    "bureaux_poste":                 ("silver_bureaux_poste",      None,             "services_publics"),
    "revenus_medians":               ("silver_revenus",            "arrondissement", "immobilier"),
    "logements_sociaux":             ("silver_logements_sociaux",  None,             "immobilier"),
    "dvf_prix_m2":                   ("silver_dvf",                None,             "immobilier"),
}

_BULK_CHUNK = 5_000


def _latest_silver_date(s3) -> str | None:
    """Trouve la partition ingestion_date= la plus récente dans MinIO silver."""
    try:
        resp = s3.list_objects_v2(Bucket=BUCKET_SILVER, Delimiter="/", Prefix="ingestion_date=")
        prefixes = [p["Prefix"] for p in resp.get("CommonPrefixes", [])]
        dates = []
        for p in prefixes:
            import re as _re
            m = _re.match(r"ingestion_date=(\d{4}-\d{2}-\d{2})/", p)
            if m:
                dates.append(m.group(1))
        return max(dates) if dates else None
    except Exception:
        return None


def _read_silver_clean(s3, dataset_id: str, indicateur: str, ingestion_date: str) -> pd.DataFrame | None:
    key = f"ingestion_date={ingestion_date}/{indicateur}/{dataset_id}/clean.parquet"
    try:
        obj = s3.get_object(Bucket=BUCKET_SILVER, Key=key)
        return pd.read_parquet(BytesIO(obj["Body"].read()))
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            log.warning(f"  Silver absent : {key}")
            return None
        raise


def _write_mongo_collection(db, df: pd.DataFrame, collection_name: str, id_col: str | None):
    """Écrit un DataFrame dans MongoDB Gold (upsert ou bulk insert)."""
    coll = db[collection_name]
    records = df.where(pd.notna(df), other=None).to_dict("records")
    if not records:
        return

    if id_col and id_col in df.columns and len(records) < 10_000:
        ops = [UpdateOne({id_col: r[id_col]}, {"$set": r}, upsert=True) for r in records]
        result = coll.bulk_write(ops, ordered=False)
        log.info(f"    ✓ MongoDB gold/{collection_name} — upserted={result.upserted_count} modified={result.modified_count}")
    else:
        coll.drop()
        total = 0
        for i in range(0, len(records), _BULK_CHUNK):
            coll.insert_many(records[i:i + _BULK_CHUNK], ordered=False)
            total += min(_BULK_CHUNK, len(records) - i)
        log.info(f"    ✓ MongoDB gold/{collection_name} — {total} insérés (bulk)")

    if "location" in df.columns:
        try:
            coll.create_index([("location", "2dsphere")])
        except Exception:
            pass
    for idx_col in ("arrondissement", "quartier_id", "iris_id", "_indicateur"):
        if idx_col in df.columns:
            coll.create_index(idx_col)


def load_silver_to_mongo(db, ingestion_date: str | None = None) -> str:
    """Phase 1 Gold : lit les clean.parquet Silver et les charge dans MongoDB Gold."""
    s3 = _get_s3()
    if ingestion_date is None:
        ingestion_date = _latest_silver_date(s3)
        if not ingestion_date:
            raise RuntimeError("Aucune partition Silver trouvée dans MinIO — lancer silver_transformer d'abord")
    log.info(f"  Partition Silver : ingestion_date={ingestion_date}")

    ok, skip, ko = 0, 0, 0
    for dataset_id, (collection, id_col, indicateur) in _MONGO_LOAD_CONFIG.items():
        df = _read_silver_clean(s3, dataset_id, indicateur, ingestion_date)
        if df is None:
            skip += 1
            continue
        try:
            _write_mongo_collection(db, df, collection, id_col)
            ok += 1
        except Exception as e:
            log.error(f"  ✗ {dataset_id} → {collection} : {e}")
            ko += 1

    log.info(f"  Phase 1 terminée — {ok} collections chargées, {skip} absentes, {ko} erreurs")
    return ingestion_date


# ─── Helpers ──────────────────────────────────────────────────────────────────

def minmax_normalize(values: dict) -> dict:
    """Normalise un dict {arrondissement: valeur} → scores 0-100."""
    vals = [v for v in values.values() if v is not None and not math.isnan(v)]
    if not vals:
        return {k: 0.0 for k in values}
    vmin, vmax = min(vals), max(vals)
    if vmax == vmin:
        return {k: 50.0 for k in values}
    return {
        k: round((v - vmin) / (vmax - vmin) * 100, 2) if v is not None else 0.0
        for k, v in values.items()
    }


def count_by_arr(coll, arr_field: str = "arrondissement") -> dict:
    """Compte les documents par arrondissement dans une collection MongoDB."""
    pipeline = [
        {"$match": {arr_field: {"$in": ARRONDISSEMENTS}}},
        {"$group": {"_id": f"${arr_field}", "count": {"$sum": 1}}},
    ]
    return {doc["_id"]: doc["count"] for doc in coll.aggregate(pipeline)}


def avg_by_arr(coll, value_field: str, arr_field: str = "arrondissement") -> dict:
    """Moyenne d'un champ numérique par arrondissement."""
    pipeline = [
        {"$match": {arr_field: {"$in": ARRONDISSEMENTS}, value_field: {"$exists": True, "$ne": None}}},
        {"$group": {"_id": f"${arr_field}", "avg": {"$avg": f"${value_field}"}}},
    ]
    return {doc["_id"]: doc["avg"] for doc in coll.aggregate(pipeline)}


def safe_get(d: dict, arr: int, default=0):
    v = d.get(arr, default)
    return v if v is not None and not (isinstance(v, float) and math.isnan(v)) else default


def weighted_avg(scored_pairs: list, zone_keys: list | None = None, skip_zero_collections: bool = True) -> dict:
    """
    Moyenne pondérée de sous-scores normalisés par arrondissement.

    scored_pairs : liste de (scores_dict {arr: float}, poids int)
    skip_zero_collections : si True, ignore les collections où tous les arrondissements = 0
      (évite de pénaliser un arrondissement quand toute une source est vide)
    """
    if zone_keys is None:
        zone_keys = sorted({key for scores, _ in scored_pairs for key in scores.keys()})
    result = {zone: {"num": 0.0, "den": 0} for zone in zone_keys}
    for scores, poids in scored_pairs:
        if skip_zero_collections and all(v == 0 for v in scores.values()):
            continue
        for zone in zone_keys:
            result[zone]["num"] += scores.get(zone, 0) * poids
            result[zone]["den"] += poids
    return {
        zone: round(result[zone]["num"] / result[zone]["den"], 2) if result[zone]["den"] > 0 else 0.0
        for zone in zone_keys
    }


def invert(scores: dict) -> dict:
    """Inverse un dict de scores normalisés 0-100 → (100 - score)."""
    return {arr: round(100 - v, 2) for arr, v in scores.items()}


def _first_present(props: dict, keys: list[str], default=None):
    """Retourne la première propriété non vide trouvée parmi plusieurs alias."""
    for key in keys:
        value = props.get(key)
        if value not in (None, ""):
            return value
    return default


def _default_arrondissement_kpis() -> dict:
    """Valeurs par défaut pour garantir un upsert Gold complet."""
    return {
        "prix_m2_median": None,
        "pct_logements_sociaux": None,
        "nb_logements_sociaux": 0,
        "revenu_median_uc": None,
        "taux_effort_achat": None,
        "surface_mediane": None,
        "nb_appartements": None,
        "nb_maisons": None,
        "pct_appartements": None,
        "nb_t1": None,
        "nb_t2": None,
        "nb_t3": None,
        "nb_t4plus": None,
        "score_qualite_vie": 0.0,
        "nb_espaces_verts": 0,
        "nb_arbres": 0,
        "score_air_no2": 0.0,
        "score_air_pm25": 0.0,
        "pct_fibre": 0.0,
        "nb_sanisettes": 0,
        "nb_chantiers_actifs": 0,
        "nb_anomalies": 0,
        "score_transports": 0.0,
        "score_transport_offre": 0.0,
        "score_transport_intensite": 0.0,
        "nb_gares": 0,
        "nb_stations_velib": 0,
        "capacite_velib_totale": 0.0,
        "nb_lignes_transport": 0,
        "lignes_par_gare_moyen": 0.0,
        "nb_modes_lourds": 0,
        "nb_arrets_bus": 0,
        "pct_arrets_accessibles": 0.0,
        "flux_multimodal": 0.0,
        "flux_velo_trott": 0.0,
        "flux_bus": 0.0,
        "flux_motorise": 0.0,
        "pct_flux_velo_trott": 0.0,
        "pct_flux_motorise": 0.0,
        "pct_flux_voie_cyclable": 0.0,
        "score_loisirs": 0.0,
        "nb_evenements": 0,
        "nb_cinemas": 0,
        "nb_terrasses": 0,
        "nb_musees": 0,
        "score_services": 0.0,
        "nb_ecoles": 0,
        "nb_maternelles": 0,
        "nb_colleges": 0,
        "nb_bibliotheques": 0,
        "nb_bureaux_poste": 0,
        "nb_ensup": 0,
        "score_global": 0.0,
    }


def _parse_arrondissement_number(value) -> int | None:
    """Normalise une représentation d'arrondissement vers un entier 1-20."""
    if value in (None, ""):
        return None
    s = str(value).strip()
    m = re.match(r"^75[01](\d{2})$", s)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 20 else None
    m = re.search(r"\b(\d{1,2})\b", s)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 20 else None
    return None


def _as_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _median(values: list[float]) -> float | None:
    cleaned = [float(v) for v in values if v is not None and not math.isnan(float(v))]
    if not cleaned:
        return None
    return round(float(statistics.median(cleaned)), 2)


def _is_truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "t", "yes", "oui", "vrai"}


def _normalize_mode_label(value) -> str:
    return str(value or "").strip().lower()


def _is_active_heavy_mode(mode_label: str) -> bool:
    label = _normalize_mode_label(mode_label)
    return any(token in label for token in ("metro", "rer", "tram", "train", "val"))


def _is_velo_or_trott_mode(mode_label: str) -> bool:
    label = _normalize_mode_label(mode_label)
    return any(token in label for token in ("velo", "vélo", "trott", "cycl"))


def _is_bus_mode(mode_label: str) -> bool:
    label = _normalize_mode_label(mode_label)
    return "bus" in label


def _is_motorized_mode(mode_label: str) -> bool:
    label = _normalize_mode_label(mode_label)
    return any(token in label for token in ("motor", "moto", "veh", "véh", "auto", "voiture", "car", "camion"))


def _is_cycle_way(voie_label: str) -> bool:
    label = _normalize_mode_label(voie_label)
    return any(token in label for token in ("cycl", "velo", "vélo", "trott"))


def _extract_polygon_rings(geometry: dict) -> list[list[list[float]]]:
    """Extrait les anneaux externes d'une géométrie GeoJSON Polygon/MultiPolygon."""
    if not isinstance(geometry, dict):
        return []
    gtype = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if gtype == "Polygon":
        return [coords[0]] if coords else []
    if gtype == "MultiPolygon":
        return [poly[0] for poly in coords if poly]
    return []


def _ring_centroid(ring: list[list[float]]) -> tuple[float, float] | None:
    """Calcule un centroïde 2D simple pour un anneau de polygone."""
    if len(ring) < 3:
        return None

    area_twice = 0.0
    centroid_x = 0.0
    centroid_y = 0.0

    for i in range(len(ring)):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % len(ring)][0], ring[(i + 1) % len(ring)][1]
        cross = (x1 * y2) - (x2 * y1)
        area_twice += cross
        centroid_x += (x1 + x2) * cross
        centroid_y += (y1 + y2) * cross

    if abs(area_twice) < 1e-12:
        xs = [pt[0] for pt in ring]
        ys = [pt[1] for pt in ring]
        return (sum(xs) / len(xs), sum(ys) / len(ys))

    factor = 1 / (3 * area_twice)
    return (centroid_x * factor, centroid_y * factor)


def _geometry_center_point(geometry: dict) -> dict | None:
    """Construit un point GeoJSON au centre approximatif d'un polygone/multipolygone."""
    rings = _extract_polygon_rings(geometry)
    if not rings:
        return None

    outer_ring = max(rings, key=len)
    centroid = _ring_centroid(outer_ring)
    if centroid is None:
        return None

    return {"type": "Point", "coordinates": [float(centroid[0]), float(centroid[1])]}


def _point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    """Test de point-in-polygon simple par ray casting sur un anneau."""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        intersects = ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        )
        if intersects:
            inside = not inside
        j = i
    return inside


def _load_quartier_shapes():
    """Charge et met en cache les polygones et métadonnées des quartiers administratifs."""
    global _QUARTIER_SHAPES, _QUARTIER_BY_ID
    if _QUARTIER_SHAPES is not None and _QUARTIER_BY_ID is not None:
        return _QUARTIER_SHAPES, _QUARTIER_BY_ID

    shapes = []
    quartiers_by_id = {}
    geojson_features = []

    try:
        response = requests.get(QUARTIERS_GEO_URL, timeout=30)
        response.raise_for_status()
        geojson_features = response.json().get("features", [])
    except Exception as exc:
        log.warning(f"  Impossible de charger les polygones de quartiers depuis l'Open Data Paris : {exc}")

    if not geojson_features:
        try:
            engine = get_engine()
            with engine.connect() as conn:
                rows = conn.execute(text("""
                    SELECT
                        quartier_id,
                        quartier_code,
                        arrondissement,
                        nom,
                        ST_AsGeoJSON(geom) AS geometry
                    FROM gold.quartiers_geo
                    ORDER BY arrondissement NULLS LAST, nom
                """)).mappings().all()
            for row in rows:
                geometry = row["geometry"]
                if isinstance(geometry, str):
                    geometry = json.loads(geometry)
                geojson_features.append({
                    "type": "Feature",
                    "geometry": geometry,
                    "properties": {
                        "quartier_id": row["quartier_id"],
                        "quartier_code": row["quartier_code"],
                        "arrondissement": row["arrondissement"],
                        "nom": row["nom"],
                    },
                })
            if geojson_features:
                log.info("  Polygones quartiers rechargés depuis gold.quartiers_geo")
        except Exception as exc:
            log.warning(f"  Impossible de charger les polygones de quartiers depuis PostgreSQL : {exc}")

    try:
        for feature in geojson_features:
            props = feature.get("properties", {})
            quartier_id = _first_present(props, [
                "n_sq_qu", "id_quartier", "quartier_id", "c_qu", "code_qu",
            ])
            quartier_code = _first_present(props, [
                "c_qu", "code_quartier", "code_qu", "quartier_code",
            ], quartier_id)
            nom = _first_present(props, [
                "l_qu", "nom_quart", "nom_quartier", "quartier", "nom",
            ])
            arr_raw = _first_present(props, [
                "c_ar", "arrondissement", "code_arrondissement", "c_arinsee", "arr_insee",
            ])
            arr_num = _parse_arrondissement_number(arr_raw)
            rings = _extract_polygon_rings(feature.get("geometry", {}))

            if not quartier_id:
                quartier_id = quartier_code or nom
            if not quartier_id or not nom or not rings:
                continue

            info = {
                "quartier_id": str(quartier_id),
                "quartier_code": str(quartier_code) if quartier_code is not None else None,
                "arrondissement": arr_num,
                "nom": str(nom),
                "rings": rings,
            }
            shapes.append(info)
            quartiers_by_id[info["quartier_id"]] = info
    except Exception as exc:
        log.warning(f"  Impossible de préparer les polygones de quartiers pour l'agrégation fine : {exc}")
        shapes = []
        quartiers_by_id = {}

    _QUARTIER_SHAPES = shapes
    _QUARTIER_BY_ID = quartiers_by_id
    return _QUARTIER_SHAPES, _QUARTIER_BY_ID


def _load_iris_shapes():
    """Charge et met en cache les polygones et métadonnées IRIS de Paris."""
    global _IRIS_SHAPES, _IRIS_BY_ID
    if _IRIS_SHAPES is not None and _IRIS_BY_ID is not None:
        return _IRIS_SHAPES, _IRIS_BY_ID

    shapes = []
    iris_by_id = {}
    geojson_features = []

    try:
        response = requests.get(IRIS_GEO_URL, timeout=60)
        response.raise_for_status()
        geojson_features = response.json().get("features", [])
    except Exception as exc:
        log.warning(f"  Impossible de charger les polygones IRIS depuis l'Open Data IDF : {exc}")

    if not geojson_features:
        try:
            engine = get_engine()
            with engine.connect() as conn:
                rows = conn.execute(text("""
                    SELECT
                        iris_id,
                        iris_code,
                        quartier_id,
                        quartier_code,
                        arrondissement,
                        nom,
                        iris_type,
                        ST_AsGeoJSON(geom) AS geometry
                    FROM gold.iris_geo
                    ORDER BY arrondissement NULLS LAST, nom
                """)).mappings().all()
            for row in rows:
                geometry = row["geometry"]
                if isinstance(geometry, str):
                    geometry = json.loads(geometry)
                geojson_features.append({
                    "type": "Feature",
                    "geometry": geometry,
                    "properties": {
                        "iris_id": row["iris_id"],
                        "iris_code": row["iris_code"],
                        "quartier_id": row["quartier_id"],
                        "quartier_code": row["quartier_code"],
                        "arrondissement": row["arrondissement"],
                        "nom": row["nom"],
                        "iris_type": row["iris_type"],
                    },
                })
            if geojson_features:
                log.info("  Polygones IRIS rechargés depuis gold.iris_geo")
        except Exception as exc:
            log.warning(f"  Impossible de charger les polygones IRIS depuis PostgreSQL : {exc}")

    try:
        for feature in geojson_features:
            props = feature.get("properties", {})
            iris_id = _first_present(props, ["code_iris", "iris_id", "id"])
            iris_code = _first_present(props, ["code_iris", "iris_code"], iris_id)
            nom = _first_present(props, ["nom_iris", "nom", "iris_nom"])
            iris_type = _first_present(props, ["typ_iris", "iris_type"])
            arr_num = _parse_arrondissement_number(
                _first_present(props, ["insee_com", "arrondissement"], str(iris_code)[:5] if iris_code else None)
            )
            quartier_id = _first_present(props, ["quartier_id"])
            quartier_code = _first_present(props, ["quartier_code"])
            rings = _extract_polygon_rings(feature.get("geometry", {}))

            if not iris_id or not nom or not rings:
                continue

            info = {
                "iris_id": str(iris_id),
                "iris_code": str(iris_code) if iris_code is not None else str(iris_id),
                "quartier_id": str(quartier_id) if quartier_id not in (None, "") else None,
                "quartier_code": str(quartier_code) if quartier_code not in (None, "") else None,
                "arrondissement": arr_num,
                "nom": str(nom),
                "iris_type": str(iris_type) if iris_type not in (None, "") else None,
                "rings": rings,
            }
            shapes.append(info)
            iris_by_id[info["iris_id"]] = info
    except Exception as exc:
        log.warning(f"  Impossible de préparer les polygones IRIS pour l'agrégation fine : {exc}")
        shapes = []
        iris_by_id = {}

    _IRIS_SHAPES = shapes
    _IRIS_BY_ID = iris_by_id
    return _IRIS_SHAPES, _IRIS_BY_ID


def infer_quartier_from_location(location: dict | None) -> dict | None:
    """Déduit le quartier administratif à partir d'un point GeoJSON WGS84."""
    if not isinstance(location, dict):
        return None
    coords = location.get("coordinates", [])
    if len(coords) != 2:
        return None
    try:
        lon = float(coords[0])
        lat = float(coords[1])
    except Exception:
        return None

    cache_key = (round(lon, 6), round(lat, 6))
    cached = _QUARTIER_POINT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    quartiers, _ = _load_quartier_shapes()
    for quartier in quartiers:
        if any(_point_in_ring(lon, lat, ring) for ring in quartier["rings"]):
            _QUARTIER_POINT_CACHE[cache_key] = quartier
            return quartier

    _QUARTIER_POINT_CACHE[cache_key] = None
    return None


def infer_iris_from_location(location: dict | None) -> dict | None:
    """Déduit l'IRIS à partir d'un point GeoJSON WGS84."""
    if not isinstance(location, dict):
        return None
    coords = location.get("coordinates", [])
    if len(coords) != 2:
        return None
    try:
        lon = float(coords[0])
        lat = float(coords[1])
    except Exception:
        return None

    cache_key = (round(lon, 6), round(lat, 6))
    cached = _IRIS_POINT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    iris_shapes, _ = _load_iris_shapes()
    for iris in iris_shapes:
        if any(_point_in_ring(lon, lat, ring) for ring in iris["rings"]):
            _IRIS_POINT_CACHE[cache_key] = iris
            return iris

    _IRIS_POINT_CACHE[cache_key] = None
    return None


def _resolve_quartier_meta(doc: dict, quartiers_by_id: dict | None = None) -> dict | None:
    """Récupère les métadonnées quartier soit depuis le document, soit via le point géographique."""
    if quartiers_by_id is None:
        _, quartiers_by_id = _load_quartier_shapes()
    quartier_id = doc.get("quartier_id")
    if quartier_id is not None:
        quartier = quartiers_by_id.get(str(quartier_id))
        if quartier is not None:
            return quartier
    return infer_quartier_from_location(doc.get("location"))


def _resolve_iris_meta(doc: dict, iris_by_id: dict | None = None) -> dict | None:
    """Récupère les métadonnées IRIS soit depuis le document, soit via le point géographique."""
    if iris_by_id is None:
        _, iris_by_id = _load_iris_shapes()
    iris_id = doc.get("iris_id")
    if iris_id is not None:
        iris = iris_by_id.get(str(iris_id))
        if iris is not None:
            return iris
    return infer_iris_from_location(doc.get("location"))


def _count_by_quartier(coll, quartier_ids: list[str]) -> dict:
    counts = {qid: 0 for qid in quartier_ids}
    quartier_ids_set = set(quartier_ids)

    pipeline = [
        {"$match": {"quartier_id": {"$in": quartier_ids}}},
        {"$group": {"_id": "$quartier_id", "count": {"$sum": 1}}},
    ]
    for doc in coll.aggregate(pipeline):
        qid = str(doc["_id"])
        if qid in counts:
            counts[qid] = int(doc["count"])

    _, quartiers_by_id = _load_quartier_shapes()
    fallback_query = {"$or": [{"quartier_id": {"$exists": False}}, {"quartier_id": None}]}
    for doc in coll.find(fallback_query, {"quartier_id": 1, "location": 1}):
        quartier = _resolve_quartier_meta(doc, quartiers_by_id)
        if quartier and quartier["quartier_id"] in quartier_ids_set:
            counts[quartier["quartier_id"]] += 1
    return counts


def _iter_quartier_docs(coll, query: dict, projection: dict, quartier_ids: list[str]):
    """
    Itère sur des documents enrichis quartier quand disponible.
    Utilise `quartier_id` directement et ne retombe sur le calcul géométrique
    que pour les anciens documents Silver qui n'ont pas encore été réenrichis.
    """
    quartier_ids_set = set(quartier_ids)
    _, quartiers_by_id = _load_quartier_shapes()
    for doc in coll.find(query, projection):
        quartier = _resolve_quartier_meta(doc, quartiers_by_id)
        if quartier and quartier["quartier_id"] in quartier_ids_set:
            yield quartier["quartier_id"], doc


def _count_by_iris(coll, iris_ids: list[str]) -> dict:
    counts = {iris_id: 0 for iris_id in iris_ids}
    iris_ids_set = set(iris_ids)

    pipeline = [
        {"$match": {"iris_id": {"$in": iris_ids}}},
        {"$group": {"_id": "$iris_id", "count": {"$sum": 1}}},
    ]
    for doc in coll.aggregate(pipeline):
        iris_id = str(doc["_id"])
        if iris_id in counts:
            counts[iris_id] = int(doc["count"])

    _, iris_by_id = _load_iris_shapes()
    fallback_query = {"$or": [{"iris_id": {"$exists": False}}, {"iris_id": None}]}
    for doc in coll.find(fallback_query, {"iris_id": 1, "location": 1}):
        iris = _resolve_iris_meta(doc, iris_by_id)
        if iris and iris["iris_id"] in iris_ids_set:
            counts[iris["iris_id"]] += 1
    return counts


def _iter_iris_docs(coll, query: dict, projection: dict, iris_ids: list[str]):
    """Itère sur des documents enrichis IRIS quand disponible."""
    iris_ids_set = set(iris_ids)
    _, iris_by_id = _load_iris_shapes()
    for doc in coll.find(query, projection):
        iris = _resolve_iris_meta(doc, iris_by_id)
        if iris and iris["iris_id"] in iris_ids_set:
            yield iris["iris_id"], doc


# ─── Agrégations par indicateur ───────────────────────────────────────────────

# Scoring Indicateur 1 — Qualité de vie
#
# Source                  | Collection            | Champ      | Poids | Signe
# ----------------------- | --------------------- | ---------- | ----- | ------
# Espaces verts / îlots   | silver_espaces_verts  | COUNT      |   2   | +
# Arbres                  | silver_arbres         | COUNT      |   1   | +
# Sanisettes              | silver_sanisettes     | COUNT      |   1   | +
# Fibre (% déployé)       | silver_fibre_imb      | % déployé  |   1   | +
# Chantiers               | silver_chantiers      | COUNT      |   1   | - (inversé)
# Anomalies signalées     | silver_anomalies      | COUNT      |   1   | - (inversé)
# Qualité air NO2         | silver_qualite_air    | no2 (avg)  |   1   | - (inversé)

def agg_qualite_vie(db) -> dict:
    espaces_verts = count_by_arr(db["silver_espaces_verts"])
    arbres        = count_by_arr(db["silver_arbres"])
    sanisettes    = count_by_arr(db["silver_sanisettes"])
    chantiers     = count_by_arr(db["silver_chantiers"])
    anomalies     = count_by_arr(db["silver_anomalies"])

    # Air : données Paris-wide → valeur propagée à tous les arrondissements
    air_doc        = db["silver_qualite_air"].find_one(sort=[("annee", -1)])
    score_air_no2  = float(air_doc.get("no2", 0))   if air_doc else 0.0
    score_air_pm25 = float(air_doc.get("pm2_5", 0)) if air_doc else 0.0
    # NO2 normalisé sur 100 µg/m³ (seuil OMS annuel) puis inversé
    no2_norm = {a: min(score_air_no2 / 100.0 * 100, 100) for a in ARRONDISSEMENTS}

    # Fibre : % immeubles avec statut "Déployé" par arrondissement
    pct_fibre = {}
    for arr in ARRONDISSEMENTS:
        total    = db["silver_fibre_imb"].count_documents({"arrondissement": arr})
        deployes = db["silver_fibre_imb"].count_documents(
            {"arrondissement": arr, "statut_immeuble": {"$regex": "Déployé", "$options": "i"}}
        ) if total else 0
        pct_fibre[arr] = round(deployes / total * 100, 1) if total else 0

    result = {}
    for arr in ARRONDISSEMENTS:
        result[arr] = {
            "nb_espaces_verts":    safe_get(espaces_verts, arr),
            "nb_arbres":           safe_get(arbres, arr),
            "nb_sanisettes":       safe_get(sanisettes, arr),
            "nb_chantiers_actifs": safe_get(chantiers, arr),
            "nb_anomalies":        safe_get(anomalies, arr),
            "score_air_no2":       score_air_no2,
            "score_air_pm25":      score_air_pm25,
            "pct_fibre":           pct_fibre.get(arr, 0),
        }

    s_ev  = minmax_normalize({a: result[a]["nb_espaces_verts"]    for a in ARRONDISSEMENTS})
    s_arb = minmax_normalize({a: result[a]["nb_arbres"]           for a in ARRONDISSEMENTS})
    s_san = minmax_normalize({a: result[a]["nb_sanisettes"]       for a in ARRONDISSEMENTS})
    s_fib = minmax_normalize({a: result[a]["pct_fibre"]           for a in ARRONDISSEMENTS})
    s_cha = invert(minmax_normalize({a: result[a]["nb_chantiers_actifs"] for a in ARRONDISSEMENTS}))
    s_ano = invert(minmax_normalize({a: result[a]["nb_anomalies"]        for a in ARRONDISSEMENTS}))
    s_air = invert(no2_norm)  # NO2 bas = meilleur

    scores = weighted_avg([
        (s_ev,  2),
        (s_arb, 1),
        (s_san, 1),
        (s_fib, 1),
        (s_cha, 1),
        (s_ano, 1),
        (s_air, 1),
    ])
    for arr in ARRONDISSEMENTS:
        result[arr]["score_qualite_vie"] = scores[arr]

    return result


# Scoring Indicateur 2 — Transports
#
# Formule retenue :
#   score_transports = 0.6 * Offre + 0.4 * Intensite
#
# Offre :
# - densité / nombre de stations Vélib
# - capacité totale Vélib
# - nombre de gares physiques
# - nombre moyen de lignes par gare
# - variété ferrée/lourde (metro, RER, tram, train, VAL)
# - densité d'arrêts
# - part d'arrêts accessibles quand disponible
#
# Intensité :
# - flux total observé
# - flux vélo / trottinette
# - flux bus
# - part de flux sur voies cyclables
# - part motorisée (inversée)

def agg_transports(db) -> dict:
    result = {
        arr: {
            "nb_stations_velib": 0,
            "capacite_velib_totale": 0.0,
            "nb_gares": 0,
            "nb_lignes_transport": 0,
            "lignes_par_gare_moyen": 0.0,
            "nb_modes_lourds": 0,
            "nb_arrets_bus": 0,
            "pct_arrets_accessibles": 0.0,
            "flux_multimodal": 0.0,
            "flux_velo_trott": 0.0,
            "flux_bus": 0.0,
            "flux_motorise": 0.0,
            "pct_flux_velo_trott": 0.0,
            "pct_flux_motorise": 0.0,
            "pct_flux_voie_cyclable": 0.0,
        }
        for arr in ARRONDISSEMENTS
    }

    # Velib
    for doc in db["silver_velib"].find({"arrondissement": {"$in": ARRONDISSEMENTS}}):
        arr = doc.get("arrondissement")
        if arr not in result:
            continue
        result[arr]["nb_stations_velib"] += 1
        result[arr]["capacite_velib_totale"] += _as_float(doc.get("capacity"), 0.0)

    # Gares : dédoublonner la gare physique, mais mesurer aussi l'intensité de desserte.
    gares_uniques = {arr: set() for arr in ARRONDISSEMENTS}
    lignes_par_arr = {arr: set() for arr in ARRONDISSEMENTS}
    modes_lourds_par_arr = {arr: set() for arr in ARRONDISSEMENTS}
    for doc in db["silver_gares"].find({"arrondissement": {"$in": ARRONDISSEMENTS}}):
        arr = doc.get("arrondissement")
        if arr not in result:
            continue
        gare_id = doc.get("identifiant") or doc.get("nom")
        ligne = doc.get("ligne")
        mode = doc.get("mode_transport")
        if gare_id:
            gares_uniques[arr].add(str(gare_id))
        if ligne not in (None, ""):
            lignes_par_arr[arr].add(str(ligne))
        if _is_active_heavy_mode(mode):
            modes_lourds_par_arr[arr].add(_normalize_mode_label(mode))
        for flag_name, canonical in [
            ("termetro", "metro"),
            ("terrer", "rer"),
            ("tertram", "tram"),
            ("tertrain", "train"),
            ("terval", "val"),
        ]:
            if _is_truthy(doc.get(flag_name)):
                modes_lourds_par_arr[arr].add(canonical)

    for arr in ARRONDISSEMENTS:
        nb_gares = len(gares_uniques[arr])
        nb_lignes = len(lignes_par_arr[arr])
        result[arr]["nb_gares"] = nb_gares
        result[arr]["nb_lignes_transport"] = nb_lignes
        result[arr]["lignes_par_gare_moyen"] = round(nb_lignes / nb_gares, 2) if nb_gares else 0.0
        result[arr]["nb_modes_lourds"] = len(modes_lourds_par_arr[arr])

    # Arrêts : maillage fin + accessibilité
    arrets_total = {arr: 0 for arr in ARRONDISSEMENTS}
    arrets_accessibles = {arr: 0 for arr in ARRONDISSEMENTS}
    for doc in db["silver_bus"].find({"arrondissement": {"$in": ARRONDISSEMENTS}}):
        arr = doc.get("arrondissement")
        if arr not in result:
            continue
        arrets_total[arr] += 1
        if _is_truthy(doc.get("accessible")):
            arrets_accessibles[arr] += 1
    for arr in ARRONDISSEMENTS:
        result[arr]["nb_arrets_bus"] = arrets_total[arr]
        result[arr]["pct_arrets_accessibles"] = round(
            arrets_accessibles[arr] / arrets_total[arr] * 100, 2
        ) if arrets_total[arr] else 0.0

    # Comptages : intensité d'usage observée
    flux_collection = db["silver_voies"]
    if flux_collection.count_documents({}) == 0:
        flux_collection = db["silver_comptage_multimodal"]

    flux_total = {arr: 0.0 for arr in ARRONDISSEMENTS}
    flux_velo = {arr: 0.0 for arr in ARRONDISSEMENTS}
    flux_bus = {arr: 0.0 for arr in ARRONDISSEMENTS}
    flux_motorise = {arr: 0.0 for arr in ARRONDISSEMENTS}
    flux_cycle_way = {arr: 0.0 for arr in ARRONDISSEMENTS}

    for doc in flux_collection.find({"arrondissement": {"$in": ARRONDISSEMENTS}}):
        arr = doc.get("arrondissement")
        if arr not in result:
            continue
        volume = _as_float(doc.get("nb_usagers"), None)
        if volume is None:
            volume = _as_float(doc.get("q"), 1.0)
        mode_label = doc.get("mode")
        voie_label = doc.get("voie")

        flux_total[arr] += volume
        if _is_velo_or_trott_mode(mode_label):
            flux_velo[arr] += volume
        if _is_bus_mode(mode_label):
            flux_bus[arr] += volume
        if _is_motorized_mode(mode_label):
            flux_motorise[arr] += volume
        if _is_cycle_way(voie_label):
            flux_cycle_way[arr] += volume

    for arr in ARRONDISSEMENTS:
        total = flux_total[arr]
        result[arr]["flux_multimodal"] = int(round(total))
        result[arr]["flux_velo_trott"] = int(round(flux_velo[arr]))
        result[arr]["flux_bus"] = int(round(flux_bus[arr]))
        result[arr]["flux_motorise"] = int(round(flux_motorise[arr]))
        result[arr]["pct_flux_velo_trott"] = round(flux_velo[arr] / total * 100, 2) if total else 0.0
        result[arr]["pct_flux_motorise"] = round(flux_motorise[arr] / total * 100, 2) if total else 0.0
        result[arr]["pct_flux_voie_cyclable"] = round(flux_cycle_way[arr] / total * 100, 2) if total else 0.0

    # Offre
    s_velib_stations = minmax_normalize({a: result[a]["nb_stations_velib"] for a in ARRONDISSEMENTS})
    s_velib_cap = minmax_normalize({a: result[a]["capacite_velib_totale"] for a in ARRONDISSEMENTS})
    s_gares = minmax_normalize({a: result[a]["nb_gares"] for a in ARRONDISSEMENTS})
    s_lignes = minmax_normalize({a: result[a]["lignes_par_gare_moyen"] for a in ARRONDISSEMENTS})
    s_modes = minmax_normalize({a: result[a]["nb_modes_lourds"] for a in ARRONDISSEMENTS})
    s_arrets = minmax_normalize({a: result[a]["nb_arrets_bus"] for a in ARRONDISSEMENTS})
    s_access = minmax_normalize({a: result[a]["pct_arrets_accessibles"] for a in ARRONDISSEMENTS})

    score_offre = weighted_avg([
        (s_velib_stations, 1),
        (s_velib_cap, 2),
        (s_gares, 2),
        (s_lignes, 2),
        (s_modes, 1),
        (s_arrets, 2),
        (s_access, 1),
    ])

    # Intensité
    s_flux_total = minmax_normalize({a: result[a]["flux_multimodal"] for a in ARRONDISSEMENTS})
    s_flux_velo = minmax_normalize({a: result[a]["flux_velo_trott"] for a in ARRONDISSEMENTS})
    s_flux_bus = minmax_normalize({a: result[a]["flux_bus"] for a in ARRONDISSEMENTS})
    s_flux_cycle = minmax_normalize({a: result[a]["pct_flux_voie_cyclable"] for a in ARRONDISSEMENTS})
    s_flux_motor = invert(minmax_normalize({a: result[a]["pct_flux_motorise"] for a in ARRONDISSEMENTS}))

    score_intensite = weighted_avg([
        (s_flux_total, 2),
        (s_flux_velo, 1),
        (s_flux_bus, 1),
        (s_flux_cycle, 1),
        (s_flux_motor, 1),
    ])

    for arr in ARRONDISSEMENTS:
        result[arr]["score_transport_offre"] = score_offre[arr]
        result[arr]["score_transport_intensite"] = score_intensite[arr]
        result[arr]["score_transports"] = round(score_offre[arr] * 0.6 + score_intensite[arr] * 0.4, 2)

    return result


# Scoring Indicateur 3 — Loisirs
#
# Source          | Collection          | Champ  | Poids | Signe
# --------------- | ------------------- | ------ | ----- | -----
# Événements      | silver_evenements   | COUNT  |   2   | +
# Terrasses       | silver_terrasses    | COUNT  |   1   | +
# Cinémas         | silver_cinemas      | COUNT  |   1   | +
# Musées          | silver_musees       | COUNT  |   1   | +

def agg_loisirs(db) -> dict:
    evenements = count_by_arr(db["silver_evenements"])
    terrasses  = count_by_arr(db["silver_terrasses"])
    cinemas    = count_by_arr(db["silver_cinemas"])
    musees     = count_by_arr(db["silver_musees"])

    result = {}
    for arr in ARRONDISSEMENTS:
        result[arr] = {
            "nb_evenements": safe_get(evenements, arr),
            "nb_terrasses":  safe_get(terrasses, arr),
            "nb_cinemas":    safe_get(cinemas, arr),
            "nb_musees":     safe_get(musees, arr),
        }

    s_evt = minmax_normalize({a: result[a]["nb_evenements"] for a in ARRONDISSEMENTS})
    s_ter = minmax_normalize({a: result[a]["nb_terrasses"]  for a in ARRONDISSEMENTS})
    s_cin = minmax_normalize({a: result[a]["nb_cinemas"]    for a in ARRONDISSEMENTS})
    s_mus = minmax_normalize({a: result[a]["nb_musees"]     for a in ARRONDISSEMENTS})

    scores = weighted_avg([(s_evt, 2), (s_ter, 1), (s_cin, 1), (s_mus, 1)])
    for arr in ARRONDISSEMENTS:
        result[arr]["score_loisirs"] = scores[arr]

    return result


# Scoring Indicateur 4 — Services publics
#
# Source                  | Collection              | Champ  | Poids | Signe
# ----------------------- | ----------------------- | ------ | ----- | -----
# Écoles élémentaires     | silver_ecoles_elem      | COUNT  |   2   | +
# Maternelles             | silver_maternelles      | COUNT  |   2   | +
# Collèges                | silver_colleges         | COUNT  |   1   | +
# Bibliothèques           | silver_bibliotheques    | COUNT  |   2   | +
# Bureaux de poste        | silver_bureaux_poste    | COUNT  |   1   | +
# Enseignement supérieur  | silver_ensup            | COUNT  |   1   | +

def agg_services(db) -> dict:
    ecoles      = count_by_arr(db["silver_ecoles_elem"])
    maternelles = count_by_arr(db["silver_maternelles"])
    colleges    = count_by_arr(db["silver_colleges"])
    biblio      = count_by_arr(db["silver_bibliotheques"])
    poste       = count_by_arr(db["silver_bureaux_poste"])
    ensup       = count_by_arr(db["silver_ensup"])

    result = {}
    for arr in ARRONDISSEMENTS:
        result[arr] = {
            "nb_ecoles":        safe_get(ecoles, arr),
            "nb_maternelles":   safe_get(maternelles, arr),
            "nb_colleges":      safe_get(colleges, arr),
            "nb_bibliotheques": safe_get(biblio, arr),
            "nb_bureaux_poste": safe_get(poste, arr),
            "nb_ensup":         safe_get(ensup, arr),
        }

    s_eco = minmax_normalize({a: result[a]["nb_ecoles"]        for a in ARRONDISSEMENTS})
    s_mat = minmax_normalize({a: result[a]["nb_maternelles"]   for a in ARRONDISSEMENTS})
    s_col = minmax_normalize({a: result[a]["nb_colleges"]      for a in ARRONDISSEMENTS})
    s_bib = minmax_normalize({a: result[a]["nb_bibliotheques"] for a in ARRONDISSEMENTS})
    s_pos = minmax_normalize({a: result[a]["nb_bureaux_poste"] for a in ARRONDISSEMENTS})
    s_ens = minmax_normalize({a: result[a]["nb_ensup"]         for a in ARRONDISSEMENTS})

    scores = weighted_avg([(s_eco, 2), (s_mat, 2), (s_col, 1), (s_bib, 2), (s_pos, 1), (s_ens, 1)])
    for arr in ARRONDISSEMENTS:
        result[arr]["score_services"] = scores[arr]

    return result


def agg_immobilier(db, annee: int) -> dict:
    """Prix m² médian + logements sociaux + répartition par type/surface par arrondissement."""
    ls = count_by_arr(db["silver_logements_sociaux"])
    prix_by_arr  = {arr: [] for arr in ARRONDISSEMENTS}
    surfaces_by_arr = {arr: [] for arr in ARRONDISSEMENTS}
    type_counts  = {arr: {"Appartement": 0, "Maison": 0} for arr in ARRONDISSEMENTS}
    surf_buckets = {arr: {"t1": 0, "t2": 0, "t3": 0, "t4plus": 0} for arr in ARRONDISSEMENTS}

    available_years = sorted(v for v in db["silver_dvf"].distinct("annee") if isinstance(v, int))
    target_year = annee if annee in available_years else (max([y for y in available_years if y <= annee], default=None))

    if target_year is not None:
        dvf_query = {
            "annee": target_year,
            "arrondissement": {"$in": ARRONDISSEMENTS},
            "nature_mutation": "Vente",
            "type_local": {"$in": ["Appartement", "Maison"]},
            "prix_m2": {"$exists": True, "$ne": None},
        }
        for doc in db["silver_dvf"].find(dvf_query, {"arrondissement": 1, "prix_m2": 1, "type_local": 1, "surface_reelle_bati": 1}):
            arr  = doc.get("arrondissement")
            prix = _as_float(doc.get("prix_m2"), None)
            tl   = doc.get("type_local", "")
            surf = _as_float(doc.get("surface_reelle_bati"), None)

            if arr not in ARRONDISSEMENTS:
                continue
            if tl in ("Appartement", "Maison"):
                type_counts[arr][tl] += 1
            if prix and 500 <= prix <= 50_000 and tl == "Appartement":
                prix_by_arr[arr].append(prix)
            if surf and 5 <= surf <= 500:
                surfaces_by_arr[arr].append(surf)
                if surf <= 25:
                    surf_buckets[arr]["t1"] += 1
                elif surf <= 45:
                    surf_buckets[arr]["t2"] += 1
                elif surf <= 65:
                    surf_buckets[arr]["t3"] += 1
                else:
                    surf_buckets[arr]["t4plus"] += 1

    result = {}
    for arr in ARRONDISSEMENTS:
        nb_app  = type_counts[arr]["Appartement"]
        nb_mai  = type_counts[arr]["Maison"]
        nb_tot  = nb_app + nb_mai
        result[arr] = {
            "nb_logements_sociaux":  safe_get(ls, arr),
            "pct_logements_sociaux": None,
            "prix_m2_median":        _median(prix_by_arr[arr]),
            "surface_mediane":       _median(surfaces_by_arr[arr]),
            "nb_appartements":       nb_app,
            "nb_maisons":            nb_mai,
            "pct_appartements":      round(nb_app / nb_tot * 100, 1) if nb_tot > 0 else None,
            "nb_t1":                 surf_buckets[arr]["t1"],
            "nb_t2":                 surf_buckets[arr]["t2"],
            "nb_t3":                 surf_buckets[arr]["t3"],
            "nb_t4plus":             surf_buckets[arr]["t4plus"],
        }
    return result


def agg_quartiers(db, annee: int, arrondissement_kpis: dict) -> dict:
    """
    Recalcule les KPI à l'échelle des 80 quartiers administratifs.

    Logique :
    - toutes les sources Silver ponctuelles sont affectées à un quartier via `location`
    - les métriques purement agrégées au niveau arrondissement (ex. DVF) sont héritées
    - le score global reste la moyenne des 4 scores composites
    """
    quartiers, quartiers_by_id = _load_quartier_shapes()
    quartier_ids = [q["quartier_id"] for q in quartiers]
    if not quartier_ids:
        log.warning("  Impossible de calculer gold.quartier_kpis : aucun quartier chargé")
        return {}

    result = {
        qid: {
            "quartier_id": qid,
            "quartier_code": quartiers_by_id[qid]["quartier_code"],
            "arrondissement": quartiers_by_id[qid]["arrondissement"],
            "nom": quartiers_by_id[qid]["nom"],
            "prix_m2_median": None,
            "pct_logements_sociaux": None,
            "nb_logements_sociaux": 0,
            "score_qualite_vie": 0.0,
            "nb_espaces_verts": 0,
            "nb_arbres": 0,
            "score_air_no2": None,
            "score_air_pm25": None,
            "pct_fibre": 0.0,
            "nb_sanisettes": 0,
            "nb_chantiers_actifs": 0,
            "nb_anomalies": 0,
            "score_transports": 0.0,
            "score_transport_offre": 0.0,
            "score_transport_intensite": 0.0,
            "nb_gares": 0,
            "nb_stations_velib": 0,
            "capacite_velib_totale": 0.0,
            "nb_lignes_transport": 0,
            "lignes_par_gare_moyen": 0.0,
            "nb_modes_lourds": 0,
            "nb_arrets_bus": 0,
            "pct_arrets_accessibles": 0.0,
            "flux_multimodal": 0.0,
            "flux_velo_trott": 0.0,
            "flux_bus": 0.0,
            "flux_motorise": 0.0,
            "pct_flux_velo_trott": 0.0,
            "pct_flux_motorise": 0.0,
            "pct_flux_voie_cyclable": 0.0,
            "score_loisirs": 0.0,
            "nb_evenements": 0,
            "nb_cinemas": 0,
            "nb_terrasses": 0,
            "nb_musees": 0,
            "score_services": 0.0,
            "nb_ecoles": 0,
            "nb_maternelles": 0,
            "nb_colleges": 0,
            "nb_bibliotheques": 0,
            "nb_bureaux_poste": 0,
            "nb_ensup": 0,
            "revenu_median_uc": None,
            "taux_effort_achat": None,
            "surface_mediane": None,
            "nb_appartements": None,
            "nb_maisons": None,
            "pct_appartements": None,
            "nb_t1": None,
            "nb_t2": None,
            "nb_t3": None,
            "nb_t4plus": None,
            "score_global": 0.0,
        }
        for qid in quartier_ids
    }

    # Héritage arrondissement pour les métriques non distribuables précisément
    for qid in quartier_ids:
        arr = result[qid]["arrondissement"]
        arr_row = arrondissement_kpis.get(arr, {})
        result[qid]["prix_m2_median"] = arr_row.get("prix_m2_median")
        result[qid]["pct_logements_sociaux"] = arr_row.get("pct_logements_sociaux")
        result[qid]["score_air_no2"] = arr_row.get("score_air_no2")
        result[qid]["score_air_pm25"] = arr_row.get("score_air_pm25")
        result[qid]["revenu_median_uc"]  = arr_row.get("revenu_median_uc")
        result[qid]["taux_effort_achat"] = arr_row.get("taux_effort_achat")
        result[qid]["surface_mediane"]   = arr_row.get("surface_mediane")
        result[qid]["nb_appartements"]   = arr_row.get("nb_appartements")
        result[qid]["nb_maisons"]        = arr_row.get("nb_maisons")
        result[qid]["pct_appartements"]  = arr_row.get("pct_appartements")
        result[qid]["nb_t1"]             = arr_row.get("nb_t1")
        result[qid]["nb_t2"]             = arr_row.get("nb_t2")
        result[qid]["nb_t3"]             = arr_row.get("nb_t3")
        result[qid]["nb_t4plus"]         = arr_row.get("nb_t4plus")

    # Immobilier fin : médiane réelle du prix m² au niveau quartier si disponible
    available_years = sorted(v for v in db["silver_dvf"].distinct("annee") if isinstance(v, int))
    target_year = annee if annee in available_years else (max([y for y in available_years if y <= annee], default=None))
    if target_year is not None:
        prix_by_quartier = {qid: [] for qid in quartier_ids}
        dvf_query = {
            "annee": target_year,
            "nature_mutation": "Vente",
            "type_local": "Appartement",
            "prix_m2": {"$exists": True, "$ne": None},
        }
        for qid, doc in _iter_quartier_docs(
            db["silver_dvf"],
            dvf_query,
            {"quartier_id": 1, "location": 1, "prix_m2": 1},
            quartier_ids,
        ):
            prix = _as_float(doc.get("prix_m2"), None)
            if prix and 500 <= prix <= 50_000:
                prix_by_quartier[qid].append(prix)
        for qid in quartier_ids:
            prix_quartier = _median(prix_by_quartier[qid])
            if prix_quartier is not None:
                result[qid]["prix_m2_median"] = prix_quartier

    # Qualité de vie
    for field, coll_name in [
        ("nb_espaces_verts", "silver_espaces_verts"),
        ("nb_arbres", "silver_arbres"),
        ("nb_sanisettes", "silver_sanisettes"),
        ("nb_chantiers_actifs", "silver_chantiers"),
        ("nb_anomalies", "silver_anomalies"),
    ]:
        counts = _count_by_quartier(db[coll_name], quartier_ids)
        for qid in quartier_ids:
            result[qid][field] = counts[qid]

    fibre_total = {qid: 0 for qid in quartier_ids}
    fibre_deployes = {qid: 0 for qid in quartier_ids}
    for qid, doc in _iter_quartier_docs(
        db["silver_fibre_imb"],
        {},
        {"quartier_id": 1, "location": 1, "statut_immeuble": 1},
        quartier_ids,
    ):
        fibre_total[qid] += 1
        if re.search(r"d[ée]ploy", str(doc.get("statut_immeuble", "")), flags=re.IGNORECASE):
            fibre_deployes[qid] += 1
    for qid in quartier_ids:
        total = fibre_total[qid]
        result[qid]["pct_fibre"] = round(fibre_deployes[qid] / total * 100, 1) if total else 0.0

    air_no2 = {
        qid: min(_as_float(result[qid]["score_air_no2"], 0.0), 100.0)
        for qid in quartier_ids
    }

    s_ev = minmax_normalize({qid: result[qid]["nb_espaces_verts"] for qid in quartier_ids})
    s_arb = minmax_normalize({qid: result[qid]["nb_arbres"] for qid in quartier_ids})
    s_san = minmax_normalize({qid: result[qid]["nb_sanisettes"] for qid in quartier_ids})
    s_fib = minmax_normalize({qid: result[qid]["pct_fibre"] for qid in quartier_ids})
    s_cha = invert(minmax_normalize({qid: result[qid]["nb_chantiers_actifs"] for qid in quartier_ids}))
    s_ano = invert(minmax_normalize({qid: result[qid]["nb_anomalies"] for qid in quartier_ids}))
    s_air = invert(air_no2)

    score_qv = weighted_avg([
        (s_ev, 2),
        (s_arb, 1),
        (s_san, 1),
        (s_fib, 1),
        (s_cha, 1),
        (s_ano, 1),
        (s_air, 1),
    ], zone_keys=quartier_ids)
    for qid in quartier_ids:
        result[qid]["score_qualite_vie"] = score_qv[qid]

    # Transports
    gares_uniques = {qid: set() for qid in quartier_ids}
    lignes_par_quartier = {qid: set() for qid in quartier_ids}
    modes_lourds_par_quartier = {qid: set() for qid in quartier_ids}
    arrets_total = {qid: 0 for qid in quartier_ids}
    arrets_accessibles = {qid: 0 for qid in quartier_ids}
    flux_total = {qid: 0.0 for qid in quartier_ids}
    flux_velo = {qid: 0.0 for qid in quartier_ids}
    flux_bus = {qid: 0.0 for qid in quartier_ids}
    flux_motorise = {qid: 0.0 for qid in quartier_ids}
    flux_cycle_way = {qid: 0.0 for qid in quartier_ids}

    for qid, doc in _iter_quartier_docs(
        db["silver_velib"],
        {},
        {"quartier_id": 1, "location": 1, "capacity": 1},
        quartier_ids,
    ):
        result[qid]["nb_stations_velib"] += 1
        result[qid]["capacite_velib_totale"] += _as_float(doc.get("capacity"), 0.0)

    for qid, doc in _iter_quartier_docs(
        db["silver_gares"],
        {},
        {
            "quartier_id": 1, "location": 1, "identifiant": 1, "nom": 1,
            "ligne": 1, "mode_transport": 1, "termetro": 1, "terrer": 1,
            "tertram": 1, "tertrain": 1, "terval": 1,
        },
        quartier_ids,
    ):
        gare_id = doc.get("identifiant") or doc.get("nom")
        ligne = doc.get("ligne")
        mode = doc.get("mode_transport")
        if gare_id not in (None, ""):
            gares_uniques[qid].add(str(gare_id))
        if ligne not in (None, ""):
            lignes_par_quartier[qid].add(str(ligne))
        if _is_active_heavy_mode(mode):
            modes_lourds_par_quartier[qid].add(_normalize_mode_label(mode))
        for flag_name, canonical in [
            ("termetro", "metro"),
            ("terrer", "rer"),
            ("tertram", "tram"),
            ("tertrain", "train"),
            ("terval", "val"),
        ]:
            if _is_truthy(doc.get(flag_name)):
                modes_lourds_par_quartier[qid].add(canonical)

    for qid in quartier_ids:
        nb_gares = len(gares_uniques[qid])
        nb_lignes = len(lignes_par_quartier[qid])
        result[qid]["nb_gares"] = nb_gares
        result[qid]["nb_lignes_transport"] = nb_lignes
        result[qid]["lignes_par_gare_moyen"] = round(nb_lignes / nb_gares, 2) if nb_gares else 0.0
        result[qid]["nb_modes_lourds"] = len(modes_lourds_par_quartier[qid])

    for qid, doc in _iter_quartier_docs(
        db["silver_bus"],
        {},
        {"quartier_id": 1, "location": 1, "accessible": 1},
        quartier_ids,
    ):
        arrets_total[qid] += 1
        if _is_truthy(doc.get("accessible")):
            arrets_accessibles[qid] += 1
    for qid in quartier_ids:
        result[qid]["nb_arrets_bus"] = arrets_total[qid]
        result[qid]["pct_arrets_accessibles"] = round(
            arrets_accessibles[qid] / arrets_total[qid] * 100, 2
        ) if arrets_total[qid] else 0.0

    flux_collection = db["silver_voies"]
    if flux_collection.count_documents({}) == 0:
        flux_collection = db["silver_comptage_multimodal"]

    for qid, doc in _iter_quartier_docs(
        flux_collection,
        {},
        {"quartier_id": 1, "location": 1, "nb_usagers": 1, "q": 1, "mode": 1, "voie": 1},
        quartier_ids,
    ):
        volume = _as_float(doc.get("nb_usagers"), None)
        if volume is None:
            volume = _as_float(doc.get("q"), 1.0)
        mode_label = doc.get("mode")
        voie_label = doc.get("voie")

        flux_total[qid] += volume
        if _is_velo_or_trott_mode(mode_label):
            flux_velo[qid] += volume
        if _is_bus_mode(mode_label):
            flux_bus[qid] += volume
        if _is_motorized_mode(mode_label):
            flux_motorise[qid] += volume
        if _is_cycle_way(voie_label):
            flux_cycle_way[qid] += volume

    for qid in quartier_ids:
        total = flux_total[qid]
        result[qid]["flux_multimodal"] = int(round(total))
        result[qid]["flux_velo_trott"] = int(round(flux_velo[qid]))
        result[qid]["flux_bus"] = int(round(flux_bus[qid]))
        result[qid]["flux_motorise"] = int(round(flux_motorise[qid]))
        result[qid]["pct_flux_velo_trott"] = round(flux_velo[qid] / total * 100, 2) if total else 0.0
        result[qid]["pct_flux_motorise"] = round(flux_motorise[qid] / total * 100, 2) if total else 0.0
        result[qid]["pct_flux_voie_cyclable"] = round(flux_cycle_way[qid] / total * 100, 2) if total else 0.0

    s_velib_stations = minmax_normalize({qid: result[qid]["nb_stations_velib"] for qid in quartier_ids})
    s_velib_cap = minmax_normalize({qid: result[qid]["capacite_velib_totale"] for qid in quartier_ids})
    s_gares = minmax_normalize({qid: result[qid]["nb_gares"] for qid in quartier_ids})
    s_lignes = minmax_normalize({qid: result[qid]["lignes_par_gare_moyen"] for qid in quartier_ids})
    s_modes = minmax_normalize({qid: result[qid]["nb_modes_lourds"] for qid in quartier_ids})
    s_arrets = minmax_normalize({qid: result[qid]["nb_arrets_bus"] for qid in quartier_ids})
    s_access = minmax_normalize({qid: result[qid]["pct_arrets_accessibles"] for qid in quartier_ids})

    score_offre = weighted_avg([
        (s_velib_stations, 1),
        (s_velib_cap, 2),
        (s_gares, 2),
        (s_lignes, 2),
        (s_modes, 1),
        (s_arrets, 2),
        (s_access, 1),
    ], zone_keys=quartier_ids)

    s_flux_total = minmax_normalize({qid: result[qid]["flux_multimodal"] for qid in quartier_ids})
    s_flux_velo = minmax_normalize({qid: result[qid]["flux_velo_trott"] for qid in quartier_ids})
    s_flux_bus = minmax_normalize({qid: result[qid]["flux_bus"] for qid in quartier_ids})
    s_flux_cycle = minmax_normalize({qid: result[qid]["pct_flux_voie_cyclable"] for qid in quartier_ids})
    s_flux_motor = invert(minmax_normalize({qid: result[qid]["pct_flux_motorise"] for qid in quartier_ids}))

    score_intensite = weighted_avg([
        (s_flux_total, 2),
        (s_flux_velo, 1),
        (s_flux_bus, 1),
        (s_flux_cycle, 1),
        (s_flux_motor, 1),
    ], zone_keys=quartier_ids)

    for qid in quartier_ids:
        result[qid]["score_transport_offre"] = score_offre[qid]
        result[qid]["score_transport_intensite"] = score_intensite[qid]
        result[qid]["score_transports"] = round(score_offre[qid] * 0.6 + score_intensite[qid] * 0.4, 2)

    # Loisirs
    for field, coll_name in [
        ("nb_evenements", "silver_evenements"),
        ("nb_terrasses", "silver_terrasses"),
        ("nb_cinemas", "silver_cinemas"),
        ("nb_musees", "silver_musees"),
    ]:
        counts = _count_by_quartier(db[coll_name], quartier_ids)
        for qid in quartier_ids:
            result[qid][field] = counts[qid]

    s_evt = minmax_normalize({qid: result[qid]["nb_evenements"] for qid in quartier_ids})
    s_ter = minmax_normalize({qid: result[qid]["nb_terrasses"] for qid in quartier_ids})
    s_cin = minmax_normalize({qid: result[qid]["nb_cinemas"] for qid in quartier_ids})
    s_mus = minmax_normalize({qid: result[qid]["nb_musees"] for qid in quartier_ids})
    score_loisirs = weighted_avg([(s_evt, 2), (s_ter, 1), (s_cin, 1), (s_mus, 1)], zone_keys=quartier_ids)
    for qid in quartier_ids:
        result[qid]["score_loisirs"] = score_loisirs[qid]

    # Services publics
    for field, coll_name in [
        ("nb_ecoles", "silver_ecoles_elem"),
        ("nb_maternelles", "silver_maternelles"),
        ("nb_colleges", "silver_colleges"),
        ("nb_bibliotheques", "silver_bibliotheques"),
        ("nb_bureaux_poste", "silver_bureaux_poste"),
        ("nb_ensup", "silver_ensup"),
        ("nb_logements_sociaux", "silver_logements_sociaux"),
    ]:
        counts = _count_by_quartier(db[coll_name], quartier_ids)
        for qid in quartier_ids:
            result[qid][field] = counts[qid]

    s_eco = minmax_normalize({qid: result[qid]["nb_ecoles"] for qid in quartier_ids})
    s_mat = minmax_normalize({qid: result[qid]["nb_maternelles"] for qid in quartier_ids})
    s_col = minmax_normalize({qid: result[qid]["nb_colleges"] for qid in quartier_ids})
    s_bib = minmax_normalize({qid: result[qid]["nb_bibliotheques"] for qid in quartier_ids})
    s_pos = minmax_normalize({qid: result[qid]["nb_bureaux_poste"] for qid in quartier_ids})
    s_ens = minmax_normalize({qid: result[qid]["nb_ensup"] for qid in quartier_ids})
    score_services = weighted_avg([
        (s_eco, 2),
        (s_mat, 2),
        (s_col, 1),
        (s_bib, 2),
        (s_pos, 1),
        (s_ens, 1),
    ], zone_keys=quartier_ids)
    for qid in quartier_ids:
        result[qid]["score_services"] = score_services[qid]

    for qid in quartier_ids:
        s_qv = result[qid]["score_qualite_vie"] or 0
        s_tr = result[qid]["score_transports"] or 0
        s_lo = result[qid]["score_loisirs"] or 0
        s_sv = result[qid]["score_services"] or 0
        result[qid]["score_global"] = round((s_qv + s_tr + s_lo + s_sv) / 4, 2)

    return result


def agg_iris(db, annee: int, arrondissement_kpis: dict) -> dict:
    """
    Recalcule les KPI à l'échelle des IRIS parisiens.

    Logique :
    - toutes les sources Silver ponctuelles sont affectées à un IRIS via `location`
    - les métriques purement agrégées au niveau arrondissement sont héritées par défaut
    - les métriques fines (DVF, points, flux) sont recalculées au niveau IRIS
    """
    iris_shapes, iris_by_id = _load_iris_shapes()
    iris_ids = [iris["iris_id"] for iris in iris_shapes]
    if not iris_ids:
        log.warning("  Impossible de calculer gold.iris_kpis : aucun IRIS chargé")
        return {}

    result = {
        iris_id: {
            "iris_id": iris_id,
            "iris_code": iris_by_id[iris_id]["iris_code"],
            "quartier_id": iris_by_id[iris_id]["quartier_id"],
            "quartier_code": iris_by_id[iris_id]["quartier_code"],
            "arrondissement": iris_by_id[iris_id]["arrondissement"],
            "nom": iris_by_id[iris_id]["nom"],
            "iris_type": iris_by_id[iris_id]["iris_type"],
            "prix_m2_median": None,
            "pct_logements_sociaux": None,
            "nb_logements_sociaux": 0,
            "score_qualite_vie": 0.0,
            "nb_espaces_verts": 0,
            "nb_arbres": 0,
            "score_air_no2": None,
            "score_air_pm25": None,
            "pct_fibre": 0.0,
            "nb_sanisettes": 0,
            "nb_chantiers_actifs": 0,
            "nb_anomalies": 0,
            "score_transports": 0.0,
            "score_transport_offre": 0.0,
            "score_transport_intensite": 0.0,
            "nb_gares": 0,
            "nb_stations_velib": 0,
            "capacite_velib_totale": 0.0,
            "nb_lignes_transport": 0,
            "lignes_par_gare_moyen": 0.0,
            "nb_modes_lourds": 0,
            "nb_arrets_bus": 0,
            "pct_arrets_accessibles": 0.0,
            "flux_multimodal": 0.0,
            "flux_velo_trott": 0.0,
            "flux_bus": 0.0,
            "flux_motorise": 0.0,
            "pct_flux_velo_trott": 0.0,
            "pct_flux_motorise": 0.0,
            "pct_flux_voie_cyclable": 0.0,
            "score_loisirs": 0.0,
            "nb_evenements": 0,
            "nb_cinemas": 0,
            "nb_terrasses": 0,
            "nb_musees": 0,
            "score_services": 0.0,
            "nb_ecoles": 0,
            "nb_maternelles": 0,
            "nb_colleges": 0,
            "nb_bibliotheques": 0,
            "nb_bureaux_poste": 0,
            "nb_ensup": 0,
            "revenu_median_uc": None,
            "taux_effort_achat": None,
            "surface_mediane": None,
            "nb_appartements": None,
            "nb_maisons": None,
            "pct_appartements": None,
            "nb_t1": None,
            "nb_t2": None,
            "nb_t3": None,
            "nb_t4plus": None,
            "score_global": 0.0,
        }
        for iris_id in iris_ids
    }

    for iris_id in iris_ids:
        arr = result[iris_id]["arrondissement"]
        arr_row = arrondissement_kpis.get(arr, {})
        result[iris_id]["prix_m2_median"] = arr_row.get("prix_m2_median")
        result[iris_id]["pct_logements_sociaux"] = arr_row.get("pct_logements_sociaux")
        result[iris_id]["score_air_no2"] = arr_row.get("score_air_no2")
        result[iris_id]["score_air_pm25"] = arr_row.get("score_air_pm25")
        result[iris_id]["revenu_median_uc"] = arr_row.get("revenu_median_uc")
        result[iris_id]["taux_effort_achat"] = arr_row.get("taux_effort_achat")
        result[iris_id]["surface_mediane"] = arr_row.get("surface_mediane")
        result[iris_id]["nb_appartements"] = arr_row.get("nb_appartements")
        result[iris_id]["nb_maisons"] = arr_row.get("nb_maisons")
        result[iris_id]["pct_appartements"] = arr_row.get("pct_appartements")
        result[iris_id]["nb_t1"] = arr_row.get("nb_t1")
        result[iris_id]["nb_t2"] = arr_row.get("nb_t2")
        result[iris_id]["nb_t3"] = arr_row.get("nb_t3")
        result[iris_id]["nb_t4plus"] = arr_row.get("nb_t4plus")

    available_years = sorted(v for v in db["silver_dvf"].distinct("annee") if isinstance(v, int))
    target_year = annee if annee in available_years else (max([y for y in available_years if y <= annee], default=None))
    if target_year is not None:
        prix_by_iris = {iris_id: [] for iris_id in iris_ids}
        dvf_query = {
            "annee": target_year,
            "nature_mutation": "Vente",
            "type_local": "Appartement",
            "prix_m2": {"$exists": True, "$ne": None},
        }
        for iris_id, doc in _iter_iris_docs(
            db["silver_dvf"],
            dvf_query,
            {"iris_id": 1, "location": 1, "prix_m2": 1},
            iris_ids,
        ):
            prix = _as_float(doc.get("prix_m2"), None)
            if prix and 500 <= prix <= 50_000:
                prix_by_iris[iris_id].append(prix)
        for iris_id in iris_ids:
            prix_iris = _median(prix_by_iris[iris_id])
            if prix_iris is not None:
                result[iris_id]["prix_m2_median"] = prix_iris

    for field, coll_name in [
        ("nb_espaces_verts", "silver_espaces_verts"),
        ("nb_arbres", "silver_arbres"),
        ("nb_sanisettes", "silver_sanisettes"),
        ("nb_chantiers_actifs", "silver_chantiers"),
        ("nb_anomalies", "silver_anomalies"),
    ]:
        counts = _count_by_iris(db[coll_name], iris_ids)
        for iris_id in iris_ids:
            result[iris_id][field] = counts[iris_id]

    fibre_total = {iris_id: 0 for iris_id in iris_ids}
    fibre_deployes = {iris_id: 0 for iris_id in iris_ids}
    for iris_id, doc in _iter_iris_docs(
        db["silver_fibre_imb"],
        {},
        {"iris_id": 1, "location": 1, "statut_immeuble": 1},
        iris_ids,
    ):
        fibre_total[iris_id] += 1
        if re.search(r"d[ée]ploy", str(doc.get("statut_immeuble", "")), flags=re.IGNORECASE):
            fibre_deployes[iris_id] += 1
    for iris_id in iris_ids:
        total = fibre_total[iris_id]
        result[iris_id]["pct_fibre"] = round(fibre_deployes[iris_id] / total * 100, 1) if total else 0.0

    air_no2 = {iris_id: min(_as_float(result[iris_id]["score_air_no2"], 0.0), 100.0) for iris_id in iris_ids}

    s_ev = minmax_normalize({iris_id: result[iris_id]["nb_espaces_verts"] for iris_id in iris_ids})
    s_arb = minmax_normalize({iris_id: result[iris_id]["nb_arbres"] for iris_id in iris_ids})
    s_san = minmax_normalize({iris_id: result[iris_id]["nb_sanisettes"] for iris_id in iris_ids})
    s_fib = minmax_normalize({iris_id: result[iris_id]["pct_fibre"] for iris_id in iris_ids})
    s_cha = invert(minmax_normalize({iris_id: result[iris_id]["nb_chantiers_actifs"] for iris_id in iris_ids}))
    s_ano = invert(minmax_normalize({iris_id: result[iris_id]["nb_anomalies"] for iris_id in iris_ids}))
    s_air = invert(air_no2)

    score_qv = weighted_avg([
        (s_ev, 2),
        (s_arb, 1),
        (s_san, 1),
        (s_fib, 1),
        (s_cha, 1),
        (s_ano, 1),
        (s_air, 1),
    ], zone_keys=iris_ids)
    for iris_id in iris_ids:
        result[iris_id]["score_qualite_vie"] = score_qv[iris_id]

    gares_uniques = {iris_id: set() for iris_id in iris_ids}
    lignes_par_iris = {iris_id: set() for iris_id in iris_ids}
    modes_lourds_par_iris = {iris_id: set() for iris_id in iris_ids}
    arrets_total = {iris_id: 0 for iris_id in iris_ids}
    arrets_accessibles = {iris_id: 0 for iris_id in iris_ids}
    flux_total = {iris_id: 0.0 for iris_id in iris_ids}
    flux_velo = {iris_id: 0.0 for iris_id in iris_ids}
    flux_bus = {iris_id: 0.0 for iris_id in iris_ids}
    flux_motorise = {iris_id: 0.0 for iris_id in iris_ids}
    flux_cycle_way = {iris_id: 0.0 for iris_id in iris_ids}

    for iris_id, doc in _iter_iris_docs(
        db["silver_velib"],
        {},
        {"iris_id": 1, "location": 1, "capacity": 1},
        iris_ids,
    ):
        result[iris_id]["nb_stations_velib"] += 1
        result[iris_id]["capacite_velib_totale"] += _as_float(doc.get("capacity"), 0.0)

    for iris_id, doc in _iter_iris_docs(
        db["silver_gares"],
        {},
        {
            "iris_id": 1, "location": 1, "identifiant": 1, "nom": 1,
            "ligne": 1, "mode_transport": 1, "termetro": 1, "terrer": 1,
            "tertram": 1, "tertrain": 1, "terval": 1,
        },
        iris_ids,
    ):
        gare_id = doc.get("identifiant") or doc.get("nom")
        ligne = doc.get("ligne")
        mode = doc.get("mode_transport")
        if gare_id not in (None, ""):
            gares_uniques[iris_id].add(str(gare_id))
        if ligne not in (None, ""):
            lignes_par_iris[iris_id].add(str(ligne))
        if _is_active_heavy_mode(mode):
            modes_lourds_par_iris[iris_id].add(_normalize_mode_label(mode))
        for flag_name, canonical in [
            ("termetro", "metro"),
            ("terrer", "rer"),
            ("tertram", "tram"),
            ("tertrain", "train"),
            ("terval", "val"),
        ]:
            if _is_truthy(doc.get(flag_name)):
                modes_lourds_par_iris[iris_id].add(canonical)

    for iris_id in iris_ids:
        nb_gares = len(gares_uniques[iris_id])
        nb_lignes = len(lignes_par_iris[iris_id])
        result[iris_id]["nb_gares"] = nb_gares
        result[iris_id]["nb_lignes_transport"] = nb_lignes
        result[iris_id]["lignes_par_gare_moyen"] = round(nb_lignes / nb_gares, 2) if nb_gares else 0.0
        result[iris_id]["nb_modes_lourds"] = len(modes_lourds_par_iris[iris_id])

    for iris_id, doc in _iter_iris_docs(
        db["silver_bus"],
        {},
        {"iris_id": 1, "location": 1, "accessible": 1},
        iris_ids,
    ):
        arrets_total[iris_id] += 1
        if _is_truthy(doc.get("accessible")):
            arrets_accessibles[iris_id] += 1
    for iris_id in iris_ids:
        result[iris_id]["nb_arrets_bus"] = arrets_total[iris_id]
        result[iris_id]["pct_arrets_accessibles"] = round(
            arrets_accessibles[iris_id] / arrets_total[iris_id] * 100, 2
        ) if arrets_total[iris_id] else 0.0

    flux_collection = db["silver_voies"]
    if flux_collection.count_documents({}) == 0:
        flux_collection = db["silver_comptage_multimodal"]

    for iris_id, doc in _iter_iris_docs(
        flux_collection,
        {},
        {"iris_id": 1, "location": 1, "nb_usagers": 1, "q": 1, "mode": 1, "voie": 1},
        iris_ids,
    ):
        volume = _as_float(doc.get("nb_usagers"), None)
        if volume is None:
            volume = _as_float(doc.get("q"), 1.0)
        mode_label = doc.get("mode")
        voie_label = doc.get("voie")

        flux_total[iris_id] += volume
        if _is_velo_or_trott_mode(mode_label):
            flux_velo[iris_id] += volume
        if _is_bus_mode(mode_label):
            flux_bus[iris_id] += volume
        if _is_motorized_mode(mode_label):
            flux_motorise[iris_id] += volume
        if _is_cycle_way(voie_label):
            flux_cycle_way[iris_id] += volume

    for iris_id in iris_ids:
        total = flux_total[iris_id]
        result[iris_id]["flux_multimodal"] = int(round(total))
        result[iris_id]["flux_velo_trott"] = int(round(flux_velo[iris_id]))
        result[iris_id]["flux_bus"] = int(round(flux_bus[iris_id]))
        result[iris_id]["flux_motorise"] = int(round(flux_motorise[iris_id]))
        result[iris_id]["pct_flux_velo_trott"] = round(flux_velo[iris_id] / total * 100, 2) if total else 0.0
        result[iris_id]["pct_flux_motorise"] = round(flux_motorise[iris_id] / total * 100, 2) if total else 0.0
        result[iris_id]["pct_flux_voie_cyclable"] = round(flux_cycle_way[iris_id] / total * 100, 2) if total else 0.0

    s_velib_stations = minmax_normalize({iris_id: result[iris_id]["nb_stations_velib"] for iris_id in iris_ids})
    s_velib_cap = minmax_normalize({iris_id: result[iris_id]["capacite_velib_totale"] for iris_id in iris_ids})
    s_gares = minmax_normalize({iris_id: result[iris_id]["nb_gares"] for iris_id in iris_ids})
    s_lignes = minmax_normalize({iris_id: result[iris_id]["lignes_par_gare_moyen"] for iris_id in iris_ids})
    s_modes = minmax_normalize({iris_id: result[iris_id]["nb_modes_lourds"] for iris_id in iris_ids})
    s_arrets = minmax_normalize({iris_id: result[iris_id]["nb_arrets_bus"] for iris_id in iris_ids})
    s_access = minmax_normalize({iris_id: result[iris_id]["pct_arrets_accessibles"] for iris_id in iris_ids})

    score_offre = weighted_avg([
        (s_velib_stations, 1),
        (s_velib_cap, 2),
        (s_gares, 2),
        (s_lignes, 2),
        (s_modes, 1),
        (s_arrets, 2),
        (s_access, 1),
    ], zone_keys=iris_ids)

    s_flux_total = minmax_normalize({iris_id: result[iris_id]["flux_multimodal"] for iris_id in iris_ids})
    s_flux_velo = minmax_normalize({iris_id: result[iris_id]["flux_velo_trott"] for iris_id in iris_ids})
    s_flux_bus = minmax_normalize({iris_id: result[iris_id]["flux_bus"] for iris_id in iris_ids})
    s_flux_cycle = minmax_normalize({iris_id: result[iris_id]["pct_flux_voie_cyclable"] for iris_id in iris_ids})
    s_flux_motor = invert(minmax_normalize({iris_id: result[iris_id]["pct_flux_motorise"] for iris_id in iris_ids}))

    score_intensite = weighted_avg([
        (s_flux_total, 2),
        (s_flux_velo, 1),
        (s_flux_bus, 1),
        (s_flux_cycle, 1),
        (s_flux_motor, 1),
    ], zone_keys=iris_ids)

    for iris_id in iris_ids:
        result[iris_id]["score_transport_offre"] = score_offre[iris_id]
        result[iris_id]["score_transport_intensite"] = score_intensite[iris_id]
        result[iris_id]["score_transports"] = round(score_offre[iris_id] * 0.6 + score_intensite[iris_id] * 0.4, 2)

    for field, coll_name in [
        ("nb_evenements", "silver_evenements"),
        ("nb_terrasses", "silver_terrasses"),
        ("nb_cinemas", "silver_cinemas"),
        ("nb_musees", "silver_musees"),
    ]:
        counts = _count_by_iris(db[coll_name], iris_ids)
        for iris_id in iris_ids:
            result[iris_id][field] = counts[iris_id]

    s_evt = minmax_normalize({iris_id: result[iris_id]["nb_evenements"] for iris_id in iris_ids})
    s_ter = minmax_normalize({iris_id: result[iris_id]["nb_terrasses"] for iris_id in iris_ids})
    s_cin = minmax_normalize({iris_id: result[iris_id]["nb_cinemas"] for iris_id in iris_ids})
    s_mus = minmax_normalize({iris_id: result[iris_id]["nb_musees"] for iris_id in iris_ids})
    score_loisirs = weighted_avg([(s_evt, 2), (s_ter, 1), (s_cin, 1), (s_mus, 1)], zone_keys=iris_ids)
    for iris_id in iris_ids:
        result[iris_id]["score_loisirs"] = score_loisirs[iris_id]

    for field, coll_name in [
        ("nb_ecoles", "silver_ecoles_elem"),
        ("nb_maternelles", "silver_maternelles"),
        ("nb_colleges", "silver_colleges"),
        ("nb_bibliotheques", "silver_bibliotheques"),
        ("nb_bureaux_poste", "silver_bureaux_poste"),
        ("nb_ensup", "silver_ensup"),
        ("nb_logements_sociaux", "silver_logements_sociaux"),
    ]:
        counts = _count_by_iris(db[coll_name], iris_ids)
        for iris_id in iris_ids:
            result[iris_id][field] = counts[iris_id]

    s_eco = minmax_normalize({iris_id: result[iris_id]["nb_ecoles"] for iris_id in iris_ids})
    s_mat = minmax_normalize({iris_id: result[iris_id]["nb_maternelles"] for iris_id in iris_ids})
    s_col = minmax_normalize({iris_id: result[iris_id]["nb_colleges"] for iris_id in iris_ids})
    s_bib = minmax_normalize({iris_id: result[iris_id]["nb_bibliotheques"] for iris_id in iris_ids})
    s_pos = minmax_normalize({iris_id: result[iris_id]["nb_bureaux_poste"] for iris_id in iris_ids})
    s_ens = minmax_normalize({iris_id: result[iris_id]["nb_ensup"] for iris_id in iris_ids})
    score_services = weighted_avg([
        (s_eco, 2),
        (s_mat, 2),
        (s_col, 1),
        (s_bib, 2),
        (s_pos, 1),
        (s_ens, 1),
    ], zone_keys=iris_ids)
    for iris_id in iris_ids:
        result[iris_id]["score_services"] = score_services[iris_id]

    for iris_id in iris_ids:
        s_qv = result[iris_id]["score_qualite_vie"] or 0
        s_tr = result[iris_id]["score_transports"] or 0
        s_lo = result[iris_id]["score_loisirs"] or 0
        s_sv = result[iris_id]["score_services"] or 0
        result[iris_id]["score_global"] = round((s_qv + s_tr + s_lo + s_sv) / 4, 2)

    return result


# ─── Agrégation Revenus INSEE Filosofi ───────────────────────────────────────

def agg_revenus(db) -> dict:
    """
    Lit silver_revenus (20 docs, un par arrondissement) et retourne
    {arr: {revenu_median_uc: float}} — revenu médian annuel par UC en €.
    Source : INSEE Filosofi 2021, mesure MED_SL (EUR/an).
    """
    coll = db["silver_revenus"]
    result = {arr: {} for arr in ARRONDISSEMENTS}
    for doc in coll.find({"arrondissement": {"$in": ARRONDISSEMENTS}, "revenu_median_uc": {"$ne": None}}):
        arr = doc.get("arrondissement")
        val = doc.get("revenu_median_uc")
        if arr in result and val is not None:
            result[arr]["revenu_median_uc"] = round(float(val), 2)
    return result


def agg_revenus_quartier(db, quartier_by_arr: dict) -> dict:
    """Propage le revenu médian de l'arrondissement vers chaque quartier."""
    rev_arr = agg_revenus(db)
    result = {}
    for qid, meta in quartier_by_arr.items():
        arr = meta.get("arrondissement")
        result[qid] = rev_arr.get(arr, {}).copy()
    return result


# ─── Géométries arrondissements ───────────────────────────────────────────────

def load_arrondissements_geo(engine):
    """Télécharge et insère le GeoJSON des arrondissements depuis parisdata."""
    log.info(f"  Téléchargement GeoJSON arrondissements...")
    try:
        r = requests.get(ARRONDISSEMENTS_GEO_URL, timeout=30)
        r.raise_for_status()
        geojson = r.json()
    except Exception as e:
        log.warning(f"  Impossible de récupérer le GeoJSON arrondissements : {e}")
        return

    rows = []
    for feature in geojson.get("features", []):
        props = feature.get("properties", {})
        arr_num = None
        for k in ["c_ar", "c_arinsee", "l_ar", "arrondissement"]:
            if k in props:
                arr_num = _parse_arrondissement_number(props[k])
                if arr_num:
                    break
        if not arr_num or not (1 <= arr_num <= 20):
            continue
        import json as _json
        geom_str = _json.dumps(feature["geometry"])
        nom = props.get("l_ar", f"{arr_num}e arrondissement")
        rows.append((arr_num, nom, geom_str))

    if not rows:
        log.warning("  Aucune géométrie extraite du GeoJSON")
        return

    upsert_sql = text("""
        INSERT INTO gold.arrondissements_geo (arrondissement, nom, geom)
        VALUES (:arr, :nom, ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326))
        ON CONFLICT (arrondissement) DO UPDATE
          SET nom  = EXCLUDED.nom,
              geom = EXCLUDED.geom
    """)
    with engine.connect() as conn:
        for arr_num, nom, geom_str in rows:
            conn.execute(upsert_sql, {"arr": arr_num, "nom": nom, "geom": geom_str})
        conn.commit()
    log.info(f"  ✓ {len(rows)} géométries arrondissements upsertées")


def load_quartiers_geo(engine):
    """
    Télécharge et insère le GeoJSON des 80 quartiers administratifs.
    """
    log.info("  Téléchargement GeoJSON quartiers administratifs...")
    try:
        r = requests.get(QUARTIERS_GEO_URL, timeout=30)
        r.raise_for_status()
        geojson = r.json()
    except Exception as e:
        log.warning(f"  Impossible de récupérer le GeoJSON quartiers : {e}")
        return

    rows = []
    for feature in geojson.get("features", []):
        props = feature.get("properties", {})

        quartier_id = _first_present(props, [
            "n_sq_qu", "id_quartier", "quartier_id", "c_qu", "code_qu",
        ])
        quartier_code = _first_present(props, [
            "c_qu", "code_quartier", "code_qu", "quartier_code",
        ], quartier_id)
        nom = _first_present(props, [
            "l_qu", "nom_quart", "nom_quartier", "quartier", "nom",
        ])
        arr_raw = _first_present(props, [
            "c_ar", "arrondissement", "code_arrondissement", "c_arinsee", "arr_insee",
        ])
        arr_num = _parse_arrondissement_number(arr_raw)

        if not quartier_id:
            quartier_id = quartier_code or nom
        if not quartier_id or not nom or not feature.get("geometry"):
            continue

        geom_str = json.dumps(feature["geometry"])
        rows.append((str(quartier_id), str(quartier_code) if quartier_code is not None else None, arr_num, str(nom), geom_str))

    if not rows:
        log.warning("  Aucune géométrie quartier extraite du GeoJSON")
        return

    upsert_sql = text("""
        INSERT INTO gold.quartiers_geo (quartier_id, quartier_code, arrondissement, nom, geom)
        VALUES (:quartier_id, :quartier_code, :arrondissement, :nom, ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326))
        ON CONFLICT (quartier_id) DO UPDATE
          SET quartier_code  = EXCLUDED.quartier_code,
              arrondissement = EXCLUDED.arrondissement,
              nom            = EXCLUDED.nom,
              geom           = EXCLUDED.geom
    """)
    with engine.connect() as conn:
        for quartier_id, quartier_code, arr_num, nom, geom_str in rows:
            conn.execute(
                upsert_sql,
                {
                    "quartier_id": quartier_id,
                    "quartier_code": quartier_code,
                    "arrondissement": arr_num,
                    "nom": nom,
                    "geom": geom_str,
                },
            )
        conn.commit()
    log.info(f"  ✓ {len(rows)} géométries quartiers upsertées")


def load_iris_geo(engine):
    """Télécharge et insère le GeoJSON des IRIS parisiens."""
    log.info("  Téléchargement GeoJSON IRIS Paris...")
    try:
        r = requests.get(IRIS_GEO_URL, timeout=60)
        r.raise_for_status()
        geojson = r.json()
    except Exception as e:
        log.warning(f"  Impossible de récupérer le GeoJSON IRIS : {e}")
        return

    rows = []
    for feature in geojson.get("features", []):
        props = feature.get("properties", {})
        iris_id = _first_present(props, ["code_iris", "id"])
        iris_code = _first_present(props, ["code_iris"], iris_id)
        nom = _first_present(props, ["nom_iris", "nom"])
        iris_type = _first_present(props, ["typ_iris", "iris_type"])
        arr_num = _parse_arrondissement_number(_first_present(props, ["insee_com"], str(iris_code)[:5] if iris_code else None))
        if not iris_id or not nom or not feature.get("geometry"):
            continue

        center = props.get("geo_point_2d") or {}
        center_loc = None
        if isinstance(center, dict) and center.get("lon") is not None and center.get("lat") is not None:
            try:
                center_loc = {"type": "Point", "coordinates": [float(center["lon"]), float(center["lat"])]}
            except Exception:
                center_loc = None
        quartier = infer_quartier_from_location(center_loc) if center_loc else None
        if quartier is None:
            quartier = infer_quartier_from_location(_geometry_center_point(feature.get("geometry", {})))

        geom_str = json.dumps(feature["geometry"])
        rows.append((
            str(iris_id),
            str(iris_code) if iris_code is not None else str(iris_id),
            quartier["quartier_id"] if quartier else None,
            quartier["quartier_code"] if quartier else None,
            arr_num,
            str(nom),
            str(iris_type) if iris_type not in (None, "") else None,
            geom_str,
        ))

    if not rows:
        log.warning("  Aucune géométrie IRIS extraite du GeoJSON")
        return

    upsert_sql = text("""
        INSERT INTO gold.iris_geo (iris_id, iris_code, quartier_id, quartier_code, arrondissement, nom, iris_type, geom)
        VALUES (:iris_id, :iris_code, :quartier_id, :quartier_code, :arrondissement, :nom, :iris_type, ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326))
        ON CONFLICT (iris_id) DO UPDATE
          SET iris_code      = EXCLUDED.iris_code,
              quartier_id    = EXCLUDED.quartier_id,
              quartier_code  = EXCLUDED.quartier_code,
              arrondissement = EXCLUDED.arrondissement,
              nom            = EXCLUDED.nom,
              iris_type      = EXCLUDED.iris_type,
              geom           = EXCLUDED.geom
    """)
    with engine.connect() as conn:
        for iris_id, iris_code, quartier_id, quartier_code, arr_num, nom, iris_type, geom_str in rows:
            conn.execute(
                upsert_sql,
                {
                    "iris_id": iris_id,
                    "iris_code": iris_code,
                    "quartier_id": quartier_id,
                    "quartier_code": quartier_code,
                    "arrondissement": arr_num,
                    "nom": nom,
                    "iris_type": iris_type,
                    "geom": geom_str,
                },
            )
        conn.commit()
    log.info(f"  ✓ {len(rows)} géométries IRIS upsertées")


# ─── Upsert Gold ──────────────────────────────────────────────────────────────

def upsert_kpis(engine, kpis_by_arr: dict, annee: int):
    sql = text("""
        INSERT INTO gold.arrondissement_kpis (
            arrondissement, annee,
            prix_m2_median, pct_logements_sociaux, nb_logements_sociaux,
            revenu_median_uc, taux_effort_achat,
            surface_mediane, nb_appartements, nb_maisons, pct_appartements,
            nb_t1, nb_t2, nb_t3, nb_t4plus,
            score_qualite_vie, nb_espaces_verts, nb_arbres,
            score_air_no2, score_air_pm25, pct_fibre,
            nb_sanisettes, nb_chantiers_actifs, nb_anomalies,
            score_transports, score_transport_offre, score_transport_intensite,
            nb_gares, nb_stations_velib, capacite_velib_totale, nb_lignes_transport,
            lignes_par_gare_moyen, nb_modes_lourds, nb_arrets_bus, pct_arrets_accessibles,
            flux_multimodal, flux_velo_trott, flux_bus, flux_motorise,
            pct_flux_velo_trott, pct_flux_motorise, pct_flux_voie_cyclable,
            score_loisirs, nb_evenements, nb_cinemas, nb_terrasses, nb_musees,
            score_services, nb_ecoles, nb_maternelles, nb_colleges, nb_bibliotheques,
            nb_bureaux_poste, nb_ensup,
            score_global
        ) VALUES (
            :arrondissement, :annee,
            :prix_m2_median, :pct_logements_sociaux, :nb_logements_sociaux,
            :revenu_median_uc, :taux_effort_achat,
            :surface_mediane, :nb_appartements, :nb_maisons, :pct_appartements,
            :nb_t1, :nb_t2, :nb_t3, :nb_t4plus,
            :score_qualite_vie, :nb_espaces_verts, :nb_arbres,
            :score_air_no2, :score_air_pm25, :pct_fibre,
            :nb_sanisettes, :nb_chantiers_actifs, :nb_anomalies,
            :score_transports, :score_transport_offre, :score_transport_intensite,
            :nb_gares, :nb_stations_velib, :capacite_velib_totale, :nb_lignes_transport,
            :lignes_par_gare_moyen, :nb_modes_lourds, :nb_arrets_bus, :pct_arrets_accessibles,
            :flux_multimodal, :flux_velo_trott, :flux_bus, :flux_motorise,
            :pct_flux_velo_trott, :pct_flux_motorise, :pct_flux_voie_cyclable,
            :score_loisirs, :nb_evenements, :nb_cinemas, :nb_terrasses, :nb_musees,
            :score_services, :nb_ecoles, :nb_maternelles, :nb_colleges, :nb_bibliotheques,
            :nb_bureaux_poste, :nb_ensup,
            :score_global
        )
        ON CONFLICT (arrondissement, annee) DO UPDATE SET
            prix_m2_median        = EXCLUDED.prix_m2_median,
            pct_logements_sociaux = EXCLUDED.pct_logements_sociaux,
            nb_logements_sociaux  = EXCLUDED.nb_logements_sociaux,
            revenu_median_uc      = EXCLUDED.revenu_median_uc,
            taux_effort_achat     = EXCLUDED.taux_effort_achat,
            surface_mediane       = EXCLUDED.surface_mediane,
            nb_appartements       = EXCLUDED.nb_appartements,
            nb_maisons            = EXCLUDED.nb_maisons,
            pct_appartements      = EXCLUDED.pct_appartements,
            nb_t1                 = EXCLUDED.nb_t1,
            nb_t2                 = EXCLUDED.nb_t2,
            nb_t3                 = EXCLUDED.nb_t3,
            nb_t4plus             = EXCLUDED.nb_t4plus,
            score_qualite_vie     = EXCLUDED.score_qualite_vie,
            nb_espaces_verts      = EXCLUDED.nb_espaces_verts,
            nb_arbres             = EXCLUDED.nb_arbres,
            score_air_no2         = EXCLUDED.score_air_no2,
            score_air_pm25        = EXCLUDED.score_air_pm25,
            pct_fibre             = EXCLUDED.pct_fibre,
            nb_sanisettes         = EXCLUDED.nb_sanisettes,
            nb_chantiers_actifs   = EXCLUDED.nb_chantiers_actifs,
            nb_anomalies          = EXCLUDED.nb_anomalies,
            score_transports      = EXCLUDED.score_transports,
            score_transport_offre = EXCLUDED.score_transport_offre,
            score_transport_intensite = EXCLUDED.score_transport_intensite,
            nb_gares              = EXCLUDED.nb_gares,
            nb_stations_velib     = EXCLUDED.nb_stations_velib,
            capacite_velib_totale = EXCLUDED.capacite_velib_totale,
            nb_lignes_transport   = EXCLUDED.nb_lignes_transport,
            lignes_par_gare_moyen = EXCLUDED.lignes_par_gare_moyen,
            nb_modes_lourds       = EXCLUDED.nb_modes_lourds,
            nb_arrets_bus         = EXCLUDED.nb_arrets_bus,
            pct_arrets_accessibles = EXCLUDED.pct_arrets_accessibles,
            flux_multimodal       = EXCLUDED.flux_multimodal,
            flux_velo_trott       = EXCLUDED.flux_velo_trott,
            flux_bus              = EXCLUDED.flux_bus,
            flux_motorise         = EXCLUDED.flux_motorise,
            pct_flux_velo_trott   = EXCLUDED.pct_flux_velo_trott,
            pct_flux_motorise     = EXCLUDED.pct_flux_motorise,
            pct_flux_voie_cyclable = EXCLUDED.pct_flux_voie_cyclable,
            score_loisirs         = EXCLUDED.score_loisirs,
            nb_evenements         = EXCLUDED.nb_evenements,
            nb_cinemas            = EXCLUDED.nb_cinemas,
            nb_terrasses          = EXCLUDED.nb_terrasses,
            nb_musees             = EXCLUDED.nb_musees,
            score_services        = EXCLUDED.score_services,
            nb_ecoles             = EXCLUDED.nb_ecoles,
            nb_maternelles        = EXCLUDED.nb_maternelles,
            nb_colleges           = EXCLUDED.nb_colleges,
            nb_bibliotheques      = EXCLUDED.nb_bibliotheques,
            nb_bureaux_poste      = EXCLUDED.nb_bureaux_poste,
            nb_ensup              = EXCLUDED.nb_ensup,
            score_global          = EXCLUDED.score_global
    """)

    with engine.connect() as conn:
        for arr, kpis in kpis_by_arr.items():
            conn.execute(sql, {"arrondissement": arr, "annee": annee, **kpis})
        conn.commit()
    log.info(f"  ✓ {len(kpis_by_arr)} arrondissements upsertés (annee={annee})")


def upsert_quartier_kpis(engine, kpis_by_quartier: dict, annee: int):
    sql = text("""
        INSERT INTO gold.quartier_kpis (
            quartier_id, quartier_code, arrondissement, nom, annee,
            prix_m2_median, pct_logements_sociaux, nb_logements_sociaux,
            revenu_median_uc, taux_effort_achat,
            surface_mediane, nb_appartements, nb_maisons, pct_appartements,
            nb_t1, nb_t2, nb_t3, nb_t4plus,
            score_qualite_vie, nb_espaces_verts, nb_arbres,
            score_air_no2, score_air_pm25, pct_fibre,
            nb_sanisettes, nb_chantiers_actifs, nb_anomalies,
            score_transports, score_transport_offre, score_transport_intensite,
            nb_gares, nb_stations_velib, capacite_velib_totale, nb_lignes_transport,
            lignes_par_gare_moyen, nb_modes_lourds, nb_arrets_bus, pct_arrets_accessibles,
            flux_multimodal, flux_velo_trott, flux_bus, flux_motorise,
            pct_flux_velo_trott, pct_flux_motorise, pct_flux_voie_cyclable,
            score_loisirs, nb_evenements, nb_cinemas, nb_terrasses, nb_musees,
            score_services, nb_ecoles, nb_maternelles, nb_colleges, nb_bibliotheques,
            nb_bureaux_poste, nb_ensup,
            score_global
        ) VALUES (
            :quartier_id, :quartier_code, :arrondissement, :nom, :annee,
            :prix_m2_median, :pct_logements_sociaux, :nb_logements_sociaux,
            :revenu_median_uc, :taux_effort_achat,
            :surface_mediane, :nb_appartements, :nb_maisons, :pct_appartements,
            :nb_t1, :nb_t2, :nb_t3, :nb_t4plus,
            :score_qualite_vie, :nb_espaces_verts, :nb_arbres,
            :score_air_no2, :score_air_pm25, :pct_fibre,
            :nb_sanisettes, :nb_chantiers_actifs, :nb_anomalies,
            :score_transports, :score_transport_offre, :score_transport_intensite,
            :nb_gares, :nb_stations_velib, :capacite_velib_totale, :nb_lignes_transport,
            :lignes_par_gare_moyen, :nb_modes_lourds, :nb_arrets_bus, :pct_arrets_accessibles,
            :flux_multimodal, :flux_velo_trott, :flux_bus, :flux_motorise,
            :pct_flux_velo_trott, :pct_flux_motorise, :pct_flux_voie_cyclable,
            :score_loisirs, :nb_evenements, :nb_cinemas, :nb_terrasses, :nb_musees,
            :score_services, :nb_ecoles, :nb_maternelles, :nb_colleges, :nb_bibliotheques,
            :nb_bureaux_poste, :nb_ensup,
            :score_global
        )
        ON CONFLICT (quartier_id, annee) DO UPDATE SET
            quartier_code          = EXCLUDED.quartier_code,
            arrondissement         = EXCLUDED.arrondissement,
            nom                    = EXCLUDED.nom,
            prix_m2_median         = EXCLUDED.prix_m2_median,
            pct_logements_sociaux  = EXCLUDED.pct_logements_sociaux,
            nb_logements_sociaux   = EXCLUDED.nb_logements_sociaux,
            revenu_median_uc       = EXCLUDED.revenu_median_uc,
            taux_effort_achat      = EXCLUDED.taux_effort_achat,
            surface_mediane        = EXCLUDED.surface_mediane,
            nb_appartements        = EXCLUDED.nb_appartements,
            nb_maisons             = EXCLUDED.nb_maisons,
            pct_appartements       = EXCLUDED.pct_appartements,
            nb_t1                  = EXCLUDED.nb_t1,
            nb_t2                  = EXCLUDED.nb_t2,
            nb_t3                  = EXCLUDED.nb_t3,
            nb_t4plus              = EXCLUDED.nb_t4plus,
            score_qualite_vie      = EXCLUDED.score_qualite_vie,
            nb_espaces_verts       = EXCLUDED.nb_espaces_verts,
            nb_arbres              = EXCLUDED.nb_arbres,
            score_air_no2          = EXCLUDED.score_air_no2,
            score_air_pm25         = EXCLUDED.score_air_pm25,
            pct_fibre              = EXCLUDED.pct_fibre,
            nb_sanisettes          = EXCLUDED.nb_sanisettes,
            nb_chantiers_actifs    = EXCLUDED.nb_chantiers_actifs,
            nb_anomalies           = EXCLUDED.nb_anomalies,
            score_transports       = EXCLUDED.score_transports,
            score_transport_offre  = EXCLUDED.score_transport_offre,
            score_transport_intensite = EXCLUDED.score_transport_intensite,
            nb_gares               = EXCLUDED.nb_gares,
            nb_stations_velib      = EXCLUDED.nb_stations_velib,
            capacite_velib_totale  = EXCLUDED.capacite_velib_totale,
            nb_lignes_transport    = EXCLUDED.nb_lignes_transport,
            lignes_par_gare_moyen  = EXCLUDED.lignes_par_gare_moyen,
            nb_modes_lourds        = EXCLUDED.nb_modes_lourds,
            nb_arrets_bus          = EXCLUDED.nb_arrets_bus,
            pct_arrets_accessibles = EXCLUDED.pct_arrets_accessibles,
            flux_multimodal        = EXCLUDED.flux_multimodal,
            flux_velo_trott        = EXCLUDED.flux_velo_trott,
            flux_bus               = EXCLUDED.flux_bus,
            flux_motorise          = EXCLUDED.flux_motorise,
            pct_flux_velo_trott    = EXCLUDED.pct_flux_velo_trott,
            pct_flux_motorise      = EXCLUDED.pct_flux_motorise,
            pct_flux_voie_cyclable = EXCLUDED.pct_flux_voie_cyclable,
            score_loisirs          = EXCLUDED.score_loisirs,
            nb_evenements          = EXCLUDED.nb_evenements,
            nb_cinemas             = EXCLUDED.nb_cinemas,
            nb_terrasses           = EXCLUDED.nb_terrasses,
            nb_musees              = EXCLUDED.nb_musees,
            score_services         = EXCLUDED.score_services,
            nb_ecoles              = EXCLUDED.nb_ecoles,
            nb_maternelles         = EXCLUDED.nb_maternelles,
            nb_colleges            = EXCLUDED.nb_colleges,
            nb_bibliotheques       = EXCLUDED.nb_bibliotheques,
            nb_bureaux_poste       = EXCLUDED.nb_bureaux_poste,
            nb_ensup               = EXCLUDED.nb_ensup,
            score_global           = EXCLUDED.score_global
    """)

    with engine.connect() as conn:
        for quartier_id, kpis in kpis_by_quartier.items():
            conn.execute(sql, {"quartier_id": quartier_id, "annee": annee, **kpis})
        conn.commit()
    log.info(f"  ✓ {len(kpis_by_quartier)} quartiers upsertés (annee={annee})")


def upsert_iris_kpis(engine, kpis_by_iris: dict, annee: int):
    sql = text("""
        INSERT INTO gold.iris_kpis (
            iris_id, iris_code, quartier_id, quartier_code, arrondissement, nom, iris_type, annee,
            prix_m2_median, pct_logements_sociaux, nb_logements_sociaux,
            revenu_median_uc, taux_effort_achat,
            surface_mediane, nb_appartements, nb_maisons, pct_appartements,
            nb_t1, nb_t2, nb_t3, nb_t4plus,
            score_qualite_vie, nb_espaces_verts, nb_arbres,
            score_air_no2, score_air_pm25, pct_fibre,
            nb_sanisettes, nb_chantiers_actifs, nb_anomalies,
            score_transports, score_transport_offre, score_transport_intensite,
            nb_gares, nb_stations_velib, capacite_velib_totale, nb_lignes_transport,
            lignes_par_gare_moyen, nb_modes_lourds, nb_arrets_bus, pct_arrets_accessibles,
            flux_multimodal, flux_velo_trott, flux_bus, flux_motorise,
            pct_flux_velo_trott, pct_flux_motorise, pct_flux_voie_cyclable,
            score_loisirs, nb_evenements, nb_cinemas, nb_terrasses, nb_musees,
            score_services, nb_ecoles, nb_maternelles, nb_colleges, nb_bibliotheques,
            nb_bureaux_poste, nb_ensup,
            score_global
        ) VALUES (
            :iris_id, :iris_code, :quartier_id, :quartier_code, :arrondissement, :nom, :iris_type, :annee,
            :prix_m2_median, :pct_logements_sociaux, :nb_logements_sociaux,
            :revenu_median_uc, :taux_effort_achat,
            :surface_mediane, :nb_appartements, :nb_maisons, :pct_appartements,
            :nb_t1, :nb_t2, :nb_t3, :nb_t4plus,
            :score_qualite_vie, :nb_espaces_verts, :nb_arbres,
            :score_air_no2, :score_air_pm25, :pct_fibre,
            :nb_sanisettes, :nb_chantiers_actifs, :nb_anomalies,
            :score_transports, :score_transport_offre, :score_transport_intensite,
            :nb_gares, :nb_stations_velib, :capacite_velib_totale, :nb_lignes_transport,
            :lignes_par_gare_moyen, :nb_modes_lourds, :nb_arrets_bus, :pct_arrets_accessibles,
            :flux_multimodal, :flux_velo_trott, :flux_bus, :flux_motorise,
            :pct_flux_velo_trott, :pct_flux_motorise, :pct_flux_voie_cyclable,
            :score_loisirs, :nb_evenements, :nb_cinemas, :nb_terrasses, :nb_musees,
            :score_services, :nb_ecoles, :nb_maternelles, :nb_colleges, :nb_bibliotheques,
            :nb_bureaux_poste, :nb_ensup,
            :score_global
        )
        ON CONFLICT (iris_id, annee) DO UPDATE SET
            iris_code               = EXCLUDED.iris_code,
            quartier_id             = EXCLUDED.quartier_id,
            quartier_code           = EXCLUDED.quartier_code,
            arrondissement          = EXCLUDED.arrondissement,
            nom                     = EXCLUDED.nom,
            iris_type               = EXCLUDED.iris_type,
            prix_m2_median          = EXCLUDED.prix_m2_median,
            pct_logements_sociaux   = EXCLUDED.pct_logements_sociaux,
            nb_logements_sociaux    = EXCLUDED.nb_logements_sociaux,
            revenu_median_uc        = EXCLUDED.revenu_median_uc,
            taux_effort_achat       = EXCLUDED.taux_effort_achat,
            surface_mediane         = EXCLUDED.surface_mediane,
            nb_appartements         = EXCLUDED.nb_appartements,
            nb_maisons              = EXCLUDED.nb_maisons,
            pct_appartements        = EXCLUDED.pct_appartements,
            nb_t1                   = EXCLUDED.nb_t1,
            nb_t2                   = EXCLUDED.nb_t2,
            nb_t3                   = EXCLUDED.nb_t3,
            nb_t4plus               = EXCLUDED.nb_t4plus,
            score_qualite_vie       = EXCLUDED.score_qualite_vie,
            nb_espaces_verts        = EXCLUDED.nb_espaces_verts,
            nb_arbres               = EXCLUDED.nb_arbres,
            score_air_no2           = EXCLUDED.score_air_no2,
            score_air_pm25          = EXCLUDED.score_air_pm25,
            pct_fibre               = EXCLUDED.pct_fibre,
            nb_sanisettes           = EXCLUDED.nb_sanisettes,
            nb_chantiers_actifs     = EXCLUDED.nb_chantiers_actifs,
            nb_anomalies            = EXCLUDED.nb_anomalies,
            score_transports        = EXCLUDED.score_transports,
            score_transport_offre   = EXCLUDED.score_transport_offre,
            score_transport_intensite = EXCLUDED.score_transport_intensite,
            nb_gares                = EXCLUDED.nb_gares,
            nb_stations_velib       = EXCLUDED.nb_stations_velib,
            capacite_velib_totale   = EXCLUDED.capacite_velib_totale,
            nb_lignes_transport     = EXCLUDED.nb_lignes_transport,
            lignes_par_gare_moyen   = EXCLUDED.lignes_par_gare_moyen,
            nb_modes_lourds         = EXCLUDED.nb_modes_lourds,
            nb_arrets_bus           = EXCLUDED.nb_arrets_bus,
            pct_arrets_accessibles  = EXCLUDED.pct_arrets_accessibles,
            flux_multimodal         = EXCLUDED.flux_multimodal,
            flux_velo_trott         = EXCLUDED.flux_velo_trott,
            flux_bus                = EXCLUDED.flux_bus,
            flux_motorise           = EXCLUDED.flux_motorise,
            pct_flux_velo_trott     = EXCLUDED.pct_flux_velo_trott,
            pct_flux_motorise       = EXCLUDED.pct_flux_motorise,
            pct_flux_voie_cyclable  = EXCLUDED.pct_flux_voie_cyclable,
            score_loisirs           = EXCLUDED.score_loisirs,
            nb_evenements           = EXCLUDED.nb_evenements,
            nb_cinemas              = EXCLUDED.nb_cinemas,
            nb_terrasses            = EXCLUDED.nb_terrasses,
            nb_musees               = EXCLUDED.nb_musees,
            score_services          = EXCLUDED.score_services,
            nb_ecoles               = EXCLUDED.nb_ecoles,
            nb_maternelles          = EXCLUDED.nb_maternelles,
            nb_colleges             = EXCLUDED.nb_colleges,
            nb_bibliotheques        = EXCLUDED.nb_bibliotheques,
            nb_bureaux_poste        = EXCLUDED.nb_bureaux_poste,
            nb_ensup                = EXCLUDED.nb_ensup,
            score_global            = EXCLUDED.score_global
    """)

    with engine.connect() as conn:
        for iris_id, kpis in kpis_by_iris.items():
            conn.execute(sql, {"iris_id": iris_id, "annee": annee, **kpis})
        conn.commit()
    log.info(f"  ✓ {len(kpis_by_iris)} IRIS upsertés (annee={annee})")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run(annee: int | None = None):
    _t0 = time.perf_counter()
    from datetime import date
    from init_db import run as init_gold_schema

    log.info("=" * 60)
    log.info(f"Gold Aggregator — annee={annee or 'auto'}")
    log.info("=" * 60)

    log.info("Vérification du schéma Gold...")
    init_gold_schema()

    db = get_mongo()

    log.info("\n── Phase 1/2 : Silver Parquet → MongoDB Gold ──")
    load_silver_to_mongo(db)
    log.info("── Phase 2/2 : MongoDB Gold → PostgreSQL KPIs ──\n")
    engine = get_engine()
    current_year = date.today().year
    dvf_years = sorted(v for v in db["silver_dvf"].distinct("annee") if isinstance(v, int))
    if annee is not None:
        years_to_process = [annee]
    else:
        years_to_process = dvf_years[:] if dvf_years else [current_year]
        if current_year not in years_to_process:
            years_to_process.append(current_year)

    total_steps = 8 + len(years_to_process) * 4
    step_progress = tqdm(total=total_steps, desc="Gold pipeline", unit="step")

    def advance(label: str):
        next_step = step_progress.n + 1
        log.info(f"\n[{next_step}/{total_steps}] {label}")
        step_progress.set_description_str(f"Gold {next_step}/{total_steps}")
        step_progress.set_postfix_str(label)

    advance("Géométries quartiers")
    load_quartiers_geo(engine)
    step_progress.update(1)

    advance("Géométries IRIS")
    load_iris_geo(engine)
    step_progress.update(1)

    advance("Géométries arrondissements")
    load_arrondissements_geo(engine)
    step_progress.update(1)

    advance("Agrégation qualité de vie")
    qv = agg_qualite_vie(db)
    step_progress.update(1)

    advance("Agrégation transports")
    tr = agg_transports(db)
    step_progress.update(1)

    advance("Agrégation loisirs")
    lo = agg_loisirs(db)
    step_progress.update(1)

    advance("Agrégation services publics")
    sv = agg_services(db)
    step_progress.update(1)

    advance("Revenus médians INSEE Filosofi 2021")
    rev = agg_revenus(db)
    step_progress.update(1)

    for target_year in years_to_process:
        advance(f"Immobilier — {target_year}")
        im = agg_immobilier(db, target_year)
        step_progress.update(1)

        kpis_by_arr = {}
        for arr in ARRONDISSEMENTS:
            row = _default_arrondissement_kpis()
            row.update(qv.get(arr, {}))
            row.update(tr.get(arr, {}))
            row.update(lo.get(arr, {}))
            row.update(sv.get(arr, {}))
            row.update(im.get(arr, {}))
            row.update(rev.get(arr, {}))

            # Taux d'effort achat : nb d'années de revenu médian pour acheter 50m²
            prix = row.get("prix_m2_median")
            revenu = row.get("revenu_median_uc")
            if prix and revenu and revenu > 0:
                row["taux_effort_achat"] = round(prix * 50 / revenu, 2)
            else:
                row["taux_effort_achat"] = None

            s_qv = row.get("score_qualite_vie", 0) or 0
            s_tr = row.get("score_transports", 0)  or 0
            s_lo = row.get("score_loisirs", 0)     or 0
            s_sv = row.get("score_services", 0)    or 0
            row["score_global"] = round((s_qv + s_tr + s_lo + s_sv) / 4, 2)

            kpis_by_arr[arr] = row

        advance(f"Upsert arrondissement_kpis — {target_year}")
        upsert_kpis(engine, kpis_by_arr, target_year)
        step_progress.update(1)

        advance(f"KPI quartiers administratifs — {target_year}")
        kpis_by_quartier = agg_quartiers(db, target_year, kpis_by_arr)
        upsert_quartier_kpis(engine, kpis_by_quartier, target_year)
        step_progress.update(1)

        advance(f"KPI IRIS — {target_year}")
        kpis_by_iris = agg_iris(db, target_year, kpis_by_arr)
        upsert_iris_kpis(engine, kpis_by_iris, target_year)
        step_progress.update(1)

        log.info("\n" + "=" * 60)
        log.info(f"RAPPORT GOLD — {target_year}")
        log.info("=" * 60)
        for arr in ARRONDISSEMENTS:
            k = kpis_by_arr[arr]
            log.info(
                f"  {arr:>2}e  QV={k.get('score_qualite_vie',0):5.1f}  "
                f"TR={k.get('score_transports',0):5.1f}  "
                f"LO={k.get('score_loisirs',0):5.1f}  "
                f"SV={k.get('score_services',0):5.1f}  "
                f"→ GLOBAL={k.get('score_global',0):5.1f}"
            )
        log.info("=" * 60)

    elapsed = time.perf_counter() - _t0
    log.info(f"[PERF] gold_aggregator : {elapsed:.1f}s — annee={annee or 'auto'}")
    step_progress.close()
    engine.dispose()


if __name__ == "__main__":
    import sys
    annee_arg = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(annee_arg)
