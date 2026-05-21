"""
Bronze Layer Feeder — Indicateur Qualité de Vie Paris
Medallion Architecture : Raw data ingestion → MinIO bucket 'bronze'
"""

import json
import logging
import requests
import pandas as pd
import boto3
from io import BytesIO
from pathlib import Path
from datetime import date

# ─── Config ───────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
DATASRC = ROOT / "datasrc"
PARIS_API = "https://parisdata.opendatasoft.com/api/explore/v2.1/catalog/datasets"
PAGE_SIZE = 100
JSON_MAX_ROWS = 50_000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bronze_feeder")

# ─── MinIO ────────────────────────────────────────────────────────────────────

s3 = boto3.client(
    "s3",
    endpoint_url="http://localhost:9000",
    aws_access_key_id="admin",
    aws_secret_access_key="password123",
)
BUCKET = "bronze"

# ─── Dataset Registry ─────────────────────────────────────────────────────────

DATASETS = [
    # ── POSITIF ──────────────────────────────────────────────────────────────
    {
        "id": "ilots_fraicheur_espaces_verts",
        "label": "Îlots de fraîcheur — Espaces verts frais",
        "signe": "positif",
        "source": "paris_opendata",
        "local_file": "ilots-de-fraicheur-espaces-verts-frais.parquet",
        "api_dataset_id": "ilots-de-fraicheur-espaces-verts-frais",
    },
    {
        "id": "arbres",
        "label": "Arbres de Paris",
        "signe": "positif",
        "source": "paris_opendata",
        "local_file": "les-arbres.parquet",
        "api_dataset_id": "les-arbres",
    },
    {
        "id": "ilots_fraicheur_equipements",
        "label": "Îlots de fraîcheur — Équipements & Activités",
        "signe": "positif",
        "source": "paris_opendata",
        "local_file": "ilots-de-fraicheur-equipements-activites.parquet",
        "api_dataset_id": "ilots-de-fraicheur-equipements-activites",
    },
    {
        "id": "qualite_air",
        "label": "Qualité de l'air — NO2 PM2.5 PM10 O3",
        "signe": "positif",
        "source": "datagouv",
        "local_file": "qualite-de-lair-concentration-moyenne-no2-pm25-pm10-o3-a-partir-de-2015.json",
    },
    {
        "id": "fibre_actuel",
        "label": "Fibre — Déploiement actuel Paris 75",
        "signe": "positif",
        "source": "datagouv",
        "local_file": "actuel_75.csv",
    },
    {
        "id": "fibre_base_imb",
        "label": "Fibre — Base immeubles Paris 75",
        "signe": "positif",
        "source": "datagouv",
        "local_file": "base_imb_75.csv",
    },
    {
        "id": "fibre_base_imb_fc",
        "label": "Fibre — Base immeubles fibre coaxiale Paris 75",
        "signe": "positif",
        "source": "datagouv",
        "local_file": "base_imb_fc_75.csv",
    },
    {
        "id": "fibre_debit_filaire",
        "label": "Fibre — Débit filaire par département",
        "signe": "positif",
        "source": "datagouv",
        "local_file": "departement_debit_filaire.csv",
    },
    {
        "id": "fibre_operateur",
        "label": "Fibre — Opérateurs",
        "signe": "positif",
        "source": "datagouv",
        "local_file": "operateur.csv",
    },
    # ── NÉGATIF ──────────────────────────────────────────────────────────────
    {
        "id": "sanisettes",
        "label": "Sanisettes publiques",
        "signe": "negatif",
        "source": "paris_opendata",
        "api_dataset_id": "sanisettesparis",
        "api_max_records": 600,
    },
    {
        "id": "trafic_routier",
        "label": "Comptages routiers permanents",
        "signe": "negatif",
        "source": "paris_opendata",
        "api_dataset_id": "comptages-routiers-permanents",
        "api_max_records": 500,
    },
    {
        "id": "chantiers",
        "label": "Chantiers à Paris",
        "signe": "negatif",
        "source": "paris_opendata",
        "api_dataset_id": "chantiers-a-paris",
        "api_max_records": 1000,
    },
    {
        "id": "anomalies",
        "label": "Dans ma rue — Anomalies signalées",
        "signe": "negatif",
        "source": "paris_opendata",
        "api_dataset_id": "dans-ma-rue",
        "api_max_records": 2000,
    },
    {
        "id": "zones_touristiques",
        "label": "Zones touristiques internationales",
        "signe": "negatif",
        "source": "paris_opendata",
        "api_dataset_id": "zones-touristiques-internationales",
        "api_max_records": 100,
    },
    {
        "id": "terrasses",
        "label": "Terrasses autorisées",
        "signe": "negatif",
        "source": "paris_opendata",
        "api_dataset_id": "terrasses-autorisations",
        "api_max_records": 2000,
    },
]

