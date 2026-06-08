"""
Bronze Layer Feeder — Indicateur Qualité de Vie Paris
Medallion Architecture : Raw data ingestion → MinIO bucket 'bronze'
"""

import json
import logging
import re
import struct
import pandas as pd
from io import BytesIO
from pathlib import Path
from datetime import date

# ─── Config ───────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SUPPORTED_LOCAL_EXTENSIONS = {".csv", ".parquet", ".shp"}
JSON_MAX_ROWS = 50_000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bronze_feeder")

# ─── MinIO ────────────────────────────────────────────────────────────────────

s3 = None
BUCKET = "bronze"


def get_s3_client():
    """Crée le client MinIO uniquement au moment de l'ingestion."""
    global s3
    if s3 is None:
        try:
            import boto3
        except ImportError as e:
            raise ImportError("boto3 est requis pour écrire dans MinIO") from e

        s3 = boto3.client(
            "s3",
            endpoint_url="http://localhost:9000",
            aws_access_key_id="admin",
            aws_secret_access_key="password123",
        )
    return s3

# ─── Dataset Discovery ────────────────────────────────────────────────────────

def _dataset_id_from_path(relative_path: Path) -> str:
    """Construit un id stable depuis le chemin relatif dans data/."""
    stem_path = relative_path.with_suffix("")
    raw_id = "_".join(stem_path.parts).lower()
    dataset_id = re.sub(r"[^a-z0-9]+", "_", raw_id).strip("_")
    return dataset_id or "dataset"


