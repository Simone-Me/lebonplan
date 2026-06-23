"""
Bronze Layer Feeder — Urban Data Explorer Paris
Medallion Architecture : Raw data ingestion → MinIO bucket 'bronze'

Formats ingérés (RNCP variété) :
  - Parquet local        : pd.read_parquet()
  - CSV local            : _detect_sep() + pd.read_csv()
  - JSON local           : json.load() + pd.json_normalize()
  - API OpenDataSoft     : fetch_api() paginé offset
  - API IDF / transport  : fetch_api_generic() paginé
  - GeoJSON direct       : requests.get() + parsing features
"""

import json
import logging
import re
import sys
import requests
import pandas as pd
import boto3
from io import BytesIO
from pathlib import Path
from datetime import date
from tempfile import SpooledTemporaryFile

# Permet l'execution directe via `python pipeline/bronze_feeder.py`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.config import (
    MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY,
    BUCKET_BRONZE, PARIS_API, IDF_API,
)
from pipeline.progress_utils import tqdm

# ─── Config ───────────────────────────────────────────────────────────────────

DATASRC = ROOT / "datasrc"
PAGE_SIZE = 100
JSON_MAX_ROWS = 50_000
EXPORT_CHUNK_SIZE = 1024 * 1024
EXPORT_SPOOL_MAX_SIZE = 25 * 1024 * 1024

FILOSOFI_URL = (
    "https://static.data.gouv.fr/resources/principaux-indicateurs-sur-la-pauvrete-en-2021"
    "-niveau-de-vie-taux-de-pauvrete-part-des-menages-imposes-et-decomposition-du-revenu"
    "-disponible-1/20260415-150720/ds-filosofi-cc-2021-data.csv"
)
_PARIS_ARM_CODES = {f"75{str(i).zfill(3)}" for i in range(101, 121)}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bronze_feeder")

# ─── MinIO ────────────────────────────────────────────────────────────────────

s3 = boto3.client(
    "s3",
    endpoint_url=MINIO_ENDPOINT,
    aws_access_key_id=MINIO_ACCESS_KEY,
    aws_secret_access_key=MINIO_SECRET_KEY,
)
BUCKET = BUCKET_BRONZE

# ─── Dataset Registry ─────────────────────────────────────────────────────────

