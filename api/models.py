from pydantic import BaseModel
from typing import Optional, List, Any


class KPIs(BaseModel):
    arrondissement: int
    annee: int

    prix_m2_median: Optional[float] = None
    pct_logements_sociaux: Optional[float] = None
    nb_logements_sociaux: Optional[int] = None

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
    nb_gares: Optional[int] = None
    nb_stations_velib: Optional[int] = None
    flux_multimodal: Optional[int] = None

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
