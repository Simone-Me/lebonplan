# Urban Data Explorer — Paris

Explorer, comprendre et comparer les dynamiques du logement et de la qualité de vie au cœur de Paris.

---

## Documentation complémentaire

- Guide de démarrage de l'application : ce README
- Logique cartographique et niveau de détail : `docs/carte-detaillee-paris.md`

---

## Dernières évolutions

- Carte principale centrée sur les 80 quartiers administratifs
- Choroplèthe avec échelle dynamique par indicateur, et palette inversée pour le `prix_m2_median`
- DVF géolocalisé historique intégré pour recalculer le prix au m² par arrondissement et par quartier administratif
- Recherche d’adresse avec suppression rapide du pin et reset
- Comparaison enrichie : arrondissement vs arrondissement, ou quartier administratif vs quartier administratif
- API sécurisée par JWT avec écran de connexion côté frontend
- Route d’accueil `/` et `favicon.ico` ajoutées pour éviter le `404` direct sur la racine Uvicorn
- Barres de progression ajoutées dans les scripts Bronze, Silver et Gold pour suivre l’avancement et estimer la fin des traitements
- Bronze : `api_max_records` absent signifie maintenant récupération complète du dataset, sans fallback caché à `500`
- Bronze : fallback automatique sur `exports/json` pour contourner la limite OpenDataSoft `offset + limit <= 10000`
- Bronze : progression visible aussi pendant les gros téléchargements `exports/json`, et pas seulement à la fin du dataset

---

## Journal de travail

- `2026-06-23` : sécurisation de l’API FastAPI avec JWT Bearer, route `POST /api/auth/login`, vérification `GET /api/auth/me`, et protection des routes métier `/api/geo`, `/api/kpis`, `/api/timeline`, `/api/compare`
- `2026-06-23` : ajout d’une connexion frontend avec stockage local du token, déconnexion, et rechargement de la carte uniquement après authentification
- `2026-06-23` : correction de la racine API pour répondre sur `http://127.0.0.1:8000/` au lieu d’un `404`
- `2026-06-23` : ajout de barres de progression `tqdm` dans `bronze_feeder.py`, `silver_transformer.py` et `gold_aggregator.py` avec suivi par dataset, chunks MongoDB et étapes d’agrégation
- `2026-06-23` : correction du Bronze pour que `api_max_records` commenté ou absent veuille dire “tout récupérer”, au lieu de retomber automatiquement à `500`
- `2026-06-23` : ajout d’un fallback Bronze via `exports/json` pour les datasets OpenDataSoft dépassant la limite d’API paginée `offset + limit <= 10000`
- `2026-06-23` : amélioration de la progression Bronze pour afficher l’avancement en octets pendant les gros exports JSON OpenDataSoft

---

## Architecture

```
APIs / fichiers locaux (Parquet, CSV, JSON, GeoJSON)
              ↓  bronze_feeder.py
  [Bronze — MinIO Parquet]       ← Object Store S3-compatible, partitionné par ingestion_date
              ↓  silver_transformer.py
  [Silver — MongoDB]             ← NoSQL Document Store, index géospatial 2dsphere
              ↓  gold_aggregator.py
  [Gold — PostgreSQL / PostGIS]  ← SQL relationnel, KPIs agrégés, géométries + KPI arrondissements/quartiers
              ↓  FastAPI
  [API REST]                     ← /api/geo, /api/kpis, /api/timeline, /api/compare
              ↓  MapLibre GL JS
  [Dashboard web interactif]     ← Choroplèthe quartier administratif + timeline + comparaison + géocodage BAN
```

### Pourquoi SQL + NoSQL ?

| Technologie | Type | Rôle |
|-------------|------|------|
| **MinIO** | Object Store (S3) | Lac de données brutes — Parquet columnar, versioning par partition |
| **MongoDB** | NoSQL Document Store | Silver enrichi, schéma flexible, index `2dsphere` pour requêtes géospatiales |
| **PostgreSQL + PostGIS** | SQL Relationnel | Gold tabulaire, KPIs typés, jointures géométriques, exposé via SQLAlchemy |

---

## Démarrage rapide

### 1. Prérequis

- Docker + Docker Compose
- Python 3.11+
- Node.js 18+ (frontend)

### 2. Configuration

