"""
Silver Layer Transformer — Urban Data Explorer Paris
Bronze (MinIO "bronze") → Silver (MinIO "silver" Parquet uniquement)
MongoDB est peuplé en Gold par gold_aggregator.py (Phase 1).
"""

import json
import logging
import os
import struct
import re
import math
import sys
import time
import threading
import pandas as pd
import boto3
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from botocore.exceptions import ClientError

# Permet l'execution directe via `python pipeline/silver_transformer.py`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.config import (
    MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY,
    BUCKET_BRONZE, BUCKET_SILVER,
)
from pipeline.progress_utils import tqdm

# ─── Config ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("silver_transformer")
ARRONDISSEMENTS_GEO_URL = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/arrondissements/exports/geojson?lang=fr&timezone=Europe%2FParis"
QUARTIERS_GEO_URL = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/quartier_paris/exports/geojson?lang=fr&timezone=Europe%2FParis"
IRIS_GEO_URL = 'https://opendata.iledefrance.fr/api/explore/v2.1/catalog/datasets/iris/exports/geojson?where=startswith(code_iris,"751")'
_ARRONDISSEMENT_SHAPES = None
_QUARTIER_SHAPES = None
_QUARTIER_BY_ID = None
_QUARTIER_POINT_CACHE = {}
_IRIS_SHAPES = None
_IRIS_BY_ID = None
_IRIS_POINT_CACHE = {}
_QUARTIER_GDF = None  # geopandas GeoDataFrame, construit à la première utilisation
_IRIS_GDF = None      # geopandas GeoDataFrame, construit à la première utilisation
ADMIN_SUBAREA_FIELDS = ["quartier_id", "quartier_code", "quartier_nom", "iris_id", "iris_code", "iris_nom", "iris_type"]

# ─── MinIO ────────────────────────────────────────────────────────────────────

s3 = boto3.client(
    "s3",
    endpoint_url=MINIO_ENDPOINT,
    aws_access_key_id=MINIO_ACCESS_KEY,
    aws_secret_access_key=MINIO_SECRET_KEY,
)

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
    m = re.search(r"(?<!\d)(\d{1,2})(?!\d)", s)
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


