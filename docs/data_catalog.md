# Data Catalog — Urban Data Explorer Paris
> Livrable 3 — Justification des sources et documentation qualité
> 30 datasets · 4 indicateurs composites · 1 couche immobilière

---

## Sources et conventions

| Code source | Plateforme | Base URL |
|---|---|---|
| `paris_opendata` | Paris Open Data (OpenDataSoft) | `https://opendata.paris.fr/api/explore/v2.1/catalog/datasets` |
| `idf_opendata` | Île-de-France Mobilités | `https://data.iledefrance-mobilites.fr/api/explore/v2.1/catalog/datasets` |
| `transport_gouv` | opendata.paris.fr (transports) | `https://opendata.paris.fr/api/explore/v2.1/catalog/datasets` |
| `datagouv` | data.gouv.fr | `https://static.data.gouv.fr/resources/` |
| `insee_datagouv` | INSEE Filosofi via data.gouv.fr | Fichier CSV statique |
| `etalab` | DVF+ Etalab | `https://apidfplus.data.gouv.fr/` |

---

## INDICATEUR 1 — QUALITÉ DE VIE

| ID | Label | Source | Format | Fréquence | Colonnes clés | Qualité |
|---|---|---|---|---|---|---|
| `ilots_fraicheur_espaces_verts` | Îlots de fraîcheur — Espaces verts frais | `paris_opendata` | Parquet (local) | Annuelle | `geo_point_2d`, `arrondissement` | Complètes, ~99% non-null |
| `arbres` | Arbres de Paris | `paris_opendata` | Parquet (local) | Annuelle | `geo_point_2d`, `arrondissement`, `genre` | Complètes, ~410 000 arbres |
| `ilots_fraicheur_equipements` | Îlots de fraîcheur — Équipements & Activités | `paris_opendata` | Parquet (local) | Annuelle | `geo_point_2d`, `type_equipement` | Complètes |
| `qualite_air` | Qualité de l'air — NO2 PM2.5 PM10 O3 | `datagouv` | JSON (local) | Horaire archivé | `date`, `polluant`, `valeur`, `arrondissement` | Couvre 2015–2025, quelques nulls sur PM10 (<5%) |
| `fibre_actuel` | Fibre — Déploiement actuel Paris 75 | `datagouv` | CSV (local) | Semestrielle | `imb_id`, `statut_immeuble`, `code_insee` | ~98% complètes |
| `fibre_base_imb` | Fibre — Base immeubles Paris 75 | `datagouv` | CSV (local) | Semestrielle | `imb_id`, `code_insee`, `nbre_log` | ~97% complètes |
| `fibre_base_imb_fc` | Fibre — Base immeubles fibre coaxiale Paris 75 | `datagouv` | CSV (local) | Semestrielle | `imb_id`, `code_insee` | ~95% complètes |
| `fibre_debit_filaire` | Fibre — Débit filaire par département | `datagouv` | CSV (local) | Annuelle | `code_dep`, `debit_moy`, `annee` | Complètes |
| `fibre_operateur` | Fibre — Opérateurs | `datagouv` | CSV (local) | Semestrielle | `op_id`, `nom_operateur` | Complètes, table de référence |
| `sanisettes` | Sanisettes publiques | `paris_opendata` | API paginée | Mensuelle | `geo_point_2d`, `arrondissement`, `statut` | ~98% non-null, ~700 enregistrements |
| `chantiers` | Chantiers à Paris | `paris_opendata` | API paginée | Hebdomadaire | `geo_point_2d`, `arrondissement`, `date_debut` | ~90% non-null sur dates |
| `anomalies` | Dans ma rue — Anomalies signalées | `paris_opendata` | API export CSV | Quotidienne | `geo_point_2d`, `arrondissement`, `type_anomalie` | ~300 000 signalements, ~5% sans coordonnées |
| `zones_touristiques` | Zones touristiques internationales | `paris_opendata` | API paginée | Stable | `geo_shape`, `nom_zone` | Complètes, 10 zones |

---

## INDICATEUR 2 — TRANSPORTS

| ID | Label | Source | Format | Fréquence | Colonnes clés | Qualité |
|---|---|---|---|---|---|---|
| `voies` | Comptages multimodaux vélo/trottinette/bus | `paris_opendata` | API export CSV | Temps réel (archivé) | `t`, `counts`, `geo_point_2d`, `mode` | ~100 000 passages, 3% nulls sur mode |
| `velib` | Vélib — Stations et disponibilité | `transport_gouv` | API paginée | Temps réel | `stationcode`, `geo_point_2d`, `capacity`, `numbikesavailable` | Complètes, ~1 500 stations |
| `gares` | Gares de voyageurs — Île-de-France | `idf_opendata` | API IDF paginée | Stable | `nom_long`, `geo_point_2d`, `ligne` | Complètes, ~500 gares IDF |
| `bus` | Arrêts de bus — Île-de-France | `idf_opendata` | API IDF paginée | Mensuelle | `nom_arret`, `geo_point_2d`, `ligne` | ~25 000 arrêts, ~2% sans coordonnées |

---

## INDICATEUR 3 — LOISIRS

| ID | Label | Source | Format | Fréquence | Colonnes clés | Qualité |
|---|---|---|---|---|---|---|
| `evenements_paris` | Que faire à Paris — Événements | `paris_opendata` | API paginée | Quotidienne | `title`, `geo_point_2d`, `date_start`, `arrondissement` | ~5 000 événements, ~10% sans coordonnées |
| `terrasses` | Terrasses autorisées | `paris_opendata` | API paginée | Annuelle | `geo_point_2d`, `arrondissement`, `surface` | ~15 000 terrasses, complètes |
| `cinemas_idf` | Salles de cinéma — Île-de-France | `idf_opendata` | API IDF paginée | Annuelle | `nom`, `geo_point_2d`, `nb_salles`, `commune` | Complètes, ~300 cinémas IDF |
| `musees_idf` | Musées — Île-de-France | `idf_opendata` | API IDF paginée | Annuelle | `nom_musee`, `geo_point_2d`, `commune` | Complètes, ~100 musées franciliens |