```bash
cp .env.example .env
# Adapter les mots de passe si nécessaire
```

### 3. Infrastructure

```bash
docker-compose up -d
```

### 4. Pipeline complet

```bash
pip install -r requirements.txt

# Bronze : ingestion données brutes → MinIO
python pipeline/bronze_feeder.py

# Silver : nettoyage + géocodage → MongoDB + MinIO silver
python pipeline/silver_transformer.py

# Gold : init tables PostgreSQL
python pipeline/init_db.py

# Gold : agrégation KPIs arrondissement + quartier administratif → PostgreSQL
python pipeline/gold_aggregator.py
```

Ordre conseillé au premier lancement :

1. `docker-compose up -d`
2. `python pipeline/init_db.py`
3. `python pipeline/bronze_feeder.py`
4. `python pipeline/silver_transformer.py`
5. `python pipeline/gold_aggregator.py`

Si vous travaillez avec le `docker-compose.yml` du projet, la configuration attendue côté Python est maintenant :

```env
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5433
```

Le port `5433` évite le conflit fréquent avec un PostgreSQL Windows déjà lancé en local sur `5432`.

### 5. API

```bash
python -m uvicorn api.main:app --reload --port 8000
```
ou esseyer avec
```bash
uvicorn api.main:app --reload --port 8000
# Docs interactives : http://localhost:8000/docs
```

Routes utiles :

- Accueil API : `http://localhost:8000/`
- Santé : `http://localhost:8000/api/health`
- Swagger : `http://localhost:8000/docs`

Variables d’authentification à définir dans `.env` :

```env
API_AUTH_USER=admin
API_AUTH_PASSWORD=change-me
API_JWT_SECRET=change-me-dev-jwt-secret
API_JWT_EXPIRE_MINUTES=120
API_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
```

Option plus sûre :
- laisser `API_AUTH_PASSWORD` vide
- renseigner `API_AUTH_PASSWORD_HASH` au format `pbkdf2_sha256$iterations$salt$hash`

### 6. Frontend

```bash
cd frontend
npm install
npm run dev
# Dashboard : http://localhost:5173
```

---

## Sources de données

### Formats d'ingestion (variété RNCP)

| Format | Exemple | Loader |
|--------|---------|--------|
| Parquet local | arbres, espaces verts | `pd.read_parquet()` |
| CSV local (auto-sep) | fibre actuel, base_imb | `_detect_sep()` + `pd.read_csv()` |
| JSON local | qualité de l'air | `json.load()` + `pd.json_normalize()` |
| API REST paginée (OpenDataSoft Paris) | sanisettes, chantiers, événements | `fetch_api()` |
| API REST paginée (IDF / transport) | cinémas, musées, gares | `fetch_api_generic()` |
| GeoJSON (FeatureCollection) | arrondissements Paris | `requests.get()` + parsing |

### Datasets par indicateur

#### Indicateur 1 — Qualité de vie
| Dataset | Source | Format |
|---------|--------|--------|
| Îlots de fraîcheur — espaces verts | parisdata | Parquet local |
| Îlots de fraîcheur — équipements | parisdata | Parquet local |
| Arbres de Paris | parisdata | Parquet local |
| Qualité de l'air NO2/PM2.5/PM10/O3 | datagouv | JSON local |
| Fibre — déploiement actuel (75) | datagouv | CSV local |
| Fibre — base immeubles (75) | datagouv | CSV local |
| Fibre — base coaxiale (75) | datagouv | CSV local |
| Fibre — débit filaire | datagouv | CSV local |
| Fibre — opérateurs | datagouv | CSV local |
| Sanisettes publiques | parisdata | API |
| Chantiers à Paris | parisdata | API |
| Anomalies (Dans ma rue) | parisdata | API |
| Zones touristiques | parisdata | API |

#### Indicateur 2 — Transports
| Dataset | Source | Format |
|---------|--------|--------|
| Comptages multimodaux permanents des voies | parisdata | API |
| Vélib — stations disponibilité | parisdata | API |
| Gares de voyageurs IDF | data.iledefrance-mobilites.fr | API |
| Arrêts de bus IDF | data.iledefrance-mobilites.fr | API |

