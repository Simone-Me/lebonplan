from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import text

from api.database import get_db
from api.models import KPIs

router = APIRouter()

# Sections : (clé_annee, sentinel_field, liste_champs)
# Si sentinel est NULL pour l'année demandée → fallback sur l'année la plus récente avec des données.
_SECTIONS = [
    ("annee_immo", "prix_m2_median", [
        "prix_m2_median", "surface_mediane", "nb_logements_sociaux", "pct_logements_sociaux",
        "revenu_median_uc", "taux_effort_achat", "nb_t1", "nb_t2", "nb_t3", "nb_t4plus",
        "nb_appartements", "nb_maisons", "pct_appartements",
    ]),
    ("annee_qv", "nb_arbres", [
        "nb_espaces_verts", "score_fraicheur_espaces_verts", "nb_arbres", "pct_fibre", "nb_sanisettes",
        "nb_chantiers_actifs", "nb_anomalies", "score_qualite_vie", "score_air_no2", "score_air_pm25",
    ]),
    ("annee_transport", "nb_gares", [
        "nb_gares", "nb_stations_velib", "capacite_velib_totale", "nb_lignes_transport",
        "lignes_par_gare_moyen", "nb_modes_lourds", "nb_arrets_bus", "pct_arrets_accessibles",
        "flux_multimodal", "flux_velo_trott", "flux_bus", "flux_motorise",
        "pct_flux_velo_trott", "pct_flux_motorise", "pct_flux_voie_cyclable",
        "score_transports", "score_transport_offre", "score_transport_intensite",
    ]),
    ("annee_loisirs", "nb_cinemas", [
        "nb_evenements", "nb_cinemas", "nb_terrasses", "nb_musees", "score_loisirs",
    ]),
    ("annee_services", "nb_ecoles", [
        "nb_ecoles", "nb_maternelles", "nb_colleges", "nb_bibliotheques",
        "nb_bureaux_poste", "nb_ensup", "score_services",
    ]),
]


def _apply_section_fallbacks(db: Session, table: str, id_col: str, id_val, row: dict, annee: int) -> dict:
    """Pour chaque section dont le sentinel est NULL, remplace par l'année la plus récente avec données."""
    for annee_key, sentinel, fields in _SECTIONS:
        if row.get(sentinel) is not None:
            row[annee_key] = annee
            continue
        fb = db.execute(
            text(f"SELECT annee FROM gold.{table} WHERE {id_col} = :id AND {sentinel} IS NOT NULL ORDER BY annee DESC LIMIT 1"),
            {"id": id_val},
        ).fetchone()
        if not fb:
            row[annee_key] = annee
            continue
        fb_annee = fb[0]
        row[annee_key] = fb_annee
        fb_row = db.execute(
            text(f"SELECT * FROM gold.{table} WHERE {id_col} = :id AND annee = :a"),
            {"id": id_val, "a": fb_annee},
        ).fetchone()
        if fb_row:
            fb_dict = dict(fb_row._mapping)
            for field in fields:
                if row.get(field) is None and fb_dict.get(field) is not None:
                    row[field] = fb_dict[field]
    return row


def _fetch_kpis(db: Session, arrondissement: int, annee: int) -> dict | None:
    row = db.execute(
        text("SELECT * FROM gold.arrondissement_kpis WHERE arrondissement = :arr AND annee = :annee"),
        {"arr": arrondissement, "annee": annee},
    ).fetchone()
    return dict(row._mapping) if row else None


def _fetch_quartier_kpis(db: Session, quartier_id: str, annee: int) -> dict | None:
    row = db.execute(
        text("SELECT * FROM gold.quartier_kpis WHERE quartier_id = :quartier_id AND annee = :annee"),
        {"quartier_id": quartier_id, "annee": annee},
    ).fetchone()
    return dict(row._mapping) if row else None


def _fetch_iris_kpis(db: Session, iris_id: str, annee: int) -> dict | None:
    row = db.execute(
        text("SELECT * FROM gold.iris_kpis WHERE iris_id = :iris_id AND annee = :annee"),
        {"iris_id": iris_id, "annee": annee},
    ).fetchone()
    return dict(row._mapping) if row else None


@router.get("/kpis/{arrondissement}", response_model=KPIs)
def get_kpis(
    arrondissement: int,
    annee: int = Query(default=None),
    db: Session = Depends(get_db),
):
    if not (1 <= arrondissement <= 20):
        raise HTTPException(status_code=400, detail="Arrondissement doit être entre 1 et 20")
    if annee is None:
        row = db.execute(text("SELECT MAX(annee) FROM gold.arrondissement_kpis")).fetchone()
        annee = row[0] if row and row[0] else 2024

    data = _fetch_kpis(db, arrondissement, annee)
    if not data:
        raise HTTPException(status_code=404, detail=f"Aucune donnée pour arrondissement={arrondissement} annee={annee}")

    data = _apply_section_fallbacks(db, "arrondissement_kpis", "arrondissement", arrondissement, data, annee)
    return KPIs(**data)


@router.get("/kpis/quartier/{quartier_id}", response_model=KPIs)
def get_quartier_kpis(
    quartier_id: str,
    annee: int = Query(default=None),
    db: Session = Depends(get_db),
):
    if annee is None:
        row = db.execute(text("SELECT MAX(annee) FROM gold.quartier_kpis")).fetchone()
        annee = row[0] if row and row[0] else 2024

    data = _fetch_quartier_kpis(db, quartier_id, annee)
    if not data:
        raise HTTPException(status_code=404, detail=f"Aucune donnée pour quartier_id={quartier_id} annee={annee}")

    data = _apply_section_fallbacks(db, "quartier_kpis", "quartier_id", quartier_id, data, annee)
    return KPIs(**data)


@router.get("/kpis/iris/{iris_id}", response_model=KPIs)
def get_iris_kpis(
    iris_id: str,
    annee: int = Query(default=None),
    db: Session = Depends(get_db),
):
    if annee is None:
        row = db.execute(text("SELECT MAX(annee) FROM gold.iris_kpis")).fetchone()
        annee = row[0] if row and row[0] else 2024

    data = _fetch_iris_kpis(db, iris_id, annee)
    if not data:
        raise HTTPException(status_code=404, detail=f"Aucune donnée pour iris_id={iris_id} annee={annee}")

    data = _apply_section_fallbacks(db, "iris_kpis", "iris_id", iris_id, data, annee)
    return KPIs(**data)
