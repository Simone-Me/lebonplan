"""
Kafka Consumer — Urban Data Explorer
Lit en continu les topics de streaming et upsert les enregistrements dans MongoDB Silver.

Le consumer tourne en boucle infinie avec des poll de 10 secondes.
Chaque message est upsert dans la collection MongoDB correspondant au dataset_id.
La clé Kafka (unique_key du producer) est utilisée comme filtre d'upsert
pour garantir la déduplication : même record → mise à jour, nouveau record → insertion.

Collections MongoDB mises à jour en temps réel :
  silver.velib       ← état courant de chaque station Vélib (~1 500 docs)
  silver.sanisettes  ← état courant de chaque sanisette (~700 docs)
  silver.chantiers   ← chantiers actifs (mis à jour si l'API revient)
  silver.anomalies   ← 100 000 derniers signalements Dans ma rue
  silver.voies       ← 100 000 derniers comptages multimodaux vélo/bus/trottinette
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

from kafka import KafkaConsumer
from pymongo import MongoClient, UpdateOne
from pymongo.errors import BulkWriteError

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://mongodb:27017")
TOPICS = ["urban.velib", "urban.sanisettes", "urban.chantiers", "urban.anomalies", "urban.voies"]
POLL_TIMEOUT_MS = 10_000
MAX_POLL_RECORDS = 500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kafka_consumer")


def _build_filter(record: dict, unique_key: str | None, key_bytes: bytes | None) -> dict:
    """
    Construit le filtre MongoDB pour l'upsert.
    Priorité : unique_key présent dans le record → fingerprint MD5 (clé Kafka).
    """
    if unique_key and unique_key in record:
        return {unique_key: record[unique_key]}
    if key_bytes:
        return {"_kafka_key": key_bytes.decode("utf-8", errors="replace")}
    return {"_fetched_at": record.get("_fetched_at")}


def flush_batch(db, batch: dict[str, list[tuple]]) -> int:
    """Upsert un batch de messages par collection MongoDB. Retourne le total d'ops."""
    total = 0
    for collection_name, items in batch.items():
        if not items:
            continue
        ops = []
        for msg, key_bytes in items:
            record = msg["record"]
            record["_fetched_at"] = msg["_fetched_at"]
            record["_dataset_id"] = msg["_dataset_id"]
            if key_bytes:
                record["_kafka_key"] = key_bytes.decode("utf-8", errors="replace")

            unique_key = msg.get("_unique_key")
            filter_doc = _build_filter(record, unique_key, key_bytes)
            ops.append(UpdateOne(filter_doc, {"$set": record}, upsert=True))

        if ops:
            try:
                result = db[collection_name].bulk_write(ops, ordered=False)
                n = result.upserted_count + result.modified_count
                log.info(f"  ✓ {collection_name} — {n} upserts ({len(ops)} ops)")
                total += n
            except BulkWriteError as e:
                log.warning(f"  ⚠ {collection_name} bulk write partiel : {e.details.get('nInserted', 0)} insérés")
    return total


def main():
    log.info(f"Kafka Consumer démarré (bootstrap={KAFKA_BOOTSTRAP})")
    log.info(f"Topics : {TOPICS}")

    mongo = MongoClient(MONGO_URI)
    db = mongo["gold"]

    consumer = KafkaConsumer(
        *TOPICS,
        bootstrap_servers=KAFKA_BOOTSTRAP,
        group_id="urban-data-consumer-group",
        auto_offset_reset="latest",       # On part de maintenant — pas de replay historique
        enable_auto_commit=True,
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        max_poll_records=MAX_POLL_RECORDS,
    )

    log.info("Consumer prêt, en attente de messages...")
    batch: dict[str, list] = {t.split(".")[-1]: [] for t in TOPICS}

    while True:
        msg_pack = consumer.poll(timeout_ms=POLL_TIMEOUT_MS, max_records=MAX_POLL_RECORDS)

        for _tp, messages in msg_pack.items():
            for msg in messages:
                dataset_id = msg.value.get("_dataset_id", _tp.topic.split(".")[-1])
                batch.setdefault(dataset_id, []).append((msg.value, msg.key))

        has_messages = any(v for v in batch.values())
        if has_messages:
            total = flush_batch(db, batch)
            log.info(f"=== Batch traité — {total} documents mis à jour ===")
            batch = {t.split(".")[-1]: [] for t in TOPICS}
        else:
            log.debug(f"Pas de nouveaux messages (poll timeout {POLL_TIMEOUT_MS}ms)")


if __name__ == "__main__":
    main()
