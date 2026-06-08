"""
Initialisation de la couche Gold — PostgreSQL/PostGIS
Crée le schéma, les tables et les index nécessaires.
"""

import logging
from sqlalchemy import create_engine, text

from config import POSTGRES_DSN

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("init_db")

DDL = """
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS gold;

-- Géométries des arrondissements (source : parisdata GeoJSON)
CREATE TABLE IF NOT EXISTS gold.arrondissements_geo (
    arrondissement  INT PRIMARY KEY,
    nom             TEXT,
    geom            GEOMETRY(MULTIPOLYGON, 4326)
);

-- KPIs agrégés par arrondissement × année
CREATE TABLE IF NOT EXISTS gold.arrondissement_kpis (
    arrondissement          INT         NOT NULL,
    annee                   INT         NOT NULL DEFAULT EXTRACT(YEAR FROM NOW()),

    -- Immobilier
    prix_m2_median          FLOAT,
    pct_logements_sociaux   FLOAT,
    nb_logements_sociaux    INT,

    -- Indicateur 1 : Qualité de vie
    score_qualite_vie       FLOAT,
    nb_espaces_verts        INT,
    nb_arbres               INT,
    score_air_no2           FLOAT,
    score_air_pm25          FLOAT,
    pct_fibre               FLOAT,
    nb_sanisettes           INT,
    nb_chantiers_actifs     INT,
    nb_anomalies            INT,

    -- Indicateur 2 : Transports
    score_transports        FLOAT,
    nb_gares                INT,
    nb_stations_velib       INT,
    flux_multimodal         BIGINT,

    -- Indicateur 3 : Loisirs
    score_loisirs           FLOAT,
    nb_evenements           INT,
    nb_cinemas              INT,
    nb_terrasses            INT,
    nb_musees               INT,

    -- Indicateur 4 : Services publics
    score_services          FLOAT,
    nb_ecoles               INT,
    nb_maternelles          INT,
    nb_colleges             INT,
    nb_bibliotheques        INT,
    nb_bureaux_poste        INT,
    nb_ensup                INT,

    -- Score composite global (0-100)
    score_global            FLOAT,

    PRIMARY KEY (arrondissement, annee)
);

CREATE INDEX IF NOT EXISTS idx_kpis_arrondissement ON gold.arrondissement_kpis (arrondissement);
CREATE INDEX IF NOT EXISTS idx_kpis_annee          ON gold.arrondissement_kpis (annee);
CREATE INDEX IF NOT EXISTS idx_geo_geom            ON gold.arrondissements_geo USING GIST (geom);

-- Migrations : colonnes ajoutées après la création initiale
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS nb_maternelles INT;
"""


def run():
    log.info("Connexion PostgreSQL...")
    engine = create_engine(POSTGRES_DSN)
    try:
        with engine.connect() as conn:
            conn.execute(text(DDL))
            conn.commit()
        log.info("  ✓ Schéma gold initialisé (tables + index)")
    except Exception as e:
        log.error(f"  Erreur DDL : {e}")
        raise
    finally:
        engine.dispose()


if __name__ == "__main__":
    run()
