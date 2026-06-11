"""
Configuration partagée pour tous les scripts pipeline.
Les valeurs sont lues depuis les variables d'environnement (fichier .env à la racine).
"""

import os
from pathlib import Path

# Charge .env si python-dotenv est installé (optionnel)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

# ─── MinIO ────────────────────────────────────────────────────────────────────

MINIO_ENDPOINT   = os.environ.get("MINIO_ENDPOINT",   "http://localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "admin")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "password123")

BUCKET_BRONZE = "bronze"
BUCKET_SILVER = "silver"

# ─── MongoDB ──────────────────────────────────────────────────────────────────

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = "silver"

# ─── PostgreSQL ───────────────────────────────────────────────────────────────

POSTGRES_HOST     = os.environ.get("POSTGRES_HOST",     "127.0.0.1")
POSTGRES_PORT     = os.environ.get("POSTGRES_PORT",     "5433")
POSTGRES_DB       = os.environ.get("POSTGRES_DB",       "urban_data")
POSTGRES_USER     = os.environ.get("POSTGRES_USER",     "admin")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "password123")

POSTGRES_DSN = (
    f"postgresql+psycopg2://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
    f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
)

# ─── APIs externes ────────────────────────────────────────────────────────────

PARIS_API = "https://parisdata.opendatasoft.com/api/explore/v2.1/catalog/datasets"
IDF_API   = "https://data.iledefrance.fr/api/explore/v2.1/catalog/datasets"
BAN_API   = "https://api-adresse.data.gouv.fr"  # Géocodage Base Adresse Nationale
