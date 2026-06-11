# Carte détaillée de Paris

Ce document explique la logique actuelle de la carte, la source géographique utilisée, la place de `quartier_paris` dans le pipeline, et la manière de faire évoluer ce niveau de détail.

## Résumé

La carte principale n’est plus pilotée uniquement par les 20 arrondissements.

Elle fonctionne maintenant sur :

- le dataset `quartier_paris` de l’Open Data Paris
- 80 quartiers administratifs
- une table `gold.quartier_kpis`
- un endpoint API GeoJSON dédié aux quartiers

Autrement dit :

1. `silver` prépare les points et leurs coordonnées.
2. `gold` charge les polygones des quartiers.
3. `gold` affecte les points Silver à un quartier administratif.
4. `gold` agrège les KPI au niveau quartier.
5. le frontend affiche directement cette maille fine.

## Quelle source pour la carte ?

Il faut distinguer 2 choses :

- le fond de carte
- le découpage métier

Fond de carte :

- MapLibre GL JS
- style `Carto Positron`
- ce fond sert seulement à l’affichage visuel

Découpage métier :

- source Open Data Paris `quartier_paris`
- export GeoJSON :
  `https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/quartier_paris/exports/geojson?lang=fr&timezone=Europe%2FParis`

Conclusion :

- pour l’API cartographique, la vraie source métier est `quartier_paris`
- MapLibre ne sert qu’au rendu

## Pourquoi `quartier_paris` et pas IRIS tout de suite ?

`quartier_paris` correspond aux quartiers administratifs.

Avantages :

- officiel
- simple à comprendre
- cohérent avec l’échelle Paris intra-muros
- 80 zones, donc déjà beaucoup plus fin que 20 arrondissements
- facile à exposer dans une choroplèthe lisible

IRIS serait un niveau encore plus fin, mais :

- il faut ajouter une nouvelle couche géographique
- refaire la logique d’affectation spatiale
- recalculer les agrégations à cette nouvelle maille
- vérifier que les datasets restent statistiquement pertinents à cette granularité

Donc la progression logique est :

1. arrondissement
2. quartier administratif
3. IRIS si le besoin métier le justifie

## Où cela se passe dans le pipeline ?

### Bronze

`bronze_feeder.py` récupère les données brutes.

À ce stade :

- pas d’agrégation métier
- pas encore de rattachement quartier

### Silver

`silver_transformer.py` homogénéise les données.

Rôle important pour la carte :

- normalisation des coordonnées
- conversion vers WGS84
- conservation du champ `location`
- normalisation des arrondissements

`silver` ne crée pas encore la carte finale, mais il prépare tout ce qu’il faut pour la jointure spatiale.

### Gold

Le niveau quartier se construit principalement dans `gold_aggregator.py`.

C’est là que se fait la vraie logique cartographique métier :

- chargement des polygones `quartier_paris`
- stockage dans `gold.quartiers_geo`
- affectation des points Silver à un quartier par test point-in-polygon
- calcul des KPI par quartier
- upsert dans `gold.quartier_kpis`

Donc oui : pour cette version du projet, l’essentiel du passage au niveau quartier se fait bien en `gold`.

## Tables Gold concernées

### `gold.quartiers_geo`

Contient la géométrie de référence.

Champs principaux :

- `quartier_id`
- `quartier_code`
- `arrondissement`
- `nom`
- `geom`

### `gold.quartier_kpis`

Contient les valeurs agrégées par quartier et par année.

Champs principaux :

- identifiants de zone
- scores composites
- variables brutes utiles à l’explication
- année

Exemples de colonnes transport :

- `nb_stations_velib`
- `capacite_velib_totale`
- `nb_gares`
- `nb_lignes_transport`
- `nb_arrets_bus`
- `pct_arrets_accessibles`
- `flux_multimodal`
- `flux_velo_trott`
- `flux_bus`
- `flux_motorise`
- `score_transport_offre`
- `score_transport_intensite`
- `score_transports`

## Logique d’affectation spatiale

Le principe est le suivant :

1. un document Silver contient un `location`
2. `gold_aggregator.py` charge les polygones des quartiers
3. pour chaque point, on cherche dans quel polygone il tombe
4. le point est compté dans ce quartier

La logique actuelle repose sur un test simple de type point-in-polygon.

Conséquence importante :