---

## INDICATEUR 4 — SERVICES PUBLICS

| ID | Label | Source | Format | Fréquence | Colonnes clés | Qualité |
|---|---|---|---|---|---|---|
| `ecoles_elementaires` | Écoles élémentaires — Paris | `paris_opendata` | API paginée | Annuelle | `nom_etablissement`, `geo_point_2d`, `arrondissement` | Complètes, ~450 écoles |
| `maternelles_secteurs` | Secteurs scolaires — Maternelles Paris | `paris_opendata` | API paginée | Annuelle | `geo_shape`, `arrondissement` | Complètes, polygones de secteur |
| `colleges_secteurs` | Secteurs scolaires — Collèges Paris | `paris_opendata` | API paginée | Annuelle | `geo_shape`, `arrondissement` | Complètes |
| `bibliotheques` | Bibliothèques — Postes publics Paris | `paris_opendata` | API paginée | Stable | `nom`, `geo_point_2d`, `arrondissement` | Complètes, ~80 postes |
| `enseignement_superieur` | Enseignement supérieur — IDF | `idf_opendata` | API IDF paginée | Annuelle | `nom_etablissement`, `geo_point_2d`, `commune` | Complètes, ~250 établissements |
| `bureaux_poste` | Bureaux de poste — Île-de-France | `idf_opendata` | API IDF paginée | Semestrielle | `nom`, `geo_point_2d`, `commune` | Complètes, ~800 bureaux IDF |

---

## IMMOBILIER (exigé par le cahier des charges)

| ID | Label | Source | Format | Fréquence | Colonnes clés | Qualité |
|---|---|---|---|---|---|---|
| `revenus_medians` | Revenus médians — INSEE Filosofi 2021 | `insee_datagouv` | CSV SDMX (local) | Quinquennale | `GEO`, `OBS_VALUE`, `TIME_PERIOD`, `FILOSOFI_MEASURE` | Complètes pour les 20 arrondissements, année 2021 uniquement |
| `logements_sociaux` | Logements sociaux financés — Paris | `paris_opendata` | API paginée | Annuelle | `geo_point_2d`, `arrondissement`, `nb_logements`, `annee_livraison` | ~99% non-null, ~2021 enregistrements |
| `dvf_prix_m2` | Prix immobilier médian — DVF+ Etalab 2021–2025 | `etalab` | API DVF+ paginée | Annuelle | `id_mutation`, `date_mutation`, `valeur_fonciere`, `surface_reelle_bati`, `longitude`, `latitude` | ~200 000 transactions Paris, ~3% nulls surface |

---

## RÉFÉRENTIELS GÉOGRAPHIQUES

Ces datasets ne sont pas dans le pipeline Bronze mais sont chargés à la volée par `silver_transformer.py` via les API IGN / BAN :

| Référentiel | Source | Usage | Notes |
|---|---|---|---|
| Polygones arrondissements | API IGN GeoJSON | Point-in-polygon pour enrichissement géo | 20 arrondissements parisiens |
| Polygones quartiers (80) | API Paris Open Data | Point-in-polygon pour enrichissement quartier | 80 quartiers administratifs |
| Polygones IRIS (~1000) | API IGN / INSEE | Point-in-polygon pour enrichissement IRIS | Maillage fin intra-arrondissement |
| Géocodage adresses | API BAN (Base Adresse Nationale) | Résolution d'adresses textuelles en coordonnées | Utilisé dans le frontend |

---

## JUSTIFICATION DES CHOIX

### Pourquoi ces 4 indicateurs ?

| Indicateur | Justification |
|---|---|
| **Qualité de vie** | Mesure directe du cadre de vie quotidien : air, espaces verts, connectivité, nuisances. Répond à la question "est-ce agréable d'y vivre ?" |
| **Transports** | Accessibilité multimodale = facteur majeur de valorisation immobilière et de qualité de vie. Données IDFM et Paris Open Data fiables. |
| **Loisirs** | Offre culturelle et récréative — indicateur de dynamisme urbain, corrélé aux prix immobiliers dans la littérature économique. |
| **Services publics** | Accès à l'éducation et aux services de base — déterminant clé pour les familles, absent de la plupart des dashboards immobiliers comparables. |

### Pourquoi DVF+ Etalab et non les données brutes DVF ?

DVF+ est la version enrichie produite par Etalab : les doublons de mutation sont consolidés, les surfaces aberrantes filtrées, et l'identifiant unique `id_mutation` est stable. Les données brutes DVF contiennent des doublons à ~15% sur Paris.

### Pourquoi INSEE Filosofi 2021 seulement ?

Filosofi est une enquête quinquennale. La dernière disponible à la granularité arrondissement est 2021. Les données 2024 ne seront publiées qu'en 2026. Ce choix est contraint par la disponibilité, pas par un manque d'ambition.

---

## LIMITES CONNUES

| Dataset | Limite |
|---|---|
| `revenus_medians` | Année 2021 uniquement — pas de série temporelle longue |
| `qualite_air` | Données par capteur puis agrégées — précision à l'arrondissement, pas au quartier |
| `anomalies` | Biais de signalement : les arrondissements plus connectés signalent davantage |
| `evenements_paris` | Saisonnalité forte — les données reflètent la période d'ingestion |
| `dvf_prix_m2` | Exclut les mutations atypiques (enchères, donations) et les surfaces non renseignées |
