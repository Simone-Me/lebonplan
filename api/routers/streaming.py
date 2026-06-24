from fastapi import APIRouter, Depends
from api.mongo import get_mongo_db

router = APIRouter()

_STREAMING_COLLECTIONS = ["velib", "sanisettes", "chantiers", "anomalies", "voies"]
_REFRESH_INTERVAL_SECONDS = 300  # 5 minutes


@router.get("/streaming/status", tags=["Streaming"])
def streaming_status(db=Depends(get_mongo_db)):
    """Retourne l'horodatage du dernier batch Kafka et l'intervalle de rafraîchissement."""
    last_update = None
    per_collection = {}

    for col in _STREAMING_COLLECTIONS:
        doc = db[col].find_one(
            {"_fetched_at": {"$exists": True}},
            sort=[("_fetched_at", -1)],
            projection={"_fetched_at": 1, "_id": 0},
        )
        ts = doc.get("_fetched_at") if doc else None
        per_collection[col] = ts
        if ts and (last_update is None or ts > last_update):
            last_update = ts

    return {
        "last_update": last_update,
        "refresh_interval_seconds": _REFRESH_INTERVAL_SECONDS,
        "collections": per_collection,
    }
