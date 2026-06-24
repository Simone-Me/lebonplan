"""Connexion MongoDB pour la couche API (lecture Silver collections)."""

import os
from pymongo import MongoClient

_client: MongoClient | None = None


def get_mongo_db():
    global _client
    if _client is None:
        uri = os.getenv("MONGO_URI", "mongodb://admin:password123@localhost:27017")
        _client = MongoClient(uri)
    db_name = os.getenv("MONGO_DB", "gold")
    return _client[db_name]
