"""
Tests unitaires Urban Data Explorer — pipeline Silver
Couvre : parse_arrondissement, transform_dvf (prix_m2), transform_revenus
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

# Permet d'importer pipeline/ sans lancer Docker
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ─── import isolé (sans side-effects réseau/DB) ──────────────────────────────

from pipeline.silver_transformer import parse_arrondissement, transform_dvf, transform_revenus


# ─── parse_arrondissement ─────────────────────────────────────────────────────

class TestParseArrondissement:
    def test_code_insee_format_75017(self):
        assert parse_arrondissement("75017") == 17

    def test_code_insee_format_75117(self):
        assert parse_arrondissement("75117") == 17

    def test_code_insee_format_75101(self):
        assert parse_arrondissement("75101") == 1

    def test_code_insee_format_75120(self):
        assert parse_arrondissement("75120") == 20

    def test_texte_paris_arrdt(self):
        assert parse_arrondissement("PARIS 17E ARRDT") == 17

    def test_texte_17e(self):
        assert parse_arrondissement("17e") == 17

    def test_entier_direct(self):
        assert parse_arrondissement("5") == 5

    def test_hors_plage_retourne_none(self):
        assert parse_arrondissement("75121") is None
        assert parse_arrondissement("21") is None
        assert parse_arrondissement("0") is None

    def test_none_retourne_none(self):
        assert parse_arrondissement(None) is None

    def test_nan_retourne_none(self):
        assert parse_arrondissement(float("nan")) is None


# ─── transform_dvf ────────────────────────────────────────────────────────────

def _dvf_row(**overrides):
    base = {
        "id_mutation": "M001",
        "date_mutation": "2023-06-15",
        "valeur_fonciere": 300_000.0,
        "surface_reelle_bati": 50.0,
        "type_local": "Appartement",
        "code_commune": "75017",
        "longitude": 2.32,
        "latitude": 48.88,
        "nature_mutation": "Vente",
        "code_postal": "75017",
        "nom_commune": "Paris",
        "nombre_pieces_principales": 3,
        "surface_terrain": 0.0,
        "nombre_lots": 1,
        "_ingested_at": "2026-01-01",
        "_dataset_id": "dvf",
        "_indicateur": "immobilier",
        "_signe": "dvf",
        "_source": "etalab",
        "source_year": 2023,
    }
    base.update(overrides)
    return base


class TestTransformDvf:
    def test_calcul_prix_m2_correct(self):
        df = pd.DataFrame([_dvf_row(valeur_fonciere=300_000, surface_reelle_bati=50)])
        result = transform_dvf(df)
        assert "prix_m2" in result.columns
        assert abs(result.iloc[0]["prix_m2"] - 6_000.0) < 0.01

    def test_prix_m2_surface_zero_est_nan(self):
        df = pd.DataFrame([_dvf_row(valeur_fonciere=200_000, surface_reelle_bati=0)])
        result = transform_dvf(df)
        assert pd.isna(result.iloc[0]["prix_m2"])

    def test_annee_extraite_depuis_date_mutation(self):
        df = pd.DataFrame([_dvf_row(date_mutation="2022-03-10")])
        result = transform_dvf(df)
        assert result.iloc[0]["annee"] == 2022

    def test_arrondissement_resolu(self):
        df = pd.DataFrame([_dvf_row(code_commune="75005")])
        result = transform_dvf(df)
        assert result.iloc[0]["arrondissement"] == 5

    def test_colonne_prix_m2_presente(self):
        df = pd.DataFrame([_dvf_row()])
        result = transform_dvf(df)
        assert "prix_m2" in result.columns

    def test_plusieurs_mutations(self):
        rows = [
            _dvf_row(valeur_fonciere=200_000, surface_reelle_bati=40),
            _dvf_row(valeur_fonciere=600_000, surface_reelle_bati=100),
        ]
        result = transform_dvf(pd.DataFrame(rows))
        assert len(result) == 2
        assert abs(result.iloc[0]["prix_m2"] - 5_000.0) < 0.01
        assert abs(result.iloc[1]["prix_m2"] - 6_000.0) < 0.01


# ─── transform_revenus ────────────────────────────────────────────────────────

class TestTransformRevenus:
    def _make_df(self, geo, obs_value, time_period="2021"):
        return pd.DataFrame([{
            "GEO": geo,
            "OBS_VALUE": obs_value,
            "TIME_PERIOD": time_period,
            "FILOSOFI_MEASURE": "MED_SL",
            "GEO_OBJECT": "ARM",
        }])

    def test_code_geo_75101_donne_arr_1(self):
        result = transform_revenus(self._make_df("75101", 25_000))
        assert result.iloc[0]["arrondissement"] == 1

    def test_code_geo_75120_donne_arr_20(self):
        result = transform_revenus(self._make_df("75120", 30_000))
        assert result.iloc[0]["arrondissement"] == 20

    def test_valeur_obs_conservee(self):
        result = transform_revenus(self._make_df("75108", 28_500))
        assert result.iloc[0]["revenu_median_uc"] == 28_500

    def test_annee_parsee(self):
        result = transform_revenus(self._make_df("75110", 22_000, time_period="2021"))
        assert result.iloc[0]["annee"] == 2021

    def test_code_hors_paris_exclu(self):
        result = transform_revenus(self._make_df("69001", 20_000))
        assert len(result) == 0

    def test_valeur_nan_exclue(self):
        result = transform_revenus(self._make_df("75103", float("nan")))
        assert len(result) == 0
