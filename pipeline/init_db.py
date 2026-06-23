"""
Initialisation de la couche Gold — PostgreSQL/PostGIS
Crée le schéma, les tables et les index nécessaires.
"""

import logging
from sqlalchemy import create_engine, text

from pipeline.config import POSTGRES_DSN

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("init_db")

DDL = """
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE SCHEMA IF NOT EXISTS gold;

-- Géométries des quartiers administratifs de Paris (80 quartiers)
CREATE TABLE IF NOT EXISTS gold.quartiers_geo (
    quartier_id     TEXT PRIMARY KEY,
    quartier_code   TEXT,
    arrondissement  INT,
    nom             TEXT,
    geom            GEOMETRY(MULTIPOLYGON, 4326)
);

-- Géométries des arrondissements (source : parisdata GeoJSON)
CREATE TABLE IF NOT EXISTS gold.arrondissements_geo (
    arrondissement  INT PRIMARY KEY,
    nom             TEXT,
    geom            GEOMETRY(MULTIPOLYGON, 4326)
);

-- KPIs agrégés par quartier administratif × année
CREATE TABLE IF NOT EXISTS gold.quartier_kpis (
    quartier_id             TEXT        NOT NULL,
    quartier_code           TEXT,
    arrondissement          INT,
    nom                     TEXT,
    annee                   INT         NOT NULL DEFAULT EXTRACT(YEAR FROM NOW()),

    -- Immobilier
    prix_m2_median          FLOAT,
    pct_logements_sociaux   FLOAT,
    nb_logements_sociaux    INT,
    -- Accessibilité revenus (INSEE Filosofi 2021, MED_SL EUR/an)
    revenu_median_uc        FLOAT,
    taux_effort_achat       FLOAT,
    -- Répartition parc immobilier (DVF)
    surface_mediane         FLOAT,
    nb_appartements         INT,
    nb_maisons              INT,
    pct_appartements        FLOAT,
    nb_t1                   INT,
    nb_t2                   INT,
    nb_t3                   INT,
    nb_t4plus               INT,

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
    score_transport_offre   FLOAT,
    score_transport_intensite FLOAT,
    nb_gares                INT,
    nb_stations_velib       INT,
    capacite_velib_totale   FLOAT,
    nb_lignes_transport     INT,
    lignes_par_gare_moyen   FLOAT,
    nb_modes_lourds         INT,
    nb_arrets_bus           INT,
    pct_arrets_accessibles  FLOAT,
    flux_multimodal         FLOAT,
    flux_velo_trott         FLOAT,
    flux_bus                FLOAT,
    flux_motorise           FLOAT,
    pct_flux_velo_trott     FLOAT,
    pct_flux_motorise       FLOAT,
    pct_flux_voie_cyclable  FLOAT,

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

    PRIMARY KEY (quartier_id, annee)
);

-- KPIs agrégés par arrondissement × année
CREATE TABLE IF NOT EXISTS gold.arrondissement_kpis (
    arrondissement          INT         NOT NULL,
    annee                   INT         NOT NULL DEFAULT EXTRACT(YEAR FROM NOW()),

    -- Immobilier
    prix_m2_median          FLOAT,
    pct_logements_sociaux   FLOAT,
    nb_logements_sociaux    INT,
    -- Accessibilité revenus (INSEE Filosofi 2021, MED_SL EUR/an)
    revenu_median_uc        FLOAT,
    taux_effort_achat       FLOAT,
    -- Répartition parc immobilier (DVF)
    surface_mediane         FLOAT,
    nb_appartements         INT,
    nb_maisons              INT,
    pct_appartements        FLOAT,
    nb_t1                   INT,
    nb_t2                   INT,
    nb_t3                   INT,
    nb_t4plus               INT,

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
    score_transport_offre   FLOAT,
    score_transport_intensite FLOAT,
    nb_gares                INT,
    nb_stations_velib       INT,
    capacite_velib_totale   FLOAT,
    nb_lignes_transport     INT,
    lignes_par_gare_moyen   FLOAT,
    nb_modes_lourds         INT,
    nb_arrets_bus           INT,
    pct_arrets_accessibles  FLOAT,
    flux_multimodal         FLOAT,
    flux_velo_trott         FLOAT,
    flux_bus                FLOAT,
    flux_motorise           FLOAT,
    pct_flux_velo_trott     FLOAT,
    pct_flux_motorise       FLOAT,
    pct_flux_voie_cyclable  FLOAT,

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
CREATE INDEX IF NOT EXISTS idx_quartier_kpis_id    ON gold.quartier_kpis (quartier_id);
CREATE INDEX IF NOT EXISTS idx_quartier_kpis_arr   ON gold.quartier_kpis (arrondissement);
CREATE INDEX IF NOT EXISTS idx_quartier_kpis_annee ON gold.quartier_kpis (annee);
CREATE INDEX IF NOT EXISTS idx_quartiers_arr       ON gold.quartiers_geo (arrondissement);
CREATE INDEX IF NOT EXISTS idx_quartiers_geom      ON gold.quartiers_geo USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_geo_geom            ON gold.arrondissements_geo USING GIST (geom);

-- Migrations : colonnes ajoutées après la création initiale
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS nb_maternelles INT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS nb_arrets_bus INT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS score_transport_offre FLOAT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS score_transport_intensite FLOAT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS capacite_velib_totale FLOAT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS nb_lignes_transport INT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS lignes_par_gare_moyen FLOAT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS nb_modes_lourds INT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS pct_arrets_accessibles FLOAT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS flux_velo_trott FLOAT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS flux_bus FLOAT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS flux_motorise FLOAT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS pct_flux_velo_trott FLOAT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS pct_flux_motorise FLOAT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS pct_flux_voie_cyclable FLOAT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS revenu_median_uc FLOAT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS taux_effort_achat FLOAT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS surface_mediane FLOAT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS nb_appartements INT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS nb_maisons INT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS pct_appartements FLOAT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS nb_t1 INT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS nb_t2 INT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS nb_t3 INT;
ALTER TABLE gold.arrondissement_kpis ADD COLUMN IF NOT EXISTS nb_t4plus INT;
ALTER TABLE gold.quartier_kpis ADD COLUMN IF NOT EXISTS revenu_median_uc FLOAT;
ALTER TABLE gold.quartier_kpis ADD COLUMN IF NOT EXISTS taux_effort_achat FLOAT;
ALTER TABLE gold.quartier_kpis ADD COLUMN IF NOT EXISTS surface_mediane FLOAT;
ALTER TABLE gold.quartier_kpis ADD COLUMN IF NOT EXISTS nb_appartements INT;
ALTER TABLE gold.quartier_kpis ADD COLUMN IF NOT EXISTS nb_maisons INT;
ALTER TABLE gold.quartier_kpis ADD COLUMN IF NOT EXISTS pct_appartements FLOAT;
ALTER TABLE gold.quartier_kpis ADD COLUMN IF NOT EXISTS nb_t1 INT;
ALTER TABLE gold.quartier_kpis ADD COLUMN IF NOT EXISTS nb_t2 INT;
ALTER TABLE gold.quartier_kpis ADD COLUMN IF NOT EXISTS nb_t3 INT;
ALTER TABLE gold.quartier_kpis ADD COLUMN IF NOT EXISTS nb_t4plus INT;
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