- si un dataset Silver a bien un point géographique, il peut être agrégé au niveau quartier
- si une source n’existe qu’au niveau arrondissement ou Paris global, elle ne peut pas être redistribuée parfaitement

## Que fait-on pour les données non localisables finement ?

Il y a deux cas.

### Cas 1 : données ponctuelles

Exemples :

- Vélib
- gares
- arrêts de bus
- comptages
- équipements
- écoles
- musées

Ces jeux de données sont rattachés directement à un quartier via leur `location`.

### Cas 2 : données déjà agrégées

Exemple typique :

- certains prix immobiliers DVF déjà disponibles à l’échelle arrondissement

Dans ce cas, la logique actuelle est :

- on conserve l’information héritée de l’arrondissement
- on ne fabrique pas une fausse précision statistique

Autrement dit :

- le quartier hérite de la valeur arrondissement quand la granularité source ne permet pas mieux

## Logique de score transport au niveau quartier

Le score transport reste un score unique.

Formule :

`score_transports = 0.6 × offre + 0.4 × intensite`

### Bloc offre

Variables utilisées :

- nombre de stations Vélib
- capacité totale Vélib
- nombre de gares
- nombre de lignes distinctes
- moyenne de lignes par gare
- nombre de modes lourds présents
- nombre d’arrêts de bus
- pourcentage d’arrêts accessibles

### Bloc intensité

Variables utilisées :

- flux multimodal total
- flux vélo / trottinette
- flux bus
- part des flux en voie cyclable
- part motorisée inversée

Le même principe que pour les arrondissements est conservé, mais recalculé cette fois à la maille quartier.

## API utilisée par le frontend

### Carte principale

Route :

- `GET /api/geo/quartiers?annee=...&indicateur=...`

Elle renvoie :

- un `FeatureCollection` GeoJSON
- les 80 quartiers
- les KPI déjà embarqués dans `properties`

### Fiche latérale d’un quartier

Routes :

- `GET /api/kpis/quartier/{quartier_id}`
- `GET /api/timeline/quartier/{quartier_id}`

## Frontend

Le frontend consomme maintenant la maille quartier comme niveau principal.

Concrètement :

- la choroplèthe colore les quartiers
- le clic ouvre la fiche du quartier
- la timeline du panneau droit lit `gold.quartier_kpis`
- la comparaison reste au niveau arrondissement

Cette séparation est volontaire :

- la carte détaillée sert à explorer finement
- la comparaison rapide reste plus lisible au niveau arrondissement

## Comment modifier la logique

### Si tu veux changer la géométrie de référence

Il faut intervenir dans :

- `pipeline/gold_aggregator.py`
- `pipeline/init_db.py`
- `api/routers/geo.py`
- `frontend/src/api.js`
- `frontend/src/map.js`

### Si tu veux ajouter un nouveau KPI quartier

Étapes :

1. vérifier que la source Silver contient une géométrie exploitable
2. ajouter l’agrégation dans `agg_quartiers()`
3. stocker la nouvelle colonne dans `gold.quartier_kpis`
4. exposer la valeur dans l’API si nécessaire
5. l’afficher dans la sidebar ou la carte

### Si tu veux passer un jour à IRIS

La logique sera presque la même :

1. charger une nouvelle couche géographique IRIS
2. créer `gold.iris_geo`
3. créer `gold.iris_kpis`
4. remplacer la fonction d’affectation quartier par une affectation IRIS
5. brancher le frontend sur `/api/geo/iris`

## Limites actuelles

- certaines métriques restent héritées de l’arrondissement
- les performances d’affectation spatiale peuvent devenir plus coûteuses si le volume Silver augmente fortement
- le score quartier dépend fortement de la qualité du `location` dans les collections Silver

## Commandes utiles

Initialisation SQL :

```bash
python pipeline/init_db.py
```

Recalcul Gold :

```bash
python pipeline/gold_aggregator.py
```

Lancer l’API :

```bash
uvicorn api.main:app --reload --port 8000
```

Lancer le frontend :

```bash
cd frontend
npm run dev
```

## En une phrase

La carte détaillée de Paris repose maintenant sur `quartier_paris` en couche géographique de référence, avec une agrégation réelle des KPI au niveau quartier administratif dans `gold`, puis exposition directe en GeoJSON via l’API.