def discover_local_datasets() -> list[dict]:
    """Découvre tous les .csv, .parquet et .shp présents dans data/."""
    if not DATA_DIR.exists():
        raise FileNotFoundError(f"Dossier data introuvable : {DATA_DIR}")

    datasets = []
    for path in sorted(DATA_DIR.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_LOCAL_EXTENSIONS:
            continue

        relative_path = path.relative_to(DATA_DIR)
        datasets.append({
            "id": _dataset_id_from_path(relative_path),
            "label": str(relative_path),
            "signe": "non_classe",
            "source": "local_data",
            "local_file": relative_path.as_posix(),
            "format": path.suffix.lower().lstrip("."),
        })

    log.info(f"{len(datasets)} fichiers détectés dans {DATA_DIR}")
    return datasets

# ─── Loaders ──────────────────────────────────────────────────────────────────

def load_local(dataset: dict) -> pd.DataFrame:
    """Lit un fichier local depuis data/."""
    path = DATA_DIR / dataset["local_file"]
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
    elif ext == ".shp":
        return _read_shapefile(path)
    else:
        raise ValueError(f"Format non supporté : {ext}")


def _detect_sep(path: Path) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        first = f.readline()
    return ";" if first.count(";") > first.count(",") else ","


def _read_shapefile(path: Path) -> pd.DataFrame:
    """Lit un shapefile sans dépendance géospatiale externe, géométrie en WKT."""
    dbf_path = path.with_suffix(".dbf")
    if not dbf_path.exists():
        raise FileNotFoundError(f"Fichier DBF associé introuvable : {dbf_path.name}")

    records = _read_dbf(dbf_path)
    geometries = _read_shp_geometries(path)
    row_count = max(len(records), len(geometries))
    rows = []

    for idx in range(row_count):
        row = records[idx] if idx < len(records) else {}
        row = dict(row)
        row["geometry"] = geometries[idx] if idx < len(geometries) else None
        rows.append(row)

    return pd.DataFrame(rows)


def _read_dbf(path: Path) -> list[dict]:
    cpg_path = path.with_suffix(".cpg")
    encoding = cpg_path.read_text(encoding="utf-8", errors="replace").strip() if cpg_path.exists() else "cp1252"

    with open(path, "rb") as f:
        header = f.read(32)
        record_count = struct.unpack("<I", header[4:8])[0]
        header_length = struct.unpack("<H", header[8:10])[0]
        record_length = struct.unpack("<H", header[10:12])[0]

        fields = []
        while True:
            descriptor = f.read(32)
            if descriptor[0] == 0x0D:
                break

            name = descriptor[:11].split(b"\x00", 1)[0].decode(encoding, errors="replace")
            field_type = chr(descriptor[11])
            length = descriptor[16]
            decimals = descriptor[17]
            fields.append((name, field_type, length, decimals))

        f.seek(header_length)
        rows = []
        for _ in range(record_count):
            record = f.read(record_length)
            if not record or record[:1] == b"*":
                continue

            pos = 1
            row = {}
            for name, field_type, length, decimals in fields:
                raw = record[pos:pos + length]
                pos += length
                row[name] = _parse_dbf_value(raw, field_type, decimals, encoding)
            rows.append(row)

    return rows


def _parse_dbf_value(raw: bytes, field_type: str, decimals: int, encoding: str):
    value = raw.decode(encoding, errors="replace").strip()
    if value == "":
        return None

    if field_type in {"N", "F"}:
        try:
            return float(value) if decimals else int(value)
        except ValueError:
            return value
    if field_type == "D" and len(value) == 8:
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    if field_type == "L":
        return value.upper() in {"Y", "T", "1"}
    return value


def _read_shp_geometries(path: Path) -> list[str | None]:
    geometries = []
    with open(path, "rb") as f:
        f.seek(100)
        while True:
            record_header = f.read(8)
            if len(record_header) < 8:
                break

            content_length = struct.unpack(">i", record_header[4:8])[0] * 2
            content = f.read(content_length)
            if len(content) < 4:
                geometries.append(None)
                continue

            shape_type = struct.unpack("<i", content[:4])[0]
            geometries.append(_shape_content_to_wkt(shape_type, content))

    return geometries


def _shape_content_to_wkt(shape_type: int, content: bytes) -> str | None:
    if shape_type == 0:
        return None
    if shape_type in {1, 11, 21}:
        x, y = struct.unpack("<2d", content[4:20])
        return f"POINT ({x} {y})"
    if shape_type in {8, 18, 28}:
        points = _read_points(content, offset=40)
        return _points_to_wkt("MULTIPOINT", [points])
    if shape_type in {3, 13, 23}:
        parts = _read_parts(content)
        return _points_to_wkt("MULTILINESTRING", parts)
    if shape_type in {5, 15, 25}:
        parts = _read_parts(content)
        return _points_to_wkt("POLYGON", parts)
    return None


def _read_parts(content: bytes) -> list[list[tuple[float, float]]]:
    num_parts, num_points = struct.unpack("<2i", content[36:44])
    part_offsets = list(struct.unpack(f"<{num_parts}i", content[44:44 + (num_parts * 4)]))
    points_offset = 44 + (num_parts * 4)
    points = _read_points(content, points_offset, num_points)

    parts = []
    for idx, start in enumerate(part_offsets):
        end = part_offsets[idx + 1] if idx + 1 < len(part_offsets) else len(points)
        parts.append(points[start:end])
    return parts


def _read_points(content: bytes, offset: int, count: int | None = None) -> list[tuple[float, float]]:
    if count is None:
        count = struct.unpack("<i", content[36:40])[0]

    points = []
    for idx in range(count):
        start = offset + (idx * 16)
        points.append(struct.unpack("<2d", content[start:start + 16]))
    return points


def _points_to_wkt(kind: str, parts: list[list[tuple[float, float]]]) -> str:
    formatted_parts = []
    for points in parts:
        formatted_points = ", ".join(f"{x} {y}" for x, y in points)
        formatted_parts.append(f"({formatted_points})")

    if kind == "POLYGON":
        return f"POLYGON ({', '.join(formatted_parts)})"
    return f"{kind} ({', '.join(formatted_parts)})"


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
    client = get_s3_client()
    df = _sanitize_for_export(df)
    prefix = _key_prefix(ingestion_date, dataset)

    # Parquet — toujours
    buf = BytesIO()
    df.to_parquet(buf, index=False)
    buf.seek(0)
    client.put_object(Bucket=BUCKET, Key=f"{prefix}/raw.parquet", Body=buf.getvalue())
    log.info(f"    ✓ MinIO → {prefix}/raw.parquet ({len(df)} lignes)")

    # JSON — seulement pour les petits datasets
    if len(df) <= JSON_MAX_ROWS:
        body = df.to_json(orient="records", force_ascii=False, indent=2).encode("utf-8")
        client.put_object(Bucket=BUCKET, Key=f"{prefix}/raw.json", Body=body)
        log.info(f"    ✓ MinIO → {prefix}/raw.json")
    else:
        log.info(f"    ⏭  raw.json ignoré ({len(df)} lignes > {JSON_MAX_ROWS})")


def write_meta_minio(df: pd.DataFrame, dataset: dict, ingestion_date: str):
    """Écrit _meta.json dans MinIO."""
    client = get_s3_client()
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
    client.put_object(Bucket=BUCKET, Key=f"{prefix}/_meta.json", Body=body)
    log.info(f"    ✓ MinIO → {prefix}/_meta.json")


# ─── Init ─────────────────────────────────────────────────────────────────────

def init_minio():
    """Vérifie la connexion MinIO et crée le bucket bronze si absent."""
    log.info("Connexion MinIO...")
    client = get_s3_client()
    try:
        existing = [b["Name"] for b in client.list_buckets().get("Buckets", [])]
    except Exception as e:
        raise RuntimeError(f"MinIO inaccessible sur http://localhost:9000 — docker-compose up ? ({e})")

    if BUCKET not in existing:
        client.create_bucket(Bucket=BUCKET)
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

    datasets = discover_local_datasets()
    results = []

    for ds in datasets:
        log.info(f"\n[{ds['signe'].upper()}] {ds['label']}")

        try:
            df = load_local(ds)

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
    get_s3_client().put_object(Bucket=BUCKET, Key=report_key, Body=body)
    log.info(f"\n  Rapport sauvegardé : s3://{BUCKET}/{report_key}")
    log.info(f"{'='*60}\n")

if __name__ == "__main__":
    run()
