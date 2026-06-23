"""
Silver Layer Transformer — Urban Data Explorer Paris
Bronze (MinIO "bronze") → Silver (MinIO "silver" Parquet + MongoDB documents)
"""

import json
import logging
import struct
import re
import math
import pandas as pd
import boto3
import requests
from io import BytesIO
from pymongo import MongoClient, UpdateOne
from botocore.exceptions import ClientError

from config import (
    MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY,
    BUCKET_BRONZE, BUCKET_SILVER,
    MONGO_URI, MONGO_DB,
)
from progress_utils import tqdm

# ─── Config ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("silver_transformer")
ARRONDISSEMENTS_GEO_URL = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/arrondissements/exports/geojson?lang=fr&timezone=Europe%2FParis"
_ARRONDISSEMENT_SHAPES = None

# ─── MinIO ────────────────────────────────────────────────────────────────────

s3 = boto3.client(
    "s3",
    endpoint_url=MINIO_ENDPOINT,
    aws_access_key_id=MINIO_ACCESS_KEY,
    aws_secret_access_key=MINIO_SECRET_KEY,
)

# ─── MongoDB ──────────────────────────────────────────────────────────────────

mongo = MongoClient(MONGO_URI)
db = mongo[MONGO_DB]

# ─── Geo helpers ──────────────────────────────────────────────────────────────

def wkb_hex_to_geojson(hex_val) -> dict | None:
    """Décode un hex WKB Point → GeoJSON Point {type, coordinates:[lon,lat]}."""
    if not hex_val or pd.isna(hex_val):
        return None
    try:
        wkb = bytes.fromhex(str(hex_val))
        lon = struct.unpack_from("<d", wkb, 5)[0]
        lat = struct.unpack_from("<d", wkb, 13)[0]
        if -180 <= lon <= 180 and -90 <= lat <= 90:
            return {"type": "Point", "coordinates": [lon, lat]}
    except Exception:
        pass
    return None


def parse_arrondissement(val) -> int | None:
    """
    Normalise arrondissement → int 1-20.
    Gère "75017", "75117" (code INSEE), "PARIS 17E ARRDT", "17e", "17"
    """
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    m = re.match(r"^75[01](\d{2})$", s)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 20 else None
    m = re.search(r"\b(\d{1,2})\b", s)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 20 else None
    return None


def lambert93_to_wgs84(x_series, y_series):
    """Convertit coordonnées Lambert-93 → (lon, lat) WGS84."""
    try:
        from pyproj import Transformer
        t = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)
        lons, lats = t.transform(x_series.values, y_series.values)
        return lons, lats
    except ImportError:
        log.warning("    pyproj non installé — pip install pyproj pour convertir Lambert-93")
        return None, None


def latlon_cols_to_geojson(lat_col, lon_col) -> list:
    """Convertit deux séries lat/lon → liste de GeoJSON Points."""
    result = []
    for lat, lon in zip(lat_col, lon_col):
        try:
            if pd.notna(lat) and pd.notna(lon):
                lat_f = float(lat)
                lon_f = float(lon)
                if lat_f == 0 and lon_f == 0:
                    result.append(None)
                elif -90 <= lat_f <= 90 and -180 <= lon_f <= 180:
                    result.append({"type": "Point", "coordinates": [lon_f, lat_f]})
                else:
                    result.append(None)
            else:
                result.append(None)
        except Exception:
            result.append(None)
    return result


def _fill_location_from_latlon(df: pd.DataFrame, lat_col: str, lon_col: str) -> pd.DataFrame:
    """Alimente `location` depuis une paire de colonnes latitude/longitude."""
    if lat_col not in df.columns or lon_col not in df.columns:
        return df

    points = pd.Series(latlon_cols_to_geojson(df[lat_col], df[lon_col]), index=df.index, dtype="object")
    if "location" not in df.columns:
        df["location"] = points
        return df

    missing_mask = df["location"].isna()
    if missing_mask.any():
        df.loc[missing_mask, "location"] = points.loc[missing_mask]
    return df