DATASETS = [
    # ══════════════════════════════════════════════════════════════════════════
    # INDICATEUR 1 — QUALITÉ DE VIE
    # ══════════════════════════════════════════════════════════════════════════
    {
        "id": "ilots_fraicheur_espaces_verts",
        "label": "Îlots de fraîcheur — Espaces verts frais",
        "indicateur": "qualite_vie",
        "signe": "positif",
        "source": "paris_opendata",
        "local_file": "ilots-de-fraicheur-espaces-verts-frais.parquet",
        "api_dataset_id": "ilots-de-fraicheur-espaces-verts-frais",
        "format_source": "parquet",
    },
    {
        "id": "arbres",
        "label": "Arbres de Paris",
        "indicateur": "qualite_vie",
        "signe": "positif",
        "source": "paris_opendata",
        "local_file": "les-arbres.parquet",
        "api_dataset_id": "les-arbres",
        "format_source": "parquet",
    },
    {
        "id": "ilots_fraicheur_equipements",
        "label": "Îlots de fraîcheur — Équipements & Activités",
        "indicateur": "qualite_vie",
        "signe": "positif",
        "source": "paris_opendata",
        "local_file": "ilots-de-fraicheur-equipements-activites.parquet",
        "api_dataset_id": "ilots-de-fraicheur-equipements-activites",
        "format_source": "parquet",
    },
    {
        "id": "qualite_air",
        "label": "Qualité de l'air — NO2 PM2.5 PM10 O3",
        "indicateur": "qualite_vie",
        "signe": "positif",
        "source": "datagouv",
        "local_file": "qualite-de-lair-concentration-moyenne-no2-pm25-pm10-o3-a-partir-de-2015.json",
        "format_source": "json",
    },
    {
        "id": "fibre_actuel",
        "label": "Fibre — Déploiement actuel Paris 75",
        "indicateur": "qualite_vie",
        "signe": "positif",
        "source": "datagouv",
        "local_file": "actuel_75.csv",
        "format_source": "csv",
    },
    {
        "id": "fibre_base_imb",
        "label": "Fibre — Base immeubles Paris 75",
        "indicateur": "qualite_vie",
        "signe": "positif",
        "source": "datagouv",
        "local_file": "base_imb_75.csv",
        "format_source": "csv",
    },
    {
        "id": "fibre_base_imb_fc",
        "label": "Fibre — Base immeubles fibre coaxiale Paris 75",
        "indicateur": "qualite_vie",
        "signe": "positif",
        "source": "datagouv",
        "local_file": "base_imb_fc_75.csv",
        "format_source": "csv",
    },
    {
        "id": "fibre_debit_filaire",
        "label": "Fibre — Débit filaire par département",
        "indicateur": "qualite_vie",
        "signe": "positif",
        "source": "datagouv",
        "local_file": "departement_debit_filaire.csv",
        "format_source": "csv",
    },
    {
        "id": "fibre_operateur",
        "label": "Fibre — Opérateurs",
        "indicateur": "qualite_vie",
        "signe": "positif",
        "source": "datagouv",
        "local_file": "operateur.csv",
        "format_source": "csv",
    },
    {
        "id": "sanisettes",
        "label": "Sanisettes publiques",
        "indicateur": "qualite_vie",
        "signe": "negatif",
        "source": "paris_opendata",
        "api_dataset_id": "sanisettesparis",
        # "api_max_records": 600,
        "format_source": "api_opendata",
    },
    {
        "id": "chantiers",
        "label": "Chantiers à Paris",
        "indicateur": "qualite_vie",
        "signe": "negatif",
        "source": "paris_opendata",
        "api_dataset_id": "chantiers-a-paris",
        # "api_max_records": 1000,
        "format_source": "api_opendata",
    },
    {
        "id": "anomalies",
        "label": "Dans ma rue — Anomalies signalées",
        "indicateur": "qualite_vie",
        "signe": "negatif",
        "source": "paris_opendata",
        "api_dataset_id": "dans-ma-rue",
        # "api_max_records": 2000,
        "format_source": "api_opendata",
    },
    {
        "id": "zones_touristiques",
        "label": "Zones touristiques internationales",
        "indicateur": "qualite_vie",
        "signe": "negatif",
        "source": "paris_opendata",
        "api_dataset_id": "zones-touristiques-internationales",
        # "api_max_records": 100,
        "format_source": "api_opendata",
    },
    # ══════════════════════════════════════════════════════════════════════════
    # INDICATEUR 2 — TRANSPORTS
    # ══════════════════════════════════════════════════════════════════════════
    {
        "id": "voies",
        "label": "Comptages multimodaux des passage sur voies de vélo/trottinette/autobus",
        "indicateur": "transports",
        "signe": "positif",
        "source": "paris_opendata",
        "api_dataset_id": "comptage-multimodal-comptages",
        "api_max_records": 100_000,
        "api_order_by": "t desc",
        "export_format": "csv",
        "export_sep": ";",
        # "api_max_records": 2000,
        "format_source": "api_opendata",
    },
    {
        "id": "velib",
        "label": "Vélib — Stations et disponibilité",
        "indicateur": "transports",
        "signe": "positif",
        "source": "transport_gouv",
        "api_base_url": "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets",
        "api_dataset_id": "velib-disponibilite-en-temps-reel",
        # "api_max_records": 2000,
        "format_source": "api_opendata",
    },
    {
        "id": "gares",
        "label": "Gares de voyageurs — Île-de-France",
        "indicateur": "transports",
        "signe": "positif",
        "source": "transport_gouv",
        "api_base_url": "https://data.iledefrance-mobilites.fr/api/explore/v2.1/catalog/datasets",
        "api_dataset_id": "emplacement-des-gares-idf",
        # "api_max_records": 500,
        "format_source": "api_idf",
    },
    {
        "id": "bus",
        "label": "Arret de bus — Île-de-France",
        "indicateur": "transports",
        "signe": "positif",
        "source": "transport_gouv",
        "api_base_url": "https://data.iledefrance-mobilites.fr/api/explore/v2.1/catalog/datasets",
        "api_dataset_id": "arrets",
        # "api_max_records": 5000,
        "format_source": "api_idf",
    },
    # ══════════════════════════════════════════════════════════════════════════
    # INDICATEUR 3 — LOISIRS
    # ══════════════════════════════════════════════════════════════════════════
    {
        "id": "evenements_paris",
        "label": "Que faire à Paris — Événements",
        "indicateur": "loisirs",
        "signe": "positif",
        "source": "paris_opendata",
        "api_dataset_id": "que-faire-a-paris-",
        # "api_max_records": 3000,
        "format_source": "api_opendata",
    },
    {
        "id": "terrasses",
        "label": "Terrasses autorisées",
        "indicateur": "loisirs",
        "signe": "positif",
        "source": "paris_opendata",
        "api_dataset_id": "terrasses-autorisations",
        # "api_max_records": 2000,
        "format_source": "api_opendata",
    },
    {
        "id": "cinemas_idf",
        "label": "Salles de cinéma — Île-de-France",
        "indicateur": "loisirs",
        "signe": "positif",
        "source": "idf_opendata",
        "api_base_url": IDF_API,
        "api_dataset_id": "les_salles_de_cinemas_en_ile-de-france",
        # "api_max_records": 500,
        "format_source": "api_idf",
    },
    {
        "id": "musees_idf",
        "label": "Musées — Île-de-France",
        "indicateur": "loisirs",
        "signe": "positif",
        "source": "idf_opendata",
        "api_base_url": IDF_API,
        "api_dataset_id": "liste_des_musees_franciliens",
        # "api_max_records": 500,
        "format_source": "api_idf",
    },
    # ══════════════════════════════════════════════════════════════════════════
    # INDICATEUR 4 — SERVICES PUBLICS
    # ══════════════════════════════════════════════════════════════════════════
    {
        "id": "ecoles_elementaires",
        "label": "Écoles élémentaires — Paris",
        "indicateur": "services_publics",
        "signe": "positif",
        "source": "paris_opendata",
        "api_dataset_id": "etablissements-scolaires-ecoles-elementaires",
        # "api_max_records": 500,
        "format_source": "api_opendata",
    },
    {
        "id": "maternelles_secteurs",
        "label": "Secteurs scolaires — Maternelles Paris",
        "indicateur": "services_publics",
        "signe": "positif",
        "source": "paris_opendata",
        "api_dataset_id": "secteurs-scolaires-maternelles",
        # "api_max_records": 500,
        "format_source": "api_opendata",
    },
    {
        "id": "colleges_secteurs",
        "label": "Secteurs scolaires — Collèges Paris",
        "indicateur": "services_publics",
        "signe": "positif",
        "source": "paris_opendata",
        "api_dataset_id": "secteurs-scolaires-colleges",
        # "api_max_records": 500,
        "format_source": "api_opendata",
    },
    {
        "id": "bibliotheques",
        "label": "Bibliothèques — Postes publics Paris",
        "indicateur": "services_publics",
        "signe": "positif",
        "source": "paris_opendata",
        "api_dataset_id": "postes-publics-des-bibliotheques",
        # "api_max_records": 100,
        "format_source": "api_opendata",
    },
    {
        "id": "enseignement_superieur",
        "label": "Établissements d'enseignement supérieur — IDF",
        "indicateur": "services_publics",
        "signe": "positif",
        "source": "idf_opendata",
        "api_base_url": IDF_API,
        "api_dataset_id": "principaux-etablissements-denseignement-superieur",
        # "api_max_records": 300,
        "format_source": "api_idf",
    },
    {
        "id": "bureaux_poste",
        "label": "Bureaux de poste — Île-de-France",
        "indicateur": "services_publics",
        "signe": "positif",
        "source": "idf_opendata",
        "api_base_url": IDF_API,
        "api_dataset_id": "les_bureaux_de_poste_et_agences_postales_en_idf",
        # "api_max_records": 500,
        "format_source": "api_idf",
    },
    # ══════════════════════════════════════════════════════════════════════════
    # IMMOBILIER (requis par le sujet)
    # ══════════════════════════════════════════════════════════════════════════
    {
        "id": "revenus_medians",
        "label": "Revenus médians par arrondissement — INSEE Filosofi 2021",
        "indicateur": "immobilier",
        "signe": "positif",
        "source": "insee_datagouv",
        "format_source": "filosofi",
    },
    {
        "id": "logements_sociaux",
        "label": "Logements sociaux financés — Paris",
        "indicateur": "immobilier",
        "signe": "positif",
        "source": "paris_opendata",
        "api_dataset_id": "logements-sociaux-finances-a-paris",
        # "api_max_records": 5000,
        "format_source": "api_opendata",
    },
    {
        "id": "dvf_prix_m2",
        "label": "Prix immobilier médian — DVF+ Etalab (Paris par année)",
        "indicateur": "immobilier",
        "signe": "positif",
        "source": "etalab",
        "format_source": "api_dvf",
        # L'API DVF+ Etalab est appelée via fetch_dvf_etalab()
    },
]

