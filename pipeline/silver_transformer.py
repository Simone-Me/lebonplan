"""
Silver Layer Transformer — Urban Data Explorer Paris
Bronze (MinIO "bronze") → Silver (MinIO "silver" Parquet + MongoDB documents)
"""

import json
import logging
import struct
import re
import pandas as pd
import boto3
from io import BytesIO
from pymongo import MongoClient, UpdateOne
from botocore.exceptions import ClientError

from config import (
    MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY,
    BUCKET_BRONZE, BUCKET_SILVER,
    MONGO_URI, MONGO_DB,
)

# ─── Config ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("silver_transformer")

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
                result.append({"type": "Point", "coordinates": [float(lon), float(lat)]})
            else:
                result.append(None)
        except Exception:
            result.append(None)
    return result


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
    for lat_c, lon_c in [("lat", "lon"), ("latitude", "longitude"), ("ylatitude", "xlongitude")]:
        if lat_c in df.columns and lon_c in df.columns and "location" not in df.columns:
            df["location"] = latlon_cols_to_geojson(df[lat_c], df[lon_c])
    # Arrondissement
    for col in ["arrondissement", "cp_arrondissement", "code_postal", "arr_insee", "arr_libelle"]:
        if col in df.columns:
            df["arrondissement"] = df[col].apply(parse_arrondissement)
            break
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

    before = len(df)

    # 1. Code postal commence par 75
    for cp_col in ["code_postal", "cp", "codepostal", "code_commune_insee"]:
        if cp_col in df.columns:
            df = df[df[cp_col].astype(str).str.startswith("75")]
            if len(df) < before:
                return df

    # 2. Colonne département numérique (ex: "75") ou textuelle (ex: "Paris")
    for dep_col in ["departement", "dep", "code_dep", "num_dep"]:
        if dep_col in df.columns:
            mask = (
                df[dep_col].astype(str).str.startswith("75") |
                df[dep_col].astype(str).str.lower().str.contains("paris")
            )
            df = df[mask]
            if len(df) < before:
                return df

    # 3. Colonne ville/commune textuelle contenant "Paris" (ex: arrtown, town, ville)
    for town_col in ["arrtown", "town", "ville", "city_name", "commune_name"]:
        if town_col in df.columns:
            mask = df[town_col].astype(str).str.contains("Paris", case=False, na=False)
            df = df[mask]
            if len(df) < before:
                return df

    # 4. Bbox géographique Paris (fallback)
    if "location" in df.columns:
        def in_paris(loc):
            if not isinstance(loc, dict):
                return True
            coords = loc.get("coordinates", [])
            if len(coords) == 2:
                lon, lat = coords
                return 2.22 <= lon <= 2.47 and 48.81 <= lat <= 48.91
            return True
        df = df[df["location"].apply(in_paris)]

    return df


def transform_idf_transport(df: pd.DataFrame) -> pd.DataFrame:
    """
    Datasets transport IDF : filtre Paris + schéma Silver minimal.
    Normalise quelques alias fréquents pour éviter de conserver tout le payload Bronze.
    """
    df = transform_idf_geo(df)

    aliases = {
        "identifiant": ["id", "id_ref_zdl", "id_refa", "objectid", "stop_id", "stopareaid"],
        "nom": ["nom", "nomlong", "nom_gare", "nom_arret", "libelle", "stopname", "name"],
        "mode_transport": ["mode", "transportmode", "modeprincipa", "mode_principal", "type_arret", "type"],
        "ligne": ["ligne", "nomligne", "res_com", "res_stif", "line"],
        "commune": ["commune", "nom_commune", "nomcommune", "city"],
        "code_postal": ["code_postal", "cp", "codepostal", "postal_code"],
    }
    for target, candidates in aliases.items():
        df = _rename_first_existing(df, target, candidates)

    keep = [
        "identifiant", "nom", "mode_transport", "ligne",
        "commune", "code_postal", "arrondissement", "location",
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

    return _base_geo(df)


def transform_passthrough(df: pd.DataFrame) -> pd.DataFrame:
    return df


def transform_dvf(df: pd.DataFrame) -> pd.DataFrame:
    """
    DVF+ Etalab : extrait prix médian m², arrondissement et année.
    Colonnes clés : annee, code_insee, prix_m2_median (ou med_prix_m2_ventes)
    """
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
        for i in range(0, len(records), BULK_CHUNK):
            chunk = records[i:i + BULK_CHUNK]
            coll.insert_many(chunk, ordered=False)
            total += len(chunk)
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

    for dataset_id, (collection, transformer, id_col, indicateur) in SILVER_CONFIG.items():
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