def _extract_location_from_locations(value) -> dict | None:
    """Extrait un point GeoJSON depuis le payload `locations` des événements Paris."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None

    payload = value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except Exception:
            return None

    if not isinstance(payload, list):
        return None

    for entry in payload:
        if not isinstance(entry, dict):
            continue
        raw_coords = entry.get("address_lat_lon")
        if not raw_coords:
            continue
        parts = [part.strip() for part in str(raw_coords).split(",")]
        if len(parts) != 2:
            continue
        point = latlon_cols_to_geojson([parts[0]], [parts[1]])[0]
        if point is not None:
            return point
    return None


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


def _load_arrondissement_shapes():
    """Charge et met en cache les polygones des arrondissements pour affectation spatiale."""
    global _ARRONDISSEMENT_SHAPES
    if _ARRONDISSEMENT_SHAPES is not None:
        return _ARRONDISSEMENT_SHAPES

    shapes = []
    try:
        response = requests.get(ARRONDISSEMENTS_GEO_URL, timeout=30)
        response.raise_for_status()
        geojson = response.json()
        for feature in geojson.get("features", []):
            props = feature.get("properties", {})
            arr = None
            for key in ["c_ar", "c_arinsee", "l_ar", "arrondissement"]:
                if key in props:
                    arr = parse_arrondissement(props.get(key))
                    if arr:
                        break
            if not arr:
                continue
            rings = _extract_polygon_rings(feature.get("geometry", {}))
            if rings:
                shapes.append((arr, rings))
    except Exception as exc:
        log.warning(f"    Impossible de charger les polygones d'arrondissements pour l'affectation spatiale: {exc}")
        shapes = []

    _ARRONDISSEMENT_SHAPES = shapes
    return _ARRONDISSEMENT_SHAPES


def infer_arrondissement_from_location(location: dict | None) -> int | None:
    """Déduit l'arrondissement à partir d'un point GeoJSON WGS84."""
    if not isinstance(location, dict):
        return None
    coords = location.get("coordinates", [])
    if len(coords) != 2:
        return None
    lon, lat = coords
    try:
        lon = float(lon)
        lat = float(lat)
    except Exception:
        return None

    for arr, rings in _load_arrondissement_shapes():
        if any(_point_in_ring(lon, lat, ring) for ring in rings):
            return arr
    return None


def _rename_first_existing(df: pd.DataFrame, target: str, candidates: list[str]) -> pd.DataFrame:
    """Renomme la première colonne trouvée parmi plusieurs alias vers un nom cible."""
    if target in df.columns:
        return df
    for col in candidates:
        if col in df.columns:
            return df.rename(columns={col: target})
    return df


# ─── Base transformer ─────────────────────────────────────────────────────────

def _base_geo(df: pd.DataFrame) -> pd.DataFrame:
    """Appliqué à tous les datasets : décode geo_point_2d + normalise arrondissement."""
    if "geo_point_2d" in df.columns:
        df["location"] = df["geo_point_2d"].apply(wkb_hex_to_geojson)
        df = df.drop(columns=["geo_point_2d"])
    if "geo_shape" in df.columns:
        df = df.drop(columns=["geo_shape"])
    # Colonnes lat/lon explicites (API IDF)
    for lat_c, lon_c in [
        ("lat", "lon"),
        ("latitude", "longitude"),
        ("ylatitude", "xlongitude"),
        ("arrgeopoint.lat", "arrgeopoint.lon"),
        ("coordonnees_geo.lat", "coordonnees_geo.lon"),
        ("geo.lat", "geo.lon"),
        ("geolocalisation.lat", "geolocalisation.lon"),
        ("geo_point_2d.lat", "geo_point_2d.lon"),
        ("lat_lon.lat", "lat_lon.lon"),
    ]:
        df = _fill_location_from_latlon(df, lat_c, lon_c)
    if "locations" in df.columns:
        derived_locations = df["locations"].apply(_extract_location_from_locations)
        if "location" not in df.columns:
            df["location"] = derived_locations
        else:
            missing_mask = df["location"].isna()
            if missing_mask.any():
                df.loc[missing_mask, "location"] = derived_locations.loc[missing_mask]
    # Arrondissement
    for col in [
        "arrondissement", "cp_arrondissement", "code_postal", "arr_insee", "arr_libelle",
        "nom_arrondissement", "arrtown", "arrpostalregion", "code_insee_commune", "code_insee",
        "nom_arrondissement_communes",
    ]:
        if col in df.columns:
            df["arrondissement"] = df[col].apply(parse_arrondissement)
            break
    if "location" in df.columns and ("arrondissement" not in df.columns or df["arrondissement"].isna().any()):
        if "arrondissement" not in df.columns:
            df["arrondissement"] = None
        df["arrondissement"] = df["arrondissement"].astype("object")
        missing_mask = df["arrondissement"].isna()
        if missing_mask.any():
            df.loc[missing_mask, "arrondissement"] = df.loc[missing_mask, "location"].apply(infer_arrondissement_from_location)
    return df


# ─── Transformers spécifiques ─────────────────────────────────────────────────

def transform_espaces_verts(df: pd.DataFrame) -> pd.DataFrame:
    df = _base_geo(df)
    keep = [
        "identifiant", "nom", "type", "arrondissement",
        "statut_ouverture", "ouvert_24h", "adresse", "location",
        "_ingested_at", "_dataset_id", "_indicateur", "_signe", "_source",
    ]
    return df[[c for c in keep if c in df.columns]]


def transform_equipements(df: pd.DataFrame) -> pd.DataFrame:
    df = _base_geo(df)
    keep = [
        "identifiant", "nom", "type", "payant", "arrondissement",
        "statut_ouverture", "adresse", "location",
        "_ingested_at", "_dataset_id", "_indicateur", "_signe", "_source",
    ]
    return df[[c for c in keep if c in df.columns]]


def transform_arbres(df: pd.DataFrame) -> pd.DataFrame:
    df = _base_geo(df)
    keep = [
        "idbase", "libellefrancais", "genre", "espece", "domanialite",
        "arrondissement", "adresse", "hauteurencm", "circonferenceencm",
        "location", "_ingested_at", "_dataset_id", "_indicateur", "_signe", "_source",
    ]
    return df[[c for c in keep if c in df.columns]]


def transform_qualite_air(df: pd.DataFrame) -> pd.DataFrame:
    num_cols = df.select_dtypes(include="number").columns.tolist()
    keep = ["annee"] + num_cols + ["_ingested_at", "_dataset_id", "_indicateur", "_signe", "_source"]
    return df[[c for c in keep if c in df.columns]]


def transform_fibre_imb(df: pd.DataFrame) -> pd.DataFrame:
    if "imb_x" in df.columns and "imb_y" in df.columns:
        lons, lats = lambert93_to_wgs84(df["imb_x"], df["imb_y"])
        if lons is not None:
            df["location"] = [
                {"type": "Point", "coordinates": [float(lo), float(la)]}
                if pd.notna(lo) and pd.notna(la) else None
                for lo, la in zip(lons, lats)
            ]
    if "imb_code_insee" in df.columns:
        df["arrondissement"] = df["imb_code_insee"].apply(parse_arrondissement)
    return df


def transform_api_geo(df: pd.DataFrame) -> pd.DataFrame:
    """Datasets API Paris OpenDataSoft : geo_point_2d WKB + arrondissement."""
    return _base_geo(df)


def transform_ecoles(df: pd.DataFrame) -> pd.DataFrame:
    df = _base_geo(df)
    keep = [
        "identifiant_de_l_etablissement", "nom_etablissement", "type_etablissement",
        "arrondissement", "adresse_1", "location",
        "_ingested_at", "_dataset_id", "_indicateur", "_signe", "_source",
    ]
    # Noms alternatifs selon le dataset
    rename_map = {
        "nom": "nom_etablissement",
        "libelle": "nom_etablissement",
        "type_etabll": "type_etablissement",
        "adresse": "adresse_1",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns and v not in df.columns})
    return df[[c for c in keep if c in df.columns]]


def transform_idf_geo(df: pd.DataFrame) -> pd.DataFrame:
    """Datasets IDF OpenData : colonnes lat/lon + filtre Paris (75)."""
    df = _base_geo(df)
    original_df = df

    def _keep_if_non_empty(mask):
        filtered = df[mask]
        return filtered if not filtered.empty else None

    # 1. Code postal / code commune commençant par 75
    for cp_col in ["code_postal", "cp", "codepostal", "code_commune_insee"]:
        if cp_col in df.columns:
            filtered = _keep_if_non_empty(df[cp_col].astype(str).str.startswith("75"))
            if filtered is not None:
                return filtered

    # 2. Colonne département numérique (ex: "75") ou textuelle (ex: "Paris")
    for dep_col in ["departement", "dep", "code_dep", "num_dep"]:
        if dep_col in df.columns:
            mask = (
                df[dep_col].astype(str).str.startswith("75") |
                df[dep_col].astype(str).str.lower().str.contains("paris")
            )
            filtered = _keep_if_non_empty(mask)
            if filtered is not None:
                return filtered

    # 3. Colonne ville/commune textuelle contenant "Paris"
    for town_col in ["arrtown", "town", "ville", "city_name", "commune_name", "nom_commune", "commune"]:
        if town_col in df.columns:
            filtered = _keep_if_non_empty(df[town_col].astype(str).str.contains("Paris", case=False, na=False))
            if filtered is not None:
                return filtered

    # 4. Bbox géographique Paris (fallback)
    if "location" in df.columns:
        def in_paris(loc):
            if not isinstance(loc, dict):
                return False
            coords = loc.get("coordinates", [])
            if len(coords) == 2:
                lon, lat = coords
                return 2.22 <= lon <= 2.47 and 48.81 <= lat <= 48.91
            return False

        filtered = _keep_if_non_empty(df["location"].apply(in_paris))
        if filtered is not None:
            return filtered

    return original_df


def transform_idf_transport(df: pd.DataFrame) -> pd.DataFrame:
    """
    Datasets transport IDF : filtre Paris + schéma Silver minimal.
    Normalise quelques alias fréquents pour éviter de conserver tout le payload Bronze.
    """
    df = transform_idf_geo(df)

    aliases = {
        "identifiant": ["id", "id_ref_zdl", "id_refa", "objectid", "stop_id", "stopareaid", "arrid", "id_gares"],
        "nom": ["nom", "nomlong", "nom_gare", "nom_arret", "libelle", "stopname", "name", "arrname", "nom_gares"],
        "mode_transport": ["mode", "transportmode", "modeprincipa", "mode_principal", "type_arret", "type"],
        "ligne": ["ligne", "nomligne", "res_com", "res_stif", "line", "indice_lig"],
        "commune": ["commune", "nom_commune", "nomcommune", "city", "arrtown"],
        "code_postal": ["code_postal", "cp", "codepostal", "postal_code", "arrpostalregion"],
        "accessible": ["arraccessibility"],
    }
    for target, candidates in aliases.items():
        df = _rename_first_existing(df, target, candidates)

    if "arrondissement" not in df.columns or df["arrondissement"].isna().all():
        for col in ["commune", "code_postal", "nom", "ligne"]:
            if col in df.columns:
                parsed = df[col].apply(parse_arrondissement)
                if parsed.notna().any():
                    df["arrondissement"] = parsed
                    break

    keep = [
        "identifiant", "nom", "mode_transport", "ligne",
        "commune", "code_postal", "arrondissement", "accessible",
        "terrer", "termetro", "tertram", "tertrain", "terval",
        "location",
        "_ingested_at", "_dataset_id", "_indicateur", "_signe", "_source",
    ]
    return df[[c for c in keep if c in df.columns]]


def transform_velib(df: pd.DataFrame) -> pd.DataFrame:
    """Velib : keep Paris stations only, fix coordonnees_geo dot-column conflict."""
    if "nom_arrondissement_communes" in df.columns:
        before = len(df)
        df = df[df["nom_arrondissement_communes"] == "Paris"]
        log.info(f"    Velib Paris filter: {before} → {len(df)} stations")

    # The API flattens coordonnees_geo → two dot-named columns + a NaN parent.
    # MongoDB $set conflicts when both the parent and a dotted subpath are present.
    lon_col, lat_col = "coordonnees_geo.lon", "coordonnees_geo.lat"
    if lon_col in df.columns and lat_col in df.columns and "location" not in df.columns:
        df["location"] = latlon_cols_to_geojson(df[lat_col], df[lon_col])
    drop_cols = [c for c in [lon_col, lat_col, "coordonnees_geo"] if c in df.columns]
    if drop_cols:
        df = df.drop(columns=drop_cols)

    df = _base_geo(df)
    df = df.rename(columns={
        "name": "nom",
        "creditcard": "paiement_cb",
    })
    keep = [
        "stationcode", "nom", "capacity",
        "numbikesavailable", "numdocksavailable", "mechanical", "ebike",
        "is_renting", "is_returning", "paiement_cb",
        "nom_arrondissement_communes", "code_insee_commune", "arrondissement", "location",
        "_ingested_at", "_dataset_id", "_indicateur", "_signe", "_source",
    ]
    return df[[c for c in keep if c in df.columns]]


def transform_passthrough(df: pd.DataFrame) -> pd.DataFrame:
    return df


def transform_dvf(df: pd.DataFrame) -> pd.DataFrame:
    """
    DVF Etalab :
    - mode historique géolocalisé : mutations unitaires avec coordonnées
    - compatibilité conservée avec l'ancien format agrégé si présent
    """
    if "date_mutation" in df.columns:
        if "longitude" in df.columns and "latitude" in df.columns:
            df["location"] = latlon_cols_to_geojson(df["latitude"], df["longitude"])
        if "code_commune" in df.columns:
            df["arrondissement"] = df["code_commune"].apply(parse_arrondissement)
            df["code_insee"] = df["code_commune"]
        elif "code_insee" in df.columns:
            df["arrondissement"] = df["code_insee"].apply(parse_arrondissement)

        df["annee"] = pd.to_datetime(df["date_mutation"], errors="coerce").dt.year
        df["valeur_fonciere"] = pd.to_numeric(df.get("valeur_fonciere"), errors="coerce")
        df["surface_reelle_bati"] = pd.to_numeric(df.get("surface_reelle_bati"), errors="coerce")
        df["prix_m2"] = (
            df["valeur_fonciere"] / df["surface_reelle_bati"]
        ).where(df["surface_reelle_bati"].fillna(0) > 0)

        keep = [
            "id_mutation", "date_mutation", "annee", "numero_disposition",
            "nature_mutation", "valeur_fonciere", "code_postal", "code_insee",
            "nom_commune", "arrondissement", "type_local", "surface_reelle_bati",
            "nombre_pieces_principales", "surface_terrain", "nombre_lots",
            "longitude", "latitude", "location", "prix_m2", "source_year",
            "_ingested_at", "_dataset_id", "_indicateur", "_signe", "_source",
        ]
        return df[[c for c in keep if c in df.columns]]

    # Normalise le nom de colonne prix selon la version de l'API
    for col in ["med_prix_m2_ventes", "prix_m2_median", "med_prixm2_ventes"]:
        if col in df.columns:
            df = df.rename(columns={col: "prix_m2_median"})
            break

    if "code_insee" in df.columns:
        df["arrondissement"] = df["code_insee"].apply(parse_arrondissement)

    keep = [
        "annee", "code_insee", "arrondissement", "prix_m2_median",
        "nbtrans_cod111", "nbtrans_cod121",  # nb transactions maisons/apparts si dispo
        "_ingested_at", "_dataset_id", "_indicateur", "_signe", "_source",
    ]
    return df[[c for c in keep if c in df.columns]]


# ─── Dataset registry ─────────────────────────────────────────────────────────
# (collection_mongo, transformer, colonne_id_pour_upsert, indicateur)

SILVER_CONFIG = {
    # Qualité de vie
    "ilots_fraicheur_espaces_verts": ("silver_espaces_verts",      transform_espaces_verts, "identifiant",   "qualite_vie"),
    "ilots_fraicheur_equipements":   ("silver_equipements",        transform_equipements,   "identifiant",   "qualite_vie"),
    "arbres":                        ("silver_arbres",             transform_arbres,        "idbase",        "qualite_vie"),
    "qualite_air":                   ("silver_qualite_air",        transform_qualite_air,   "annee",         "qualite_vie"),
    "fibre_actuel":                  ("silver_fibre_actuel",       transform_passthrough,   None,            "qualite_vie"),
    "fibre_base_imb":                ("silver_fibre_imb",          transform_fibre_imb,     "imb_id",        "qualite_vie"),
    "fibre_base_imb_fc":             ("silver_fibre_imb_fc",       transform_passthrough,   "immeuble_id",   "qualite_vie"),
    "fibre_debit_filaire":           ("silver_fibre_debit",        transform_passthrough,   "code_dep",      "qualite_vie"),
    "fibre_operateur":               ("silver_fibre_operateur",    transform_passthrough,   "code",          "qualite_vie"),
    "sanisettes":                    ("silver_sanisettes",         transform_api_geo,       None,            "qualite_vie"),
    "chantiers":                     ("silver_chantiers",          transform_api_geo,       None,            "qualite_vie"),
    "anomalies":                     ("silver_anomalies",          transform_api_geo,       None,            "qualite_vie"),
    "zones_touristiques":            ("silver_zones_touristiques", transform_api_geo,       None,            "qualite_vie"),
    # Transports
    "voies":                         ("silver_voies",              transform_api_geo,       None,            "transports"),
    "velib":                         ("silver_velib",              transform_velib,         "stationcode",   "transports"),
    "gares":                         ("silver_gares",              transform_idf_transport, None,            "transports"),
    "bus":                           ("silver_bus",                transform_idf_transport, None,            "transports"),
    # Loisirs
    "evenements_paris":              ("silver_evenements",         transform_api_geo,       None,            "loisirs"),
    "terrasses":                     ("silver_terrasses",          transform_api_geo,       None,            "loisirs"),
    "cinemas_idf":                   ("silver_cinemas",            transform_idf_geo,       None,            "loisirs"),
    "musees_idf":                    ("silver_musees",             transform_idf_geo,       None,            "loisirs"),
    # Services publics
    "ecoles_elementaires":           ("silver_ecoles_elem",        transform_ecoles,        None,            "services_publics"),
    "maternelles_secteurs":          ("silver_maternelles",        transform_api_geo,       None,            "services_publics"),
    "colleges_secteurs":             ("silver_colleges",           transform_api_geo,       None,            "services_publics"),
    "bibliotheques":                 ("silver_bibliotheques",      transform_api_geo,       None,            "services_publics"),
    "enseignement_superieur":        ("silver_ensup",              transform_idf_geo,       None,            "services_publics"),
    "bureaux_poste":                 ("silver_bureaux_poste",      transform_idf_geo,       None,            "services_publics"),
    # Immobilier
    "logements_sociaux":             ("silver_logements_sociaux",  transform_api_geo,       None,            "immobilier"),
    "dvf_prix_m2":                   ("silver_dvf",                transform_dvf,           None,            "immobilier"),
}

# ─── Source map (dataset_id → source) ─────────────────────────────────────────

SOURCE_MAP = {
    "ilots_fraicheur_espaces_verts": "paris_opendata",
    "ilots_fraicheur_equipements":   "paris_opendata",
    "arbres":                        "paris_opendata",
    "qualite_air":                   "datagouv",
    "fibre_actuel":                  "datagouv",
    "fibre_base_imb":                "datagouv",
    "fibre_base_imb_fc":             "datagouv",
    "fibre_debit_filaire":           "datagouv",
    "fibre_operateur":               "datagouv",
    "sanisettes":                    "paris_opendata",
    "chantiers":                     "paris_opendata",
    "anomalies":                     "paris_opendata",
    "zones_touristiques":            "paris_opendata",
    "voies":           "paris_opendata",
    "velib":                         "transport_gouv",
    "gares":                         "transport_gouv",
    "bus":                           "transport_gouv",
    "evenements_paris":              "paris_opendata",
    "terrasses":                     "paris_opendata",
    "cinemas_idf":                   "idf_opendata",
    "musees_idf":                    "idf_opendata",
    "ecoles_elementaires":           "paris_opendata",
    "maternelles_secteurs":          "paris_opendata",
    "colleges_secteurs":             "paris_opendata",
    "bibliotheques":                 "paris_opendata",
    "enseignement_superieur":        "idf_opendata",
    "bureaux_poste":                 "idf_opendata",
    "logements_sociaux":             "paris_opendata",
    "dvf_prix_m2":                   "etalab",
}

# ─── MinIO reader ─────────────────────────────────────────────────────────────

def latest_ingestion_date() -> str | None:
    """Trouve la partition ingestion_date= la plus récente dans MinIO bronze."""
    paginator = s3.get_paginator("list_objects_v2")
    dates = set()
    for page in paginator.paginate(Bucket=BUCKET_BRONZE, Delimiter="/"):
        for p in page.get("CommonPrefixes", []):
            m = re.match(r"ingestion_date=(\d{4}-\d{2}-\d{2})/", p["Prefix"])
            if m:
                dates.add(m.group(1))
    return max(dates) if dates else None


def read_bronze(indicateur: str, dataset_id: str, ingestion_date: str) -> pd.DataFrame | None:
    """Lit raw.parquet depuis MinIO bronze (partition par indicateur)."""
    key = f"ingestion_date={ingestion_date}/{indicateur}/{dataset_id}/raw.parquet"
    try:
        obj = s3.get_object(Bucket=BUCKET_BRONZE, Key=key)
        df = pd.read_parquet(BytesIO(obj["Body"].read()))
        log.info(f"  Bronze lu : {key} ({len(df)} lignes)")
        return df
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            log.warning(f"  Introuvable dans MinIO : {key}")
            return None
        raise


# ─── Writers ──────────────────────────────────────────────────────────────────

def write_silver_minio(df: pd.DataFrame, dataset_id: str, indicateur: str, ingestion_date: str):
    """Écrit le Parquet nettoyé dans MinIO bucket 'silver'."""
    key = f"ingestion_date={ingestion_date}/{indicateur}/{dataset_id}/clean.parquet"
    buf = BytesIO()
    df_out = df.copy()
    if "location" in df_out.columns:
        df_out["location"] = df_out["location"].apply(
            lambda v: json.dumps(v) if isinstance(v, dict) else v
        )
    df_out.to_parquet(buf, index=False)
    buf.seek(0)
    s3.put_object(Bucket=BUCKET_SILVER, Key=key, Body=buf.getvalue())
    log.info(f"    ✓ MinIO silver → {key}")


BULK_CHUNK = 5_000  # insert_many par batch de 5000 docs

def write_silver_mongo(df: pd.DataFrame, collection_name: str, id_col: str | None):
    """Écrit les documents dans MongoDB silver — bulk insert pour les gros datasets."""
    coll = db[collection_name]
    records = df.where(pd.notna(df), other=None).to_dict("records")
    if not records:
        return

    if id_col and id_col in df.columns and len(records) < 10_000:
        # Upsert seulement pour les petits datasets avec clé métier
        ops = [UpdateOne({id_col: r[id_col]}, {"$set": r}, upsert=True) for r in records]
        result = coll.bulk_write(ops, ordered=False)
        log.info(f"    ✓ MongoDB {collection_name} — upserted={result.upserted_count} modified={result.modified_count}")
    else:
        # Bulk insert : drop la collection + insert_many par chunks
        coll.drop()
        total = 0
        chunk_count = math.ceil(len(records) / BULK_CHUNK)
        chunk_progress = tqdm(
            range(0, len(records), BULK_CHUNK),
            total=chunk_count,
            desc=f"Mongo {collection_name}",
            unit="chunk",
            leave=False,
        )
        for i in chunk_progress:
            chunk = records[i:i + BULK_CHUNK]
            coll.insert_many(chunk, ordered=False)
            total += len(chunk)
            chunk_progress.set_postfix_str(f"{total}/{len(records)} docs")
        chunk_progress.close()
        log.info(f"    ✓ MongoDB {collection_name} — {total} insérés (bulk)")

    if "location" in df.columns:
        try:
            coll.create_index([("location", "2dsphere")])
        except Exception:
            pass
    if "arrondissement" in df.columns:
        coll.create_index("arrondissement")
    coll.create_index("_indicateur")


# ─── Init ─────────────────────────────────────────────────────────────────────

def init_minio():
    try:
        existing = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
        log.info("  ✓ MinIO connecté")
    except Exception as e:
        raise RuntimeError(f"MinIO inaccessible — docker-compose up ? ({e})")
    if BUCKET_SILVER not in existing:
        s3.create_bucket(Bucket=BUCKET_SILVER)
        log.info(f"  ✓ Bucket '{BUCKET_SILVER}' créé")
    else:
        log.info(f"  ✓ Bucket '{BUCKET_SILVER}' existant")


def init_mongo():
    try:
        mongo.admin.command("ping")
        log.info("  ✓ MongoDB connecté")
    except Exception as e:
        raise RuntimeError(f"MongoDB inaccessible — docker-compose up ? ({e})")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run(ingestion_date: str | None = None):
    log.info("=" * 60)
    log.info("Silver Transformer")
    log.info("=" * 60)

    init_minio()
    init_mongo()

    if ingestion_date is None:
        ingestion_date = latest_ingestion_date()
        if not ingestion_date:
            raise RuntimeError("Aucune partition trouvée dans MinIO — lancer bronze_feeder d'abord")
    log.info(f"  Partition : ingestion_date={ingestion_date}")

    results = []

    silver_items = list(SILVER_CONFIG.items())
    dataset_progress = tqdm(silver_items, desc="Silver datasets", unit="dataset")
    for idx, (dataset_id, (collection, transformer, id_col, indicateur)) in enumerate(dataset_progress, start=1):
        dataset_progress.set_description_str(f"Silver {idx}/{len(silver_items)}")
        dataset_progress.set_postfix_str(dataset_id)
        log.info(f"\n[{indicateur.upper()}] [{dataset_id}]")

        try:
            df = read_bronze(indicateur, dataset_id, ingestion_date)
            if df is None:
                results.append({"id": dataset_id, "status": "SKIPPED (absent bronze)"})
                continue

            df = transformer(df)

            if df.empty:
                log.warning(f"  DataFrame vide après transformation")
                results.append({"id": dataset_id, "status": "VIDE après transform"})
                continue

            write_silver_minio(df, dataset_id, indicateur, ingestion_date)
            write_silver_mongo(df, collection, id_col)

            results.append({
                "id": dataset_id,
                "collection": collection,
                "indicateur": indicateur,
                "rows": len(df),
                "status": "OK",
                "has_geo": "location" in df.columns,
            })

        except Exception as e:
            log.error(f"  ERREUR : {e}")
            results.append({"id": dataset_id, "status": f"ERREUR: {e}"})
    dataset_progress.close()

    log.info(f"\n{'='*60}")
    log.info("RAPPORT SILVER")
    log.info(f"{'='*60}")
    ok = [r for r in results if r["status"] == "OK"]
    ko = [r for r in results if r["status"] not in ("OK",) and not r["status"].startswith("SKIPPED") and not r["status"].startswith("VIDE")]
    skipped = [r for r in results if r["status"].startswith("SKIPPED") or r["status"].startswith("VIDE")]
    log.info(f"  OK      : {len(ok)}/{len(results)}")
    for r in ok:
        geo_tag = " [geo]" if r.get("has_geo") else ""
        log.info(f"    [{r.get('indicateur','?'):20}] {r['id']:40} {r['rows']:>8}  →  {r['collection']}{geo_tag}")
    if skipped:
        log.info(f"  SKIPPED/VIDE : {len(skipped)}")
        for r in skipped:
            log.info(f"    {r['id']} — {r['status']}")
    if ko:
        log.info(f"  ERREURS : {len(ko)}")
        for r in ko:
            log.info(f"    {r['id']} — {r['status']}")
    log.info(f"{'='*60}\n")


if __name__ == "__main__":
    import sys
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run(date_arg)