# ─── Loaders ──────────────────────────────────────────────────────────────────

def load_local(dataset: dict) -> pd.DataFrame:
    """Lit un fichier local depuis datasrc/."""
    path = DATASRC / dataset["local_file"]
    ext = path.suffix.lower()
    log.info(f"  Lecture locale : {path.name}")
    if ext == ".parquet":
        return pd.read_parquet(path)
    elif ext == ".csv":
        sep = _detect_sep(path)
        return pd.read_csv(path, sep=sep, low_memory=False, encoding_errors="replace")
    elif ext == ".json":
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return pd.DataFrame(raw) if isinstance(raw, list) else pd.json_normalize(raw)
    else:
        raise ValueError(f"Format non supporté : {ext}")


def _detect_sep(path: Path) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        first = f.readline()
    return ";" if first.count(";") > first.count(",") else ","


def fetch_api(dataset: dict) -> pd.DataFrame:
    """Fetche depuis l'API Paris OpenDataSoft avec pagination."""
    dataset_id = dataset["api_dataset_id"]
    max_records = dataset.get("api_max_records", 500)
    records, offset, total = [], 0, None

    log.info(f"  Fetch API : {dataset_id} (max {max_records})")
    while True:
        r = requests.get(
            f"{PARIS_API}/{dataset_id}/records",
            params={"limit": PAGE_SIZE, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if total is None:
            total = data.get("total_count", 0)
        batch = data.get("results", [])
        if not batch:
            break
        records.extend(batch)
        offset += len(batch)
        if offset >= min(max_records, total):
            break

    df = pd.json_normalize(records)
    log.info(f"    → {len(df)} / {total} lignes récupérées")
    return df


# ─── Writers ──────────────────────────────────────────────────────────────────

def add_meta_columns(df: pd.DataFrame, dataset: dict, ingestion_date: str) -> pd.DataFrame:
    """Ajoute les colonnes de traçabilité bronze."""
    df = df.copy()
    df["_ingested_at"] = ingestion_date
    df["_dataset_id"] = dataset["id"]
    df["_signe"] = dataset["signe"]
    df["_source"] = dataset["source"]
    return df


def _sanitize_for_export(df: pd.DataFrame) -> pd.DataFrame:
    """
    Prépare le DataFrame pour la sérialisation :
    - Colonnes bytes → str
    - Colonnes avec listes/dicts mixtes (géométries GeoJSON) → JSON string
    """
    df = df.copy()
    for col in df.columns:
        sample = df[col].dropna()
        if sample.empty:
            continue
        first = sample.iloc[0]
        if isinstance(first, bytes):
            # hex préserve les WKB géo pour décodage Silver — UTF-8 les détruirait
            df[col] = df[col].apply(lambda v: v.hex() if isinstance(v, bytes) else v)
        elif isinstance(first, (list, dict)):
            df[col] = df[col].apply(lambda v: json.dumps(v, ensure_ascii=False) if v is not None else None)
    return df


def _key_prefix(ingestion_date: str, dataset: dict) -> str:
    return f"ingestion_date={ingestion_date}/{dataset['source']}/{dataset['id']}"


def save_bronze_minio(df: pd.DataFrame, dataset: dict, ingestion_date: str):
    """Écrit raw.parquet (+ raw.json pour petits datasets) dans MinIO."""
    df = _sanitize_for_export(df)
    prefix = _key_prefix(ingestion_date, dataset)

    # Parquet — toujours
    buf = BytesIO()
    df.to_parquet(buf, index=False)
    buf.seek(0)
    s3.put_object(Bucket=BUCKET, Key=f"{prefix}/raw.parquet", Body=buf.getvalue())
    log.info(f"    ✓ MinIO → {prefix}/raw.parquet ({len(df)} lignes)")

    # JSON — seulement pour les petits datasets
    if len(df) <= JSON_MAX_ROWS:
        body = df.to_json(orient="records", force_ascii=False, indent=2).encode("utf-8")
        s3.put_object(Bucket=BUCKET, Key=f"{prefix}/raw.json", Body=body)
        log.info(f"    ✓ MinIO → {prefix}/raw.json")
    else:
        log.info(f"    ⏭  raw.json ignoré ({len(df)} lignes > {JSON_MAX_ROWS})")


def write_meta_minio(df: pd.DataFrame, dataset: dict, ingestion_date: str):
    """Écrit _meta.json dans MinIO."""
    meta = {
        "dataset_id": dataset["id"],
        "label": dataset["label"],
        "signe": dataset["signe"],
        "source": dataset["source"],
        "ingestion_date": ingestion_date,
        "row_count": len(df),
        "columns": df.columns.tolist(),
        "dtypes": {c: str(t) for c, t in df.dtypes.items()},
        "null_counts": df.isnull().sum().to_dict(),
    }
    prefix = _key_prefix(ingestion_date, dataset)
    body = json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")
    s3.put_object(Bucket=BUCKET, Key=f"{prefix}/_meta.json", Body=body)
    log.info(f"    ✓ MinIO → {prefix}/_meta.json")


# ─── Init ─────────────────────────────────────────────────────────────────────

def init_minio():
    """Vérifie la connexion MinIO et crée le bucket bronze si absent."""
    log.info("Connexion MinIO...")
    try:
        existing = [b["Name"] for b in s3.list_buckets().get("Buckets", [])]
    except Exception as e:
        raise RuntimeError(f"MinIO inaccessible sur http://localhost:9000 — docker-compose up ? ({e})")

    if BUCKET not in existing:
        s3.create_bucket(Bucket=BUCKET)
        log.info(f"  ✓ Bucket '{BUCKET}' créé")
    else:
        log.info(f"  ✓ Bucket '{BUCKET}' existant")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run():
    ingestion_date = str(date.today())
    log.info(f"{'='*60}")
    log.info(f"Bronze Feeder — {ingestion_date}")
    log.info(f"{'='*60}")

    init_minio()

    results = []

    for ds in DATASETS:
        log.info(f"\n[{ds['signe'].upper()}] {ds['label']}")

        try:
            if ds.get("local_file"):
                df = load_local(ds)
            else:
                df = fetch_api(ds)

            df = add_meta_columns(df, ds, ingestion_date)

            save_bronze_minio(df, ds, ingestion_date)
            write_meta_minio(df, ds, ingestion_date)

            results.append({
                "id": ds["id"],
                "signe": ds["signe"],
                "rows": len(df),
                "status": "OK",
                "minio_prefix": _key_prefix(ingestion_date, ds),
            })

        except Exception as e:
            log.error(f"  ERREUR : {e}")
            results.append({"id": ds["id"], "signe": ds["signe"], "rows": 0, "status": f"ERREUR: {e}"})

    # ── Rapport final ──────────────────────────────────────────────────────
    log.info(f"\n{'='*60}")
    log.info("RAPPORT D'INGESTION BRONZE")
    log.info(f"{'='*60}")
    ok = [r for r in results if r["status"] == "OK"]
    ko = [r for r in results if r["status"] != "OK"]
    log.info(f"  ✅ Succès : {len(ok)}/{len(results)}")
    for r in ok:
        log.info(f"    [{r['signe']:7}] {r['id']:40} {r['rows']:>8} lignes")
    if ko:
        log.info(f"  ❌ Erreurs : {len(ko)}")
        for r in ko:
            log.info(f"    {r['id']} — {r['status']}")

    report_key = f"ingestion_date={ingestion_date}/_ingestion_report.json"
    body = json.dumps({"ingestion_date": ingestion_date, "datasets": results}, indent=2).encode("utf-8")
    s3.put_object(Bucket=BUCKET, Key=report_key, Body=body)
    log.info(f"\n  Rapport sauvegardé : s3://{BUCKET}/{report_key}")
    log.info(f"{'='*60}\n")


if __name__ == "__main__":
    run()