# ─── Loaders ──────────────────────────────────────────────────────────────────

def load_local(dataset: dict) -> pd.DataFrame:
    """Lit un fichier local depuis datasrc/ (Parquet / CSV / JSON)."""
    path = DATASRC / dataset["local_file"]
    ext = path.suffix.lower()
    log.info(f"  Lecture locale [{dataset['format_source']}] : {path.name}")
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


def _resolve_max_records(dataset: dict, total: int | None = None) -> int | None:
    """
    Résout la limite de récupération :
    - `api_max_records` absent / None / vide => pas de limite
    - entier positif => limite explicite
    """
    raw_limit = dataset.get("api_max_records")
    if raw_limit in (None, "", False):
        return total
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return total
    return limit if limit > 0 else total


def _should_use_export(dataset: dict) -> bool:
    """Utilise l'endpoint exports/json si on veut tout récupérer ou plus de 10k lignes."""
    fetch_mode = str(dataset.get("api_fetch_mode", "")).lower()
    if fetch_mode == "export":
        return True
    if fetch_mode == "records":
        return False
    raw_limit = dataset.get("api_max_records")
    if raw_limit in (None, "", False):
        return True
    try:
        return int(raw_limit) > 10_000
    except (TypeError, ValueError):
        return True


def _fetch_api_export(dataset: dict, default_base_url: str, label: str) -> pd.DataFrame:
    """
    Récupère tout un dataset OpenDataSoft via l'endpoint exports/{format}.
    Ce mode contourne la limite `offset + limit <= 10000`.
    """
    base_url = dataset.get("api_base_url", default_base_url)
    dataset_id = dataset["api_dataset_id"]
    export_format = str(dataset.get("export_format", "json")).lower()
    if export_format not in {"json", "csv"}:
        raise ValueError(f"Format d'export non supporte pour {dataset_id}: {export_format}")
    export_url = f"{base_url}/{dataset_id}/exports/{export_format}"
    requested_limit = _resolve_max_records(dataset)
    request_params = {}
    if requested_limit is not None:
        request_params["limit"] = requested_limit
    if dataset.get("api_where"):
        request_params["where"] = dataset["api_where"]
    if dataset.get("api_order_by"):
        request_params["order_by"] = dataset["api_order_by"]

    scope = f"limit={requested_limit}" if requested_limit is not None else "full dataset"
    if dataset.get("api_order_by"):
        scope = f"{scope}, order_by={dataset['api_order_by']}"
    if dataset.get("api_where"):
        scope = f"{scope}, where={dataset['api_where']}"
    log.info(f"  Fetch {label} [{dataset_id}] via export {export_format.upper()} ({scope})")
    with requests.get(export_url, params=request_params, stream=True, timeout=(30, 300)) as response:
        response.raise_for_status()
        content_length = response.headers.get("content-length")
        total_bytes = int(content_length) if content_length and content_length.isdigit() else None
        download_progress = tqdm(
            total=total_bytes,
            desc=f"Export {dataset_id}",
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            leave=False,
        )
        with SpooledTemporaryFile(max_size=EXPORT_SPOOL_MAX_SIZE) as spool:
            for chunk in response.iter_content(chunk_size=EXPORT_CHUNK_SIZE):
                if not chunk:
                    continue
                spool.write(chunk)
                download_progress.update(len(chunk))
            download_progress.close()
            spool.seek(0)
            if export_format == "json":
                payload = json.load(spool)
                if not isinstance(payload, list):
                    raise ValueError(f"Format export inattendu pour {dataset_id}: liste JSON attendue")
                if requested_limit is not None:
                    payload = payload[:requested_limit]
                log.info("    Normalisation JSON export en DataFrame...")
                df = pd.json_normalize(payload)
            else:
                sample = spool.readline().decode("utf-8-sig", errors="replace")
                spool.seek(0)
                sep = dataset.get("export_sep") or (";" if sample.count(";") > sample.count(",") else ",")
                df = pd.read_csv(spool, sep=sep, low_memory=False, encoding_errors="replace")
                if requested_limit is not None:
                    df = df.head(requested_limit)

    log.info(f"    → {len(df)} lignes récupérées via export")
    return df


