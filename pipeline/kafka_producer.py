"""
Kafka Producer — Urban Data Explorer
Rafraîchit en quasi-temps-réel les datasets volatils toutes les STREAMING_REFRESH_INTERVAL secondes
(défaut : 300s = 5 minutes) et publie chaque enregistrement dans le topic Kafka correspondant.

Datasets streamés (direct API → Kafka, sans passer par le Bronze) :
  urban.velib        ← disponibilité Vélib en temps réel (~1 500 stations)
  urban.sanisettes   ← état des sanisettes publiques (~700 entrées)
  urban.chantiers    ← chantiers actifs à Paris (API peut être indisponible)
  urban.anomalies    ← 100 000 dernières anomalies Dans ma rue
  urban.voies        ← 100 000 derniers comptages multimodaux vélo/bus/trottinette (order by t desc)

Contraste avec le pipeline batch (Bronze→Silver→Gold) :
  - Les datasets historiques (DVF, Filosofi, espaces verts…) restent en batch nocturne.
  - Ces datasets temps-réel ne passent pas par MinIO : Producer → Kafka → Consumer → MongoDB directement.
"""

import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from kafka import KafkaProducer
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
REFRESH_INTERVAL = int(os.getenv("STREAMING_REFRESH_INTERVAL", "300"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kafka_producer")

# Datasets à streamer — chacun a un topic dédié et une clé unique pour la déduplication MongoDB
STREAMING_DATASETS = [
    {
        "id": "velib",
        "topic": "urban.velib",
        "api_base_url": "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets",
        "api_dataset_id": "velib-disponibilite-en-temps-reel",
        "max_records": 100_000,
        "unique_key": "stationcode",
    },
    {
        "id": "sanisettes",
        "topic": "urban.sanisettes",
        "api_base_url": "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets",
        "api_dataset_id": "sanisettesparis",
        "max_records": 100_000,
        "unique_key": "geo_point_2d",
    },
    {
        "id": "chantiers",
        "topic": "urban.chantiers",
        "api_base_url": "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets",
        "api_dataset_id": "chantiers-a-paris",
        "max_records": 100_000,
        "unique_key": "identifiant",
    },
    {
        "id": "anomalies",
        "topic": "urban.anomalies",
        "api_base_url": "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets",
        "api_dataset_id": "dans-ma-rue",
        "max_records": 100_000,
        "unique_key": "object_id",
    },
    {
        "id": "voies",
        "topic": "urban.voies",
        "api_base_url": "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets",
        "api_dataset_id": "comptage-multimodal-comptages",
        "max_records": 100_000,
        "order_by": "t desc",          # 100k comptages les plus récents
        "unique_key": None,            # pas d'ID stable — fingerprint MD5 utilisé
    },
]


@retry(
    retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout, requests.HTTPError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
def _http_get(url: str, **kwargs) -> requests.Response:
    r = requests.get(url, timeout=(10, 60), **kwargs)
    r.raise_for_status()
    return r


def fetch_records(dataset: dict) -> list[dict]:
    """Récupère les N derniers enregistrements via l'endpoint exports/json OpenDataSoft."""
    url = f"{dataset['api_base_url']}/{dataset['api_dataset_id']}/exports/json"
    params = {"limit": dataset["max_records"]}
    if dataset.get("order_by"):
        params["order_by"] = dataset["order_by"]
    log.info(f"  Fetch {dataset['id']} (limit={dataset['max_records']})...")
    try:
        resp = _http_get(url, params=params)
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("results", data.get("records", []))
    except Exception as e:
        log.warning(f"  ⚠ {dataset['id']} indisponible ({type(e).__name__}: {e}) — ignoré ce cycle")
        return []


def _message_key(record: dict, unique_key: str | None) -> bytes:
    """
    Clé Kafka = identifiant unique du record.
    Utilisée par le consumer pour le filtre d'upsert MongoDB.
    Si pas de clé stable, fingerprint MD5 sur le contenu du record.
    """
    if unique_key:
        val = record.get(unique_key)
        if val is not None:
            return str(val).encode("utf-8")
    raw = json.dumps(record, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode()).hexdigest().encode("utf-8")


def produce_dataset(producer: KafkaProducer, dataset: dict) -> int:
    records = fetch_records(dataset)
    if not records:
        return 0

    fetched_at = datetime.now(timezone.utc).isoformat()
    for record in records:
        message = {
            "_dataset_id": dataset["id"],
            "_unique_key": dataset.get("unique_key"),
            "_fetched_at": fetched_at,
            "record": record,
        }
        key = _message_key(record, dataset.get("unique_key"))
        producer.send(dataset["topic"], key=key, value=message)

    producer.flush()
    log.info(f"  ✓ {dataset['id']} — {len(records)} messages → topic {dataset['topic']}")
    return len(records)


def main():
    log.info(f"Kafka Producer démarré (bootstrap={KAFKA_BOOTSTRAP}, refresh={REFRESH_INTERVAL}s)")
    log.info(f"Topics : {[d['topic'] for d in STREAMING_DATASETS]}")

    producer = KafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP,
        value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode("utf-8"),
        acks="all",
        retries=3,
        compression_type="gzip",
    )

    while True:
        cycle_start = time.monotonic()
        log.info("=== Cycle de rafraîchissement streaming ===")
        total = 0
        for dataset in STREAMING_DATASETS:
            try:
                total += produce_dataset(producer, dataset)
            except Exception as e:
                log.error(f"  ✗ {dataset['id']} échec définitif : {e}")

        elapsed = time.monotonic() - cycle_start
        log.info(
            f"=== {total} messages publiés en {elapsed:.1f}s — "
            f"prochain cycle dans {max(0, REFRESH_INTERVAL - elapsed):.0f}s ==="
        )
        time.sleep(max(0, REFRESH_INTERVAL - elapsed))


if __name__ == "__main__":
    main()
