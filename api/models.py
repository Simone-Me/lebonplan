from pydantic import BaseModel
from typing import Optional, List, Any


class KPIs(BaseModel):
    arrondissement: Optional[int] = None
    quartier_id: Optional[str] = None
    quartier_code: Optional[str] = None
    nom: Optional[str] = None
    annee: int

    prix_m2_median: Optional[float] = None
    pct_logements_sociaux: Optional[float] = None
    nb_logements_sociaux: Optional[int] = None
    revenu_median_uc: Optional[float] = None
    taux_effort_achat: Optional[float] = None
    surface_mediane: Optional[float] = None
    nb_appartements: Optional[int] = None
    nb_maisons: Optional[int] = None
    pct_appartements: Optional[float] = None
    nb_t1: Optional[int] = None
    nb_t2: Optional[int] = None
    nb_t3: Optional[int] = None
    nb_t4plus: Optional[int] = None

    score_qualite_vie: Optional[float] = None
    nb_espaces_verts: Optional[int] = None
    nb_arbres: Optional[int] = None
    score_air_no2: Optional[float] = None
    score_air_pm25: Optional[float] = None
    pct_fibre: Optional[float] = None
    nb_sanisettes: Optional[int] = None
    nb_chantiers_actifs: Optional[int] = None
    nb_anomalies: Optional[int] = None

    score_transports: Optional[float] = None
    score_transport_offre: Optional[float] = None
    score_transport_intensite: Optional[float] = None
    nb_gares: Optional[int] = None
    nb_stations_velib: Optional[int] = None
    capacite_velib_totale: Optional[float] = None
    nb_lignes_transport: Optional[int] = None
    lignes_par_gare_moyen: Optional[float] = None
    nb_modes_lourds: Optional[int] = None
    nb_arrets_bus: Optional[int] = None
    pct_arrets_accessibles: Optional[float] = None
    flux_multimodal: Optional[float] = None
    flux_velo_trott: Optional[float] = None
    flux_bus: Optional[float] = None
    flux_motorise: Optional[float] = None
    pct_flux_velo_trott: Optional[float] = None
    pct_flux_motorise: Optional[float] = None
    pct_flux_voie_cyclable: Optional[float] = None

    score_loisirs: Optional[float] = None
    nb_evenements: Optional[int] = None
    nb_cinemas: Optional[int] = None
    nb_terrasses: Optional[int] = None
    nb_musees: Optional[int] = None

    score_services: Optional[float] = None
    nb_ecoles: Optional[int] = None
    nb_maternelles: Optional[int] = None
    nb_colleges: Optional[int] = None
    nb_bibliotheques: Optional[int] = None
    nb_bureaux_poste: Optional[int] = None
    nb_ensup: Optional[int] = None

    score_global: Optional[float] = None

    class Config:
        from_attributes = True


class GeoFeature(BaseModel):
    type: str = "Feature"
    geometry: Any
    properties: KPIs


class GeoFeatureCollection(BaseModel):
    type: str = "FeatureCollection"
    features: List[GeoFeature]


class CompareResponse(BaseModel):
    arrondissement_1: KPIs
    arrondissement_2: KPIs


class TimelinePoint(BaseModel):
    annee: int
    prix_m2_median: Optional[float] = None
    score_qualite_vie: Optional[float] = None
    score_transports: Optional[float] = None
    score_loisirs: Optional[float] = None
    score_services: Optional[float] = None
    score_global: Optional[float] = None
    pct_logements_sociaux: Optional[float] = None


class TimelineResponse(BaseModel):
    arrondissement: int
    points: List[TimelinePoint]


class QuartierTimelineResponse(BaseModel):
    quartier_id: str
    quartier_code: Optional[str] = None
    arrondissement: Optional[int] = None
    nom: Optional[str] = None
    points: List[TimelinePoint]