#### Indicateur 3 — Loisirs
| Dataset | Source | Format |
|---------|--------|--------|
| Que faire à Paris — événements | parisdata | API |
| Terrasses autorisées | parisdata | API |
| Salles de cinéma IDF | data.iledefrance.fr | API |
| Musées IDF | data.iledefrance.fr | API |

#### Indicateur 4 — Services publics
| Dataset | Source | Format |
|---------|--------|--------|
| Écoles élémentaires Paris | parisdata | API |
| Secteurs scolaires maternelles | parisdata | API |
| Secteurs scolaires collèges | parisdata | API |
| Bibliothèques — postes publics | parisdata | API |
| Enseignement supérieur IDF | data.iledefrance.fr | API |
| Bureaux de poste IDF | data.iledefrance.fr | API |

#### Immobilier
| Dataset | Source | Format |
|---------|--------|--------|
| Logements sociaux financés Paris | parisdata | API |
| DVF géolocalisées Paris (historique 2021–2025) | data.gouv.fr | CSV.gz annuel |

---

## 4 Indicateurs composites

### Qualité de vie (score 0–100)
Agrège : espaces verts, arbres, couverture fibre, sanisettes, qualité de l'air.
Soustrait : chantiers actifs et anomalies signalées.

### Transports (score 0–100)
La logique actuelle suit une formule unique :

`score_transports = 0.6 × offre + 0.4 × intensite`

Bloc offre :
- stations Vélib
- capacité Vélib
- gares
- lignes distinctes
- modes lourds présents
- arrêts de bus
- part d’arrêts accessibles

Bloc intensité :
- flux multimodal total
- flux vélo / trottinette
- flux bus
- part de flux en voie cyclable
- part motorisée inversée

### Loisirs (score 0–100)
Densité d'événements culturels, cinémas, terrasses, musées.

### Services publics (score 0–100)
Écoles, collèges, bibliothèques, bureaux de poste, enseignement supérieur.

**Score global** = moyenne des 4 indicateurs.

---

## API REST

| Méthode | Route | Description |
|---------|-------|-------------|
| GET | `/` | Accueil API + liens utiles |
| POST | `/api/auth/login` | Authentification et émission du JWT |
| GET | `/api/auth/me` | Vérification du token courant |
| GET | `/api/geo/arrondissements` | GeoJSON 20 arrondissements + KPIs |
| GET | `/api/geo/quartiers` | GeoJSON 80 quartiers administratifs + KPIs |
| GET | `/api/kpis/{1-20}` | KPIs d'un arrondissement |
| GET | `/api/kpis/quartier/{quartier_id}` | KPIs d'un quartier administratif |
| GET | `/api/timeline/{1-20}` | Évolution temporelle arrondissement |
| GET | `/api/timeline/quartier/{quartier_id}` | Évolution temporelle quartier |
| GET | `/api/compare?arr1=X&arr2=Y` | Comparaison côte à côte |
| GET | `/api/health` | Santé de l'API |

Docs Swagger : `http://localhost:8000/docs`

Sécurité actuelle :

- `/api/auth/login`, `/api/health`, `/` et `/favicon.ico` restent publics
- les routes métier exigent désormais `Authorization: Bearer <jwt>`
- le frontend stocke le token puis le renvoie automatiquement sur les appels API
- le CORS n’accepte plus `*` par défaut : il est limité aux origines listées dans `API_CORS_ORIGINS`

---

## Stack technologique

| Couche | Technologie | Justification |
|--------|-------------|---------------|
| Ingestion | Python + pandas + boto3 | Traitement multi-formats, upload S3 |
| Bronze | MinIO (S3) + Parquet | Stockage objet immuable, partitionné par date |
| Silver | MongoDB 7 + PyMongo | Schéma flexible, index géospatial 2dsphere |
| Gold | PostgreSQL 16 + PostGIS | KPIs tabulaires, jointures géospatiales |
| API | FastAPI + SQLAlchemy | Performance, docs auto, typage Pydantic |
| Carte | MapLibre GL JS | Open-source, pas de token, choroplèthe native |
| Graphiques | Chart.js | Radar, ligne, léger |
| Géocodage | API BAN (data.gouv.fr) | Gratuit, officiel France |
| Build frontend | Vite | Rapide, proxying API intégré |

---

## Structure du projet