def _extract_location_from_coordonnees_geo(value) -> dict | None:
    """Extrait un point GeoJSON depuis une chaine 'lat, lon'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text:
        return None
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 2:
        return None
    return latlon_cols_to_geojson([parts[0]], [parts[1]])[0]


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


def _first_present(props: dict, keys: list[str], default=None):
    for key in keys:
        value = props.get(key)
        if value not in (None, ""):
            return value
    return default


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


def _load_quartier_shapes():
    """Charge et met en cache les polygones et métadonnées des quartiers administratifs."""
    global _QUARTIER_SHAPES, _QUARTIER_BY_ID
    if _QUARTIER_SHAPES is not None and _QUARTIER_BY_ID is not None:
        return _QUARTIER_SHAPES, _QUARTIER_BY_ID

    shapes = []
    quartiers_by_id = {}
    try:
        response = requests.get(QUARTIERS_GEO_URL, timeout=30)
        response.raise_for_status()
        geojson = response.json()
        for feature in geojson.get("features", []):
            props = feature.get("properties", {})
            quartier_id = None
            for key in ["n_sq_qu", "id_quartier", "quartier_id", "c_qu", "code_qu"]:
                value = props.get(key)
                if value not in (None, ""):
                    quartier_id = value
                    break
            quartier_code = None
            for key in ["c_qu", "code_quartier", "code_qu", "quartier_code"]:
                value = props.get(key)
                if value not in (None, ""):
                    quartier_code = value
                    break
            nom = None
            for key in ["l_qu", "nom_quart", "nom_quartier", "quartier", "nom"]:
                value = props.get(key)
                if value not in (None, ""):
                    nom = value
                    break
            arr = None
            for key in ["c_ar", "arrondissement", "code_arrondissement", "c_arinsee", "arr_insee"]:
                if key in props:
                    arr = parse_arrondissement(props.get(key))
                    if arr:
                        break
            rings = _extract_polygon_rings(feature.get("geometry", {}))
            if not quartier_id:
                quartier_id = quartier_code or nom
            if not quartier_id or not nom or not rings:
                continue

            info = {
                "quartier_id": str(quartier_id),
                "quartier_code": str(quartier_code) if quartier_code is not None else None,
                "quartier_nom": str(nom),
                "arrondissement": arr,
                "rings": rings,
            }
            shapes.append(info)
            quartiers_by_id[info["quartier_id"]] = info
    except Exception as exc:
        log.warning(f"    Impossible de charger les polygones des quartiers administratifs: {exc}")
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
    try:
        response = requests.get(IRIS_GEO_URL, timeout=60)
        response.raise_for_status()
        geojson = response.json()
        for feature in geojson.get("features", []):
            props = feature.get("properties", {})
            iris_id = _first_present(props, ["code_iris", "CODE_IRIS", "id"])
            nom = _first_present(props, ["nom_iris", "NOM_IRIS", "libelle"])
            iris_type = _first_present(props, ["typ_iris", "TYP_IRIS", "type_iris"])
            code_insee = _first_present(props, ["insee_com", "INSEE_COM"])
            arr = parse_arrondissement(code_insee or (str(iris_id)[:5] if iris_id else None))
            rings = _extract_polygon_rings(feature.get("geometry", {}))
            if not iris_id or not nom or not rings:
                continue

            center = props.get("geo_point_2d") or {}
            center_loc = None
            if isinstance(center, dict):
                lon = center.get("lon")
                lat = center.get("lat")
                if lon is not None and lat is not None:
                    try:
                        center_loc = {"type": "Point", "coordinates": [float(lon), float(lat)]}
                    except Exception:
                        center_loc = None
            quartier = infer_quartier_from_location(center_loc) if center_loc else None

            info = {
                "iris_id": str(iris_id),
                "iris_code": str(iris_id),
                "iris_nom": str(nom),
                "iris_type": str(iris_type) if iris_type not in (None, "") else None,
                "arrondissement": arr,
                "quartier_id": quartier["quartier_id"] if quartier else None,
                "quartier_code": quartier["quartier_code"] if quartier else None,
                "quartier_nom": quartier["quartier_nom"] if quartier else None,
                "rings": rings,
            }
            shapes.append(info)
            iris_by_id[info["iris_id"]] = info
    except Exception as exc:
        log.warning(f"    Impossible de charger les polygones IRIS de Paris: {exc}")
        shapes = []
        iris_by_id = {}

    _IRIS_SHAPES = shapes
    _IRIS_BY_ID = iris_by_id
    return _IRIS_SHAPES, _IRIS_BY_ID


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
    if cache_key in _QUARTIER_POINT_CACHE:
        return _QUARTIER_POINT_CACHE[cache_key]

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
    if cache_key in _IRIS_POINT_CACHE:
        return _IRIS_POINT_CACHE[cache_key]

    iris_shapes, _ = _load_iris_shapes()
    for iris in iris_shapes:
        if any(_point_in_ring(lon, lat, ring) for ring in iris["rings"]):
            _IRIS_POINT_CACHE[cache_key] = iris
            return iris

    _IRIS_POINT_CACHE[cache_key] = None
    return None


def _rename_first_existing(df: pd.DataFrame, target: str, candidates: list[str]) -> pd.DataFrame:
    """Renomme la première colonne trouvée parmi plusieurs alias vers un nom cible."""
    if target in df.columns:
        return df
    for col in candidates:
        if col in df.columns:
            return df.rename(columns={col: target})
    return df


def _get_quartier_gdf():
    """Construit (et cache) un GeoDataFrame geopandas des quartiers depuis les shapes déjà chargés."""
    global _QUARTIER_GDF
    if _QUARTIER_GDF is not None:
        return _QUARTIER_GDF
    try:
        import geopandas as gpd
        from shapely.geometry import Polygon, MultiPolygon
        quartiers, _ = _load_quartier_shapes()
        rows = []
        for q in quartiers:
            polys = [Polygon(r) for r in q["rings"] if len(r) >= 3]
            if not polys:
                continue
            geom = polys[0] if len(polys) == 1 else MultiPolygon(polys)
            rows.append({
                "quartier_id":   q["quartier_id"],
                "quartier_code": q["quartier_code"],
                "quartier_nom":  q["quartier_nom"],
                "q_arr":         q["arrondissement"],
                "geometry":      geom,
            })
        _QUARTIER_GDF = gpd.GeoDataFrame(rows, crs="EPSG:4326") if rows else None
    except Exception as e:
        log.debug(f"_get_quartier_gdf échec : {e}")
        _QUARTIER_GDF = None
    return _QUARTIER_GDF


def _get_iris_gdf():
    """Construit (et cache) un GeoDataFrame geopandas des IRIS depuis les shapes déjà chargés."""
    global _IRIS_GDF
    if _IRIS_GDF is not None:
        return _IRIS_GDF
    try:
        import geopandas as gpd
        from shapely.geometry import Polygon, MultiPolygon
        iris_shapes, _ = _load_iris_shapes()
        rows = []
        for ir in iris_shapes:
            polys = [Polygon(r) for r in ir["rings"] if len(r) >= 3]
            if not polys:
                continue
            geom = polys[0] if len(polys) == 1 else MultiPolygon(polys)
            rows.append({
                "iris_id":   ir["iris_id"],
                "iris_code": ir["iris_code"],
                "iris_nom":  ir["iris_nom"],
                "iris_type": ir["iris_type"],
                "i_arr":     ir["arrondissement"],
                "geometry":  geom,
            })
        _IRIS_GDF = gpd.GeoDataFrame(rows, crs="EPSG:4326") if rows else None
    except Exception as e:
        log.debug(f"_get_iris_gdf échec : {e}")
        _IRIS_GDF = None
    return _IRIS_GDF


def _enrich_admin_areas_fast(df: pd.DataFrame) -> pd.DataFrame:
    """Version vectorisée avec geopandas.sjoin + R-tree — 20-50x plus rapide que row-by-row."""
    import geopandas as gpd
    from shapely.geometry import Point

    def _loc_to_point(loc):
        if not isinstance(loc, dict):
            return None
        coords = loc.get("coordinates", [])
        if len(coords) != 2:
            return None
        try:
            return Point(float(coords[0]), float(coords[1]))
        except Exception:
            return None

    geoms = df["location"].apply(_loc_to_point)
    valid_mask = geoms.notna()
    if not valid_mask.any():
        return df

    pts = gpd.GeoDataFrame(
        {"_oi": df.index[valid_mask].tolist()},
        geometry=geoms[valid_mask].tolist(),
        crs="EPSG:4326",
    )

    def _apply_cols(joined, col_pairs, arr_col):
        """Applique les colonnes du sjoin sur df, uniquement si manquantes."""
        idx_map = joined.drop_duplicates(subset=["_oi"]).set_index("_oi")
        for src, dst in col_pairs:
            if src not in idx_map.columns:
                continue
            mapped = idx_map[src].reindex(df.index)
            if dst not in df.columns:
                df[dst] = mapped.values
            else:
                missing = df[dst].isna()
                if missing.any():
                    df.loc[missing, dst] = mapped[missing].values
        if arr_col in idx_map.columns:
            mapped_arr = idx_map[arr_col].reindex(df.index)
            if "arrondissement" not in df.columns:
                df["arrondissement"] = mapped_arr.values
            else:
                df["arrondissement"] = df["arrondissement"].astype("object")
                missing = df["arrondissement"].isna()
                if missing.any():
                    df.loc[missing, "arrondissement"] = mapped_arr[missing].values

    q_gdf = _get_quartier_gdf()
    if q_gdf is not None:
        jq = gpd.sjoin(pts, q_gdf, how="left", predicate="within")
        _apply_cols(jq, [("quartier_id", "quartier_id"), ("quartier_code", "quartier_code"), ("quartier_nom", "quartier_nom")], "q_arr")

    ir_gdf = _get_iris_gdf()
    if ir_gdf is not None:
        ji = gpd.sjoin(pts, ir_gdf, how="left", predicate="within")
        _apply_cols(ji, [("iris_id", "iris_id"), ("iris_code", "iris_code"), ("iris_nom", "iris_nom"), ("iris_type", "iris_type")], "i_arr")

    return df


def _enrich_admin_areas(df: pd.DataFrame) -> pd.DataFrame:
    """
    Enrichit les documents géolocalisés avec des identifiants administratifs persistants.
    Utilise geopandas.sjoin (vectorisé) si disponible, sinon ray-casting ligne par ligne.
    """
    if "location" not in df.columns or df.empty:
        return df

    try:
        import geopandas  # noqa
        return _enrich_admin_areas_fast(df)
    except ImportError:
        pass

    # ── Fallback : implémentation originale row-by-row ─────────────────────────
    quartier_infos = [infer_quartier_from_location(loc) for loc in df["location"]]
    quartier_ids = pd.Series(
        [info["quartier_id"] if info else None for info in quartier_infos],
        index=df.index,
        dtype="object",
    )
    quartier_codes = pd.Series(
        [info["quartier_code"] if info else None for info in quartier_infos],
        index=df.index,
        dtype="object",
    )
    quartier_noms = pd.Series(
        [info["quartier_nom"] if info else None for info in quartier_infos],
        index=df.index,
        dtype="object",
    )
    quartier_arrs = pd.Series(
        [info["arrondissement"] if info else None for info in quartier_infos],
        index=df.index,
        dtype="object",
    )
    iris_infos = [infer_iris_from_location(loc) for loc in df["location"]]
    iris_ids = pd.Series(
        [info["iris_id"] if info else None for info in iris_infos],
        index=df.index,
        dtype="object",
    )
    iris_codes = pd.Series(
        [info["iris_code"] if info else None for info in iris_infos],
        index=df.index,
        dtype="object",
    )
    iris_noms = pd.Series(
        [info["iris_nom"] if info else None for info in iris_infos],
        index=df.index,
        dtype="object",
    )
    iris_types = pd.Series(
        [info["iris_type"] if info else None for info in iris_infos],
        index=df.index,
        dtype="object",
    )
    iris_arrs = pd.Series(
        [info["arrondissement"] if info else None for info in iris_infos],
        index=df.index,
        dtype="object",
    )

    for col_name, values in [
        ("quartier_id", quartier_ids),
        ("quartier_code", quartier_codes),
        ("quartier_nom", quartier_noms),
        ("iris_id", iris_ids),
        ("iris_code", iris_codes),
        ("iris_nom", iris_noms),
        ("iris_type", iris_types),
    ]:
        if col_name not in df.columns:
            df[col_name] = values
        else:
            missing_mask = df[col_name].isna()
            if missing_mask.any():
                df.loc[missing_mask, col_name] = values.loc[missing_mask]

    if "arrondissement" not in df.columns:
        df["arrondissement"] = quartier_arrs
    else:
        df["arrondissement"] = df["arrondissement"].astype("object")
        missing_mask = df["arrondissement"].isna()
        if missing_mask.any():
            df.loc[missing_mask, "arrondissement"] = quartier_arrs.loc[missing_mask]
        missing_mask = df["arrondissement"].isna()
        if missing_mask.any():
            df.loc[missing_mask, "arrondissement"] = iris_arrs.loc[missing_mask]

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
        ("coordonnees.lat", "coordonnees.lon"),
        ("geo.lat", "geo.lon"),
        ("geolocalisation.lat", "geolocalisation.lon"),
        ("position.lat", "position.lon"),
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
    if "coordonnees_geo" in df.columns:
        derived_locations = df["coordonnees_geo"].apply(_extract_location_from_coordonnees_geo)
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
    return _enrich_admin_areas(df)


def _filter_to_paris(df: pd.DataFrame, label: str) -> pd.DataFrame:
    """Conserve uniquement Paris intra-muros si possible, sinon retourne le dataset inchangé."""
    filtered = None

    if "arrondissement" in df.columns:
        arr = pd.to_numeric(df["arrondissement"], errors="coerce")
        candidate = df[arr.between(1, 20)]
        if not candidate.empty:
            filtered = candidate

    if filtered is None and "location" in df.columns:
        def in_paris(loc):
            if not isinstance(loc, dict):
                return False
            coords = loc.get("coordinates", [])
            if len(coords) != 2:
                return False
            lon, lat = coords
            return 2.22 <= lon <= 2.47 and 48.81 <= lat <= 48.91

        candidate = df[df["location"].apply(in_paris)]
        if not candidate.empty:
            filtered = candidate

    if filtered is None:
        return df

    if len(filtered) != len(df):
        log.info(f"    {label} Paris filter: {len(df)} → {len(filtered)} lignes")
    return filtered


# ─── Transformers spécifiques ─────────────────────────────────────────────────

def transform_espaces_verts(df: pd.DataFrame) -> pd.DataFrame:
    df = _base_geo(df)
    keep = [
        "identifiant", "nom", "type", "arrondissement",
        *ADMIN_SUBAREA_FIELDS,
        "statut_ouverture", "ouvert_24h", "adresse", "location",
        # Score de fraîcheur détaillé (végétation haute + amplitude horaire)
        "p_vegetation_h", "proportion_vegetation_haute",
        "horaires_periode", "horaires_lundi", "horaires_mardi", "horaires_mercredi",
        "horaires_jeudi", "horaires_vendredi", "horaires_samedi", "horaires_dimanche",
        "_ingested_at", "_dataset_id", "_indicateur", "_signe", "_source",
    ]
    return df[[c for c in keep if c in df.columns]]


def transform_equipements(df: pd.DataFrame) -> pd.DataFrame:
    df = _base_geo(df)
    keep = [
        "identifiant", "nom", "type", "payant", "arrondissement",
        *ADMIN_SUBAREA_FIELDS,
        "statut_ouverture", "adresse", "location",
        "_ingested_at", "_dataset_id", "_indicateur", "_signe", "_source",
    ]
    return df[[c for c in keep if c in df.columns]]


def transform_arbres(df: pd.DataFrame) -> pd.DataFrame:
    df = _base_geo(df)
    keep = [
        "idbase", "libellefrancais", "genre", "espece", "domanialite",
        "arrondissement", "adresse", "hauteurencm", "circonferenceencm",
        *ADMIN_SUBAREA_FIELDS,
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
    # addr_code format: "75112_xxxx_xxxxx" → arrondissement 12
    # imb_code_insee is always 75056 (Paris commune), not usable
    if "addr_code" in df.columns:
        df["arrondissement"] = df["addr_code"].apply(
            lambda v: parse_arrondissement(str(v)[:5]) if pd.notna(v) else None
        )
    elif "imb_code_insee" in df.columns:
        df["arrondissement"] = df["imb_code_insee"].apply(parse_arrondissement)
    # Presence in this dataset = building is fibre-eligible/deployed
    df["statut_immeuble"] = "Déployé"
    return _enrich_admin_areas(df)


def transform_api_geo(df: pd.DataFrame) -> pd.DataFrame:
    """Datasets API Paris OpenDataSoft : geo_point_2d WKB + arrondissement."""
    return _base_geo(df)


def transform_voies(df: pd.DataFrame) -> pd.DataFrame:
    """Comptages multimodaux : ne conserve que les colonnes utiles et Paris intra-muros."""
    df = _base_geo(df)
    df = _filter_to_paris(df, "Voies")
    keep = [
        "id_trajectoire", "id_site", "label", "t", "mode", "nb_usagers",
        "voie", "sens", "trajectoire", "arrondissement",
        *ADMIN_SUBAREA_FIELDS, "location",
        "_ingested_at", "_dataset_id", "_indicateur", "_signe", "_source",
    ]
    return df[[c for c in keep if c in df.columns]]


def transform_ecoles(df: pd.DataFrame) -> pd.DataFrame:
    df = _base_geo(df)
    keep = [
        "identifiant_de_l_etablissement", "nom_etablissement", "type_etablissement",
        "arrondissement", *ADMIN_SUBAREA_FIELDS,
        "adresse_1", "location",
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
        "commune", "code_postal", "arrondissement",
        *ADMIN_SUBAREA_FIELDS, "accessible",
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
        "nom_arrondissement_communes", "code_insee_commune", "arrondissement",
        *ADMIN_SUBAREA_FIELDS, "location",
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

        df = _enrich_admin_areas(df)

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
            *ADMIN_SUBAREA_FIELDS,
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


def transform_revenus(df: pd.DataFrame) -> pd.DataFrame:
    """
    Filosofi 2021 (GEO_OBJECT=ARM, FILOSOFI_MEASURE=MED_SL) →
    une ligne par arrondissement avec revenu_median_uc (€/an).
    GEO 75101 → arrondissement 1, ..., 75120 → 20.
    """
    df = df.copy()
    df["arrondissement"] = df["GEO"].apply(
        lambda x: int(str(x)[-2:]) if pd.notna(x) and str(x).startswith("751") and len(str(x)) == 5 else None
    )
    df = df[df["arrondissement"].notna() & df["arrondissement"].between(1, 20)].copy()
    df["arrondissement"] = df["arrondissement"].astype(int)
    df["revenu_median_uc"] = pd.to_numeric(df["OBS_VALUE"], errors="coerce")
    df["annee"] = pd.to_numeric(df["TIME_PERIOD"], errors="coerce").fillna(2021).astype(int)
    df["_indicateur"] = "immobilier"
    return df[["arrondissement", "revenu_median_uc", "annee", "_indicateur"]].dropna(subset=["revenu_median_uc"])


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
    # sanisettes, chantiers, anomalies → Kafka (kafka_consumer.py → silver_sanisettes/chantiers/anomalies)
    "zones_touristiques":            ("silver_zones_touristiques", transform_api_geo,       None,            "qualite_vie"),
    # Transports
    # voies, velib → Kafka (kafka_consumer.py → silver_voies/silver_velib)
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
    "revenus_medians":               ("silver_revenus",            transform_revenus,       "arrondissement", "immobilier"),
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
    "zones_touristiques":            "paris_opendata",
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
    "revenus_medians":               "insee_datagouv",
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

def _already_silver_today(dataset_id: str, indicateur: str, ingestion_date: str) -> bool:
    """Retourne True si clean.parquet existe déjà dans MinIO silver pour cette date."""
    key = f"ingestion_date={ingestion_date}/{indicateur}/{dataset_id}/clean.parquet"
    try:
        s3.head_object(Bucket=BUCKET_SILVER, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


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


# ─── Main ─────────────────────────────────────────────────────────────────────

def run(ingestion_date: str | None = None):
    _t0 = time.perf_counter()
    log.info("=" * 60)
    log.info("Silver Transformer")
    log.info("=" * 60)

    init_minio()

    if ingestion_date is None:
        ingestion_date = latest_ingestion_date()
        if not ingestion_date:
            raise RuntimeError("Aucune partition trouvée dans MinIO — lancer bronze_feeder d'abord")
    log.info(f"  Partition : ingestion_date={ingestion_date}")

    # Pré-chargement des référentiels géo dans le thread principal pour éviter
    # des doubles fetch HTTP si plusieurs threads les demandaient simultanément.
    log.info("  Pré-chargement des référentiels géo (arrondissements, quartiers, IRIS)...")
    _load_arrondissement_shapes()
    _load_quartier_shapes()
    _load_iris_shapes()
    log.info("  ✓ Référentiels géo prêts")

    results = []
    results_lock = threading.Lock()

    silver_items = list(SILVER_CONFIG.items())

    def _process_one(item):
        dataset_id, (collection, transformer, id_col, indicateur) = item
        if _already_silver_today(dataset_id, indicateur, ingestion_date):
            log.info(f"  ⏭  [{dataset_id}] déjà transformé pour {ingestion_date} — skip")
            return {"id": dataset_id, "indicateur": indicateur, "rows": 0, "status": "SKIP"}

        log.info(f"\n[{indicateur.upper()}] [{dataset_id}]")
        try:
            df = read_bronze(indicateur, dataset_id, ingestion_date)
            if df is None:
                return {"id": dataset_id, "status": "SKIPPED (absent bronze)"}

            df = transformer(df)

            if df.empty:
                log.warning(f"  [{dataset_id}] DataFrame vide après transformation")
                return {"id": dataset_id, "status": "VIDE après transform"}

            write_silver_minio(df, dataset_id, indicateur, ingestion_date)

            return {
                "id": dataset_id,
                "collection": collection,
                "indicateur": indicateur,
                "rows": len(df),
                "status": "OK",
                "has_geo": "location" in df.columns,
            }
        except Exception as e:
            log.error(f"  [{dataset_id}] ERREUR : {e}")
            return {"id": dataset_id, "status": f"ERREUR: {e}"}

    MAX_WORKERS = int(os.environ.get("SILVER_WORKERS", "5"))
    dataset_progress = tqdm(total=len(silver_items), desc="Silver datasets", unit="dataset")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_process_one, item): item for item in silver_items}
        for future in as_completed(futures):
            result = future.result()
            with results_lock:
                results.append(result)
            dataset_progress.set_postfix_str(result["id"])
            dataset_progress.update(1)
    dataset_progress.close()

    log.info(f"\n{'='*60}")
    log.info("RAPPORT SILVER")
    log.info(f"{'='*60}")
    _non_error = {"OK", "SKIP", "SKIPPED (absent bronze)", "VIDE après transform"}
    ok = [r for r in results if r["status"] == "OK"]
    skip = [r for r in results if r["status"] == "SKIP"]
    absent = [r for r in results if r["status"] == "SKIPPED (absent bronze)"]
    vide = [r for r in results if r["status"] == "VIDE après transform"]
    ko = [r for r in results if r["status"] not in _non_error]
    if skip:
        log.info(f"  ⏭  Skippés : {len(skip)} (déjà transformés aujourd'hui)")
    log.info(f"  ✅ OK      : {len(ok)}/{len(results)}")
    for r in ok:
        geo_tag = " [geo]" if r.get("has_geo") else ""
        log.info(f"    [{r.get('indicateur','?'):20}] {r['id']:40} {r['rows']:>8}  →  {r['collection']}{geo_tag}")
    if absent:
        log.info(f"  ⚠️  Absent bronze : {len(absent)}")
        for r in absent:
            log.info(f"    {r['id']}")
    if vide:
        log.info(f"  ⚠️  Vides : {len(vide)}")
        for r in vide:
            log.info(f"    {r['id']}")
    if ko:
        log.info(f"  ERREURS : {len(ko)}")
        for r in ko:
            log.info(f"    {r['id']} — {r['status']}")

    elapsed = time.perf_counter() - _t0
    total_docs = sum(r.get("rows", 0) for r in results if r.get("rows"))
    log.info(f"[PERF] silver_transformer : {elapsed:.1f}s — {len([r for r in results if r['status']=='OK'])} collections OK — {total_docs:,} documents MongoDB")
    log.info(f"{'='*60}\n")


if __name__ == "__main__":
    import sys
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    run(date_arg)
