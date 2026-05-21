"""
Silver Layer Transformer — Indicateur Qualité de Vie Paris
Bronze (MinIO "bronze") → Silver (MinIO "silver" Parquet + MongoDB documents)
"""

import json
import logging
import struct
import re
import pandas as pd
import boto3
from io import BytesIO
from datetime import date
from pymongo import MongoClient, UpdateOne
from botocore.exceptions import ClientError

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
    endpoint_url="http://localhost:9000",
    aws_access_key_id="admin",
    aws_secret_access_key="password123",
)
BUCKET_BRONZE = "bronze"
BUCKET_SILVER = "silver"

# ─── MongoDB ──────────────────────────────────────────────────────────────────

mongo = MongoClient("mongodb://localhost:27017")
db = mongo["silver"]

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
    Gère "75017", "75117" (code INSEE), "PARIS 17E ARRDT"
    """
    if pd.isna(val):
        return None
    s = str(val).strip()
    # "75017" ou "75117" (code INSEE arrondissement)
    m = re.match(r"^75[01](\d{2})$", s)
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 20 else None
    # "PARIS 17E ARRDT" → extrait premier nombre
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


# ─── Transformers ─────────────────────────────────────────────────────────────

def _base_geo(df: pd.DataFrame) -> pd.DataFrame:
    """Appliqué à tous les datasets : décode geo_point_2d + normalise arrondissement."""
    if "geo_point_2d" in df.columns:
        df["location"] = df["geo_point_2d"].apply(wkb_hex_to_geojson)
        df = df.drop(columns=["geo_point_2d"])
    # geo_shape (polygones) : trop lourd pour Silver, retiré
    if "geo_shape" in df.columns:
        df = df.drop(columns=["geo_shape"])
    if "arrondissement" in df.columns:
        df["arrondissement"] = df["arrondissement"].apply(parse_arrondissement)
    return df


def transform_espaces_verts(df: pd.DataFrame) -> pd.DataFrame:
    df = _base_geo(df)
    keep = [
        "identifiant", "nom", "type", "arrondissement",
        "statut_ouverture", "ouvert_24h", "adresse", "location",
        "_ingested_at", "_dataset_id", "_signe", "_source",
    ]
    return df[[c for c in keep if c in df.columns]]


def transform_equipements(df: pd.DataFrame) -> pd.DataFrame:
    df = _base_geo(df)
    keep = [
        "identifiant", "nom", "type", "payant", "arrondissement",
        "statut_ouverture", "adresse", "location",
        "_ingested_at", "_dataset_id", "_signe", "_source",
    ]
    return df[[c for c in keep if c in df.columns]]


def transform_arbres(df: pd.DataFrame) -> pd.DataFrame:
    df = _base_geo(df)
    keep = [
        "idbase", "libellefrancais", "genre", "espece", "domanialite",
        "arrondissement", "adresse", "hauteurencm", "circonferenceencm",
        "location", "_ingested_at", "_dataset_id", "_signe", "_source",
    ]
    return df[[c for c in keep if c in df.columns]]


def transform_qualite_air(df: pd.DataFrame) -> pd.DataFrame:
    # Données annuelles Paris-wide : pas de géo
    num_cols = df.select_dtypes(include="number").columns.tolist()
    keep = ["annee"] + num_cols + ["_ingested_at", "_dataset_id", "_signe", "_source"]
    return df[[c for c in keep if c in df.columns]]


def transform_fibre_imb(df: pd.DataFrame) -> pd.DataFrame:
    """base_imb_75 : coordonnées Lambert-93 → WGS84, code_insee → arrondissement."""
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
    """Datasets API Paris : geo_point_2d WKB + arrondissement."""
    return _base_geo(df)


def transform_passthrough(df: pd.DataFrame) -> pd.DataFrame:
    return df


# ─── Dataset registry ─────────────────────────────────────────────────────────
# (collection_mongo, transformer, colonne_id_pour_upsert)

SILVER_CONFIG = {
    "ilots_fraicheur_espaces_verts": ("silver_espaces_verts",    transform_espaces_verts, "identifiant"),
    "ilots_fraicheur_equipements":   ("silver_equipements",      transform_equipements,   "identifiant"),
    "arbres":                        ("silver_arbres",           transform_arbres,        "idbase"),
    "qualite_air":                   ("silver_qualite_air",      transform_qualite_air,   "annee"),
    "fibre_actuel":                  ("silver_fibre_actuel",     transform_passthrough,   None),
    "fibre_base_imb":                ("silver_fibre_imb",        transform_fibre_imb,     "imb_id"),
    "fibre_base_imb_fc":             ("silver_fibre_imb_fc",     transform_passthrough,   "immeuble_id"),
    "fibre_debit_filaire":           ("silver_fibre_debit",      transform_passthrough,   "code_dep"),
    "fibre_operateur":               ("silver_fibre_operateur",  transform_passthrough,   "code"),
    "sanisettes":                    ("silver_sanisettes",       transform_api_geo,       None),
    "trafic_routier":                ("silver_trafic",           transform_api_geo,       None),
    "chantiers":                     ("silver_chantiers",        transform_api_geo,       None),
    "anomalies":                     ("silver_anomalies",        transform_api_geo,       None),
    "zones_touristiques":            ("silver_zones_touristiques", transform_api_geo,     None),
    "terrasses":                     ("silver_terrasses",        transform_api_geo,       None),
}

# ─── MinIO reader ─────────────────────────────────────────────────────────────

def latest_ingestion_date() -> str | None:
    """Trouve la partition ingestion_date= la plus récente dans MinIO."""
    paginator = s3.get_paginator("list_objects_v2")
    dates = set()
    for page in paginator.paginate(Bucket=BUCKET_BRONZE, Delimiter="/"):
        for p in page.get("CommonPrefixes", []):
            m = re.match(r"ingestion_date=(\d{4}-\d{2}-\d{2})/", p["Prefix"])
            if m:
                dates.add(m.group(1))
    return max(dates) if dates else None


def read_bronze(source: str, dataset_id: str, ingestion_date: str) -> pd.DataFrame | None:
    """Lit raw.parquet depuis MinIO bronze."""
    key = f"ingestion_date={ingestion_date}/{source}/{dataset_id}/raw.parquet"
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

def write_silver_minio(df: pd.DataFrame, dataset_id: str, source: str, ingestion_date: str):
    """Écrit le Parquet nettoyé dans MinIO bucket 'silver'."""
    key = f"ingestion_date={ingestion_date}/{source}/{dataset_id}/clean.parquet"
    buf = BytesIO()
    # location GeoJSON (dict) → JSON string pour compatibilité Parquet
    df_out = df.copy()
    if "location" in df_out.columns:
        df_out["location"] = df_out["location"].apply(
            lambda v: json.dumps(v) if isinstance(v, dict) else v
        )
    df_out.to_parquet(buf, index=False)
    buf.seek(0)
    s3.put_object(Bucket=BUCKET_SILVER, Key=key, Body=buf.getvalue())
    log.info(f"    ✓ MinIO silver → {key}")


def write_silver_mongo(df: pd.DataFrame, collection_name: str, id_col: str | None):
    """Upsert les documents dans MongoDB silver."""
    coll = db[collection_name]
    records = df.where(pd.notna(df), other=None).to_dict("records")

    if id_col and id_col in df.columns:
        ops = [UpdateOne({id_col: r[id_col]}, {"$set": r}, upsert=True) for r in records]
        result = coll.bulk_write(ops, ordered=False)
        log.info(f"    ✓ MongoDB {collection_name} — upserted={result.upserted_count} modified={result.modified_count}")
    else:
        coll.delete_many({"_ingested_at": records[0].get("_ingested_at")} if records else {})
        coll.insert_many(records)
        log.info(f"    ✓ MongoDB {collection_name} — {len(records)} insérés")

    if "location" in df.columns:
        coll.create_index([("location", "2dsphere")])
    if "arrondissement" in df.columns:
        coll.create_index("arrondissement")


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


# ─── Source map (dataset_id → source) ─────────────────────────────────────────
# Reprend les valeurs du bronze_feeder DATASETS

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
    "trafic_routier":                "paris_opendata",
    "chantiers":                     "paris_opendata",
    "anomalies":                     "paris_opendata",
    "zones_touristiques":            "paris_opendata",
    "terrasses":                     "paris_opendata",
}

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

    for dataset_id, (collection, transformer, id_col) in SILVER_CONFIG.items():
        log.info(f"\n[{dataset_id}]")
        source = SOURCE_MAP[dataset_id]

        try:
            df = read_bronze(source, dataset_id, ingestion_date)
            if df is None:
                results.append({"id": dataset_id, "status": "SKIPPED (absent bronze)"})
                continue

            df = transformer(df)
            write_silver_minio(df, dataset_id, source, ingestion_date)
            write_silver_mongo(df, collection, id_col)

            results.append({
                "id": dataset_id,
                "collection": collection,
                "rows": len(df),
                "status": "OK",
                "has_geo": "location" in df.columns,
            })

        except Exception as e:
            log.error(f"  ERREUR : {e}")
            results.append({"id": dataset_id, "status": f"ERREUR: {e}"})

    # ── Rapport ────────────────────────────────────────────────────────────
    log.info(f"\n{'='*60}")
    log.info("RAPPORT SILVER")
    log.info(f"{'='*60}")
    ok = [r for r in results if r["status"] == "OK"]
    ko = [r for r in results if r["status"] not in ("OK",) and not r["status"].startswith("SKIPPED")]
    skipped = [r for r in results if r["status"].startswith("SKIPPED")]
    log.info(f"  OK      : {len(ok)}/{len(results)}")
    for r in ok:
        geo_tag = " [geo]" if r.get("has_geo") else ""
        log.info(f"    {r['id']:40} {r['rows']:>8} lignes  →  {r['collection']}{geo_tag}")
    if skipped:
        log.info(f"  SKIPPED : {len(skipped)}")
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