```
.
├── pipeline/
│   ├── config.py             # Config centralisée (.env)
│   ├── bronze_feeder.py      # Ingestion → MinIO bronze
│   ├── silver_transformer.py # Nettoyage → MongoDB + MinIO silver
│   ├── gold_aggregator.py    # Agrégation KPIs arrondissement + quartier → PostgreSQL
│   └── init_db.py            # DDL PostgreSQL (CREATE TABLE)
├── api/
│   ├── main.py               # FastAPI app
│   ├── database.py           # SQLAlchemy engine
│   ├── models.py             # Schemas Pydantic
│   ├── security.py           # JWT, vérification Bearer, auth env
│   └── routers/              # auth, geo, kpis, timeline, compare
├── frontend/
│   ├── index.html
│   └── src/                  # main.js, map.js, sidebar.js, compare.js, geocode.js
├── datasrc/                  # Fichiers sources locaux (gitignored)
├── docker-compose.yml
├── Dockerfile.api
├── requirements.txt
└── .env.example
```

---

## Logique cartographique

Niveau affiché par défaut :
- `quartier_paris` de l’Open Data Paris
- 80 quartiers administratifs

Source de fond de carte :
- tuiles vectorielles MapLibre via le style `Carto Positron`

Source géographique métier :
- `gold.quartiers_geo` pour les polygones
- `gold.quartier_kpis` pour les valeurs agrégées

Logique de calcul :
1. Le `silver_transformer` homogénéise les coordonnées en WGS84 et garde `location`.
2. Le `gold_aggregator` affecte les points Silver à un quartier administratif via point-in-polygon.
3. Les agrégations métier sont recalculées à l’échelle quartier.
4. Les données ponctuelles géolocalisées, y compris les mutations DVF, peuvent être réagrégées à l’échelle quartier.
5. L’API expose directement un GeoJSON quartier enrichi en KPI pour la choroplèthe.

Comportement d’affichage actuel :

- Scores composites : couleurs calculées sur l’étendue réelle des valeurs affichées
- `prix_m2_median` : plus cher = rouge, moins cher = vert
- `nb_logements_sociaux` : affichage direct sur la carte quand le pourcentage n’est pas disponible

Comparaison :

- arrondissement vs arrondissement
- quartier administratif vs quartier administratif
- pas de comparaison mixte arrondissement/quartier

Pour le détail complet de cette logique et la façon de la modifier :
- `docs/carte-detaillee-paris.md`

---

## Versioning

| Tag | Contenu |
|-----|---------|
| `v0.1.0` | Bronze + Silver initiaux (15 datasets) |
| `v0.2.0` | Bronze + Silver étendus (27 datasets, 4 indicateurs) + config .env |
| `v0.3.0` | Gold layer (PostgreSQL/PostGIS) |
| `v0.4.0` | API FastAPI + Docker |
| `v1.0.0` | Frontend complet (MapLibre + timeline + comparaison + géocodage BAN) |

---

## Conformité RNCP40875 Bloc 1

| Compétence | Implémentation |
|-----------|----------------|
| Collecter données multi-sources, multi-formats | 27 datasets : Parquet, CSV, JSON, API REST (OpenDataSoft Paris, IDF, transport), GeoJSON |
| Stocker en format optimisé | Parquet columnar (Bronze MinIO) + Parquet Silver |
| Nettoyer, normaliser, géocoder | WKB→GeoJSON, Lambert93→WGS84, arrondissement normalisé 1-20, géocodage BAN |
| Architecture en zones (Medallion) | Bronze (brut) → Silver (enrichi) → Gold (agrégé) |
| Versioning & traçabilité | Partition `ingestion_date=`, `_ingested_at`, `_meta.json`, `_format_source`, tags git |
| Bases NoSQL | MongoDB Silver (document store, index 2dsphere) |
| Bases SQL | PostgreSQL + PostGIS Gold (schéma relationnel, ST_GeomFromGeoJSON) |
| API performante et filtrable | FastAPI + SQLAlchemy, filtres ?annee=, ?indicateur= |
| Dashboard cartographique interactif | MapLibre choroplèthe + slider timeline + mode comparaison + géocodage |
| Accessibilité dataviz | Labels ARIA, tooltips, contraste couleurs, responsive |
| 4 indicateurs composites originaux | Qualité de vie / Transports / Loisirs / Services publics |