def fetch_api(dataset: dict) -> pd.DataFrame:
    """Fetche depuis l'API Paris OpenDataSoft avec pagination (format api_opendata)."""
    if _should_use_export(dataset):
        return _fetch_api_export(dataset, PARIS_API, "API OpenDataSoft")

    base_url = dataset.get("api_base_url", PARIS_API)
    dataset_id = dataset["api_dataset_id"]
    max_records = dataset.get("api_max_records")
    records, offset, total = [], 0, None
    progress = None

    log.info(f"  Fetch API OpenDataSoft [{dataset_id}] (max {'ALL' if max_records in (None, '', False) else max_records})")
    try:
        while True:
            r = requests.get(
                f"{base_url}/{dataset_id}/records",
                params={"limit": PAGE_SIZE, "offset": offset},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            if total is None:
                total = data.get("total_count", 0)
                resolved_limit = _resolve_max_records(dataset, total)
                progress = tqdm(
                    total=resolved_limit or total or 0,
                    desc=f"API {dataset_id}",
                    unit="rows",
                    leave=False,
                )
            batch = data.get("results", [])
            if not batch:
                break
            records.extend(batch)
            offset += len(batch)
            if progress is not None:
                remaining = max((progress.total or 0) - progress.n, 0)
                progress.update(min(len(batch), remaining))
            resolved_limit = _resolve_max_records(dataset, total)
            if resolved_limit is not None and offset >= resolved_limit:
                break
    finally:
        if progress is not None:
            progress.close()

    df = pd.json_normalize(records)
    log.info(f"    → {len(df)} / {total} lignes récupérées")
    return df


def fetch_api_generic(dataset: dict) -> pd.DataFrame:
    """
    Fetche depuis une API OpenDataSoft tierce (IDF, transport) avec pagination.
    Même structure que fetch_api mais base_url variable (format api_idf).
    """
    if _should_use_export(dataset):
        return _fetch_api_export(dataset, IDF_API, "API IDF/Transport")

    base_url = dataset.get("api_base_url", IDF_API)
    dataset_id = dataset["api_dataset_id"]
    max_records = dataset.get("api_max_records")
    records, offset, total = [], 0, None
    progress = None

    log.info(f"  Fetch API IDF/Transport [{dataset_id}] (max {'ALL' if max_records in (None, '', False) else max_records})")
    try:
        while True:
            try:
                r = requests.get(
                    f"{base_url}/{dataset_id}/records",
                    params={"limit": PAGE_SIZE, "offset": offset},
                    timeout=30,
                )
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                log.warning(f"    Erreur API {dataset_id} : {e}")
                break

            if total is None:
                total = data.get("total_count", 0)
                resolved_limit = _resolve_max_records(dataset, total)
                progress = tqdm(
                    total=resolved_limit or total or 0,
                    desc=f"API {dataset_id}",
                    unit="rows",
                    leave=False,
                )
            batch = data.get("results", [])
            if not batch:
                break
            records.extend(batch)
            offset += len(batch)
            if progress is not None:
                remaining = max((progress.total or 0) - progress.n, 0)
                progress.update(min(len(batch), remaining))
            resolved_limit = _resolve_max_records(dataset, total)
            if resolved_limit is not None and offset >= resolved_limit:
                break
    finally:
        if progress is not None:
            progress.close()

    if not records:
        log.warning(f"    Aucune donnée récupérée pour {dataset_id}")
        return pd.DataFrame()

    df = pd.json_normalize(records)
    log.info(f"    → {len(df)} / {total} lignes récupérées")
    return df


def fetch_filosofi() -> pd.DataFrame:
    """
    Télécharge et filtre le CSV Filosofi 2021 (42 MB) depuis data.gouv.fr.
    Ne conserve que les lignes ARM (arrondissements municipaux) de Paris
    et la mesure MED_SL (revenu médian annuel par UC en EUR).
    Source : INSEE Filosofi 2021, dataset 69dfa98bda55a687d20bae47
    """
    log.info("  Fetch INSEE Filosofi 2021 — revenus médians Paris (GEO_OBJECT=ARM, MED_SL)")
    chunks = []
    try:
        for chunk in pd.read_csv(
            FILOSOFI_URL,
            sep=";",
            chunksize=10_000,
            dtype=str,
            low_memory=False,
        ):
            filtered = chunk[
                (chunk["GEO_OBJECT"] == "ARM")
                & (chunk["GEO"].isin(_PARIS_ARM_CODES))
                & (chunk["FILOSOFI_MEASURE"] == "MED_SL")
            ]
            if not filtered.empty:
                chunks.append(filtered)
    except Exception as exc:
        log.warning(f"  Erreur Filosofi : {exc}")
        return pd.DataFrame()
    if not chunks:
        log.warning("  Filosofi : aucune ligne Paris trouvée")
        return pd.DataFrame()
    df = pd.concat(chunks, ignore_index=True)
    log.info(f"    → {len(df)} lignes revenus médians Paris arrondissements")
    return df


def fetch_dvf_etalab() -> pd.DataFrame:
    """
    Récupère l'historique DVF géolocalisé de Paris (département 75) depuis
    les fichiers CSV annuels officiels de data.gouv.fr.

    Cette source contient les mutations avec coordonnées WGS84 et permet ensuite
    un calcul réel du prix médian au m² par arrondissement et quartier administratif.
    """
    index_url = "https://files.data.gouv.fr/geo-dvf/latest/csv/"
    year_urls = []

    try:
        response = requests.get(index_url, timeout=30)
        response.raise_for_status()
        year_urls = [
            (int(year), f"{index_url}{year}/departements/75.csv.gz")
            for year in re.findall(r'/geo-dvf/latest/csv/(\d{4})/', response.text)
        ]
        year_urls = sorted({item for item in year_urls})
    except Exception as exc:
        log.warning(f"    DVF index indisponible, fallback sur 2021-2025 : {exc}")
        year_urls = [
            (year, f"{index_url}{year}/departements/75.csv.gz")
            for year in range(2021, date.today().year)
        ]

    frames = []
    log.info("  Fetch DVF géolocalisé historique — Paris (département 75)")
    year_progress = tqdm(year_urls, desc="DVF historique", unit="annee", leave=False)
    for year, url in year_progress:
        year_progress.set_postfix_str(str(year))
        try:
            df_year = pd.read_csv(url, compression="gzip", low_memory=False)
            if df_year.empty:
                continue
            df_year["source_year"] = year
            frames.append(df_year)
            log.info(f"    ✓ {year} : {len(df_year)} lignes")
        except Exception as exc:
            log.warning(f"    DVF erreur pour {year} : {exc}")
    year_progress.close()

    if not frames:
        log.warning("    DVF : aucune donnée récupérée")
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    log.info(f"    → {len(df)} lignes DVF récupérées")
    return df


# ─── Writers ──────────────────────────────────────────────────────────────────

def add_meta_columns(df: pd.DataFrame, dataset: dict, ingestion_date: str) -> pd.DataFrame:
    """Ajoute les colonnes de traçabilité bronze."""
    df = df.copy()
    df["_ingested_at"] = ingestion_date
    df["_dataset_id"] = dataset["id"]
    df["_indicateur"] = dataset.get("indicateur", "")
    df["_signe"] = dataset["signe"]
    df["_source"] = dataset["source"]
    df["_format_source"] = dataset.get("format_source", "")
    return df


def _sanitize_for_export(df: pd.DataFrame) -> pd.DataFrame:
    """
    Prépare le DataFrame pour la sérialisation :
    - Colonnes bytes → hex string (WKB géo préservé pour Silver)
    - Colonnes avec listes/dicts → JSON string
    - Colonnes object → string pour éviter les erreurs PyArrow sur types mixtes
    """
    df = df.copy()
    for col in df.columns:
        sample = df[col].dropna()
        if sample.empty:
            continue
        first = sample.iloc[0]
        if isinstance(first, bytes):
            df[col] = df[col].apply(lambda v: v.hex() if isinstance(v, bytes) else v)
        elif isinstance(first, (list, dict)):
            df[col] = df[col].apply(lambda v: json.dumps(v, ensure_ascii=False) if v is not None else None)
        elif str(df[col].dtype) == "object":
            df[col] = df[col].apply(lambda v: None if pd.isna(v) else str(v))
    return df


def _key_prefix(ingestion_date: str, dataset: dict) -> str:
    return f"ingestion_date={ingestion_date}/{dataset['indicateur']}/{dataset['id']}"


def save_bronze_minio(df: pd.DataFrame, dataset: dict, ingestion_date: str):
    """Écrit raw.parquet (+ raw.json pour petits datasets) dans MinIO."""
    df = _sanitize_for_export(df)
    prefix = _key_prefix(ingestion_date, dataset)

    buf = BytesIO()
    df.to_parquet(buf, index=False)
    buf.seek(0)
    s3.put_object(Bucket=BUCKET, Key=f"{prefix}/raw.parquet", Body=buf.getvalue())
    log.info(f"    ✓ MinIO → {prefix}/raw.parquet ({len(df)} lignes)")

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
        "indicateur": dataset.get("indicateur", ""),
        "signe": dataset["signe"],
        "source": dataset["source"],
        "format_source": dataset.get("format_source", ""),
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

    # Bucket silver (créé ici pour éviter une dépendance sur silver_transformer)
    if "silver" not in existing:
        s3.create_bucket(Bucket="silver")
        log.info("  ✓ Bucket 'silver' créé")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run():
    ingestion_date = str(date.today())
    log.info(f"{'='*60}")
    log.info(f"Bronze Feeder — {ingestion_date}")
    log.info(f"{'='*60}")

    init_minio()

    results = []

    dataset_progress = tqdm(DATASETS, desc="Bronze datasets", unit="dataset")
    for idx, ds in enumerate(dataset_progress, start=1):
        dataset_progress.set_description_str(f"Bronze {idx}/{len(DATASETS)}")
        dataset_progress.set_postfix_str(ds["id"])
        log.info(f"\n[{ds['indicateur'].upper()}][{ds['signe'].upper()}] {ds['label']}")

        try:
            if ds.get("local_file"):
                df = load_local(ds)
            elif ds.get("format_source") == "filosofi":
                df = fetch_filosofi()
            elif ds.get("format_source") == "api_dvf":
                df = fetch_dvf_etalab()
            elif ds.get("format_source") == "api_idf":
                df = fetch_api_generic(ds)
            else:
                df = fetch_api(ds)

            if df.empty:
                log.warning(f"  DataFrame vide, dataset ignoré.")
                results.append({"id": ds["id"], "indicateur": ds.get("indicateur"), "rows": 0, "status": "VIDE"})
                continue

            df = add_meta_columns(df, ds, ingestion_date)
            save_bronze_minio(df, ds, ingestion_date)
            write_meta_minio(df, ds, ingestion_date)

            results.append({
                "id": ds["id"],
                "indicateur": ds.get("indicateur"),
                "signe": ds["signe"],
                "rows": len(df),
                "status": "OK",
                "minio_prefix": _key_prefix(ingestion_date, ds),
            })

        except Exception as e:
            log.error(f"  ERREUR : {e}")
            results.append({"id": ds["id"], "indicateur": ds.get("indicateur"), "rows": 0, "status": f"ERREUR: {e}"})
    dataset_progress.close()

    # ── Rapport final ──────────────────────────────────────────────────────
    log.info(f"\n{'='*60}")
    log.info("RAPPORT D'INGESTION BRONZE")
    log.info(f"{'='*60}")
    ok = [r for r in results if r["status"] == "OK"]
    ko = [r for r in results if r["status"] not in ("OK", "VIDE")]
    vide = [r for r in results if r["status"] == "VIDE"]
    log.info(f"  ✅ Succès  : {len(ok)}/{len(results)}")
    for r in ok:
        log.info(f"    [{r.get('indicateur','?'):20}] {r['id']:45} {r['rows']:>8} lignes")
    if vide:
        log.info(f"  ⚠️  Vides   : {len(vide)}")
        for r in vide:
            log.info(f"    {r['id']}")
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
