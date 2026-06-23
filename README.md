# Urban Data Explorer — Paris

Explorer, comprendre et comparer les dynamiques du logement et de la qualité de vie au cœur de Paris.

---

## Documentation complémentaire

- Guide de démarrage : ce README
- Logique cartographique détaillée : `docs/carte-detaillee-paris.md`
- Catalogue des sources de données : `docs/data_catalog.md`
- Benchmarks de performance pipeline : `docs/performance.md`
- Décisions d'architecture (ADR) : `docs/architecture_decisions.md`
- Analyse des écarts vs cahier des charges : `TODO_GAPS.md`

---

## Dernières évolutions

- **Revenus médians INSEE Filosofi 2021** : ingestion Bronze (CSV chunked), transformation Silver, agrégation Gold (`revenu_median_uc`) et calcul `taux_effort_achat` = prix_m2 × 50 / revenu médian
- **Répartition parc immobilier** : comptage T1/T2/T3/T4+ par tranche de surface, donut chart Chart.js dans la sidebar, `surface_mediane`, `nb_appartements`, `nb_maisons`, `pct_appartements`
- **Couches de points sur la carte** : 6 couches togglables (gares, Vélib, espaces verts, musées, cinémas, bibliothèques) — endpoint `/api/geo/points`, circles MapLibre avec popup hover
- **Scheduler automatique** : `pipeline/scheduler.py` (APScheduler, cron configurable), service `scheduler` dans `docker-compose.yml`, exécution quotidienne Bronze → Silver → Gold
- **Data catalog** : `docs/data_catalog.md` — 7 sources documentées avec lignage, champs utilisés et justification
- **Benchmarks** : `docs/performance.md` — timings par étape, EXPLAIN ANALYZE PostgreSQL, tailles des couches
- **ADR batch vs streaming** : `docs/architecture_decisions.md` — justification du choix batch, architecture Medallion, REST JWT
- Carte principale centrée sur les 80 quartiers administratifs
- Choroplèthe avec échelle dynamique par indicateur, palette inversée pour `prix_m2_median`
- DVF géolocalisé historique intégré (prix au m² par arrondissement et par quartier)
- Recherche d'adresse (BAN) avec suppression rapide du pin
- Comparaison enrichie : arrondissement vs arrondissement, ou quartier vs quartier
- API sécurisée JWT avec écran de connexion frontend
- Barres de progression `tqdm` dans les scripts Bronze, Silver et Gold
- Bronze : récupération complète si `api_max_records` absent, fallback `exports/json` OpenDataSoft

---

## Journal de travail

- `2026-06-23` : revenus médians INSEE Filosofi 2021 (GAP 1) — bronze → silver → gold → API → frontend
- `2026-06-23` : répartition types de logement + donut chart (GAP 2)
- `2026-06-23` : couches de points togglables sur la carte (GAP 3)
- `2026-06-23` : scheduler automatique pipeline + service Docker (GAP 4)
- `2026-06-23` : data catalog, benchmarks, ADR batch/streaming (GAPs 6/8/9)
- `2026-06-23` : sécurisation API FastAPI avec JWT Bearer, route `POST /api/auth/login`, vérification `GET /api/auth/me`
- `2026-06-23` : connexion frontend JWT avec stockage local du token et rechargement après authentification
- `2026-06-23` : ajout barres de progression `tqdm` dans les 3 scripts pipeline
- `2026-06-23` : correction Bronze — `api_max_records` absent = tout récupérer (plus de fallback à `500`)
- `2026-06-23` : fallback Bronze via `exports/json` pour datasets OpenDataSoft > 10 000 lignes

---

## Architecture

```
APIs / fichiers locaux (CSV, GeoJSON, Parquet)
              ↓  bronze_feeder.py
  [Bronze — MinIO Parquet]         ← copie brute immuable, partitionnée par ingestion_date
              ↓  silver_transformer.py
  [Silver — MongoDB]               ← nettoyage, typage, géocodage WGS84, spatialisation quartier
              ↓  gold_aggregator.py
  [Gold — PostgreSQL / PostGIS]    ← KPIs agrégés par quartier × année, géométries polygones
              ↓  FastAPI (JWT)
  [API REST]                       ← /api/geo, /api/kpis, /api/timeline, /api/compare, /api/geo/points
              ↓  MapLibre GL JS + Chart.js
  [Dashboard web interactif]       ← choroplèthe + points + timeline + comparaison + géocodage BAN
```

Le pipeline est exécuté automatiquement chaque nuit via le scheduler APScheduler (service `scheduler` Docker).  
Pour le détail des choix batch vs streaming et l'architecture Medallion : `docs/architecture_decisions.md`.

### Pourquoi cette stack ?

| Technologie | Type | Rôle |
|-------------|------|------|
| **MinIO** | Object Store (S3) | Bronze immuable — Parquet columnar, versioning par partition |
| **MongoDB** | Document Store NoSQL | Silver flexible, index `2dsphere` pour requêtes géospatiales |
| **PostgreSQL + PostGIS** | SQL Relationnel | Gold tabulaire, KPIs typés, jointures géométriques |
| **APScheduler** | Scheduler Python | Exécution cron quotidienne Bronze → Silver → Gold |

---

## Démarrage rapide

### 1. Prérequis

- Docker + Docker Compose
- Python 3.11+
- Node.js 18+ (frontend)

### 2. Configuration

```bash
cp .env.example .env
# Adapter les mots de passe et le secret JWT si nécessaire
```

Variables d'environnement clés :

```env
API_AUTH_USER=admin
API_AUTH_PASSWORD=change-me
API_JWT_SECRET=change-me-dev-jwt-secret
API_JWT_EXPIRE_MINUTES=120
API_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173

# Scheduler (optionnel, défaut : 2 h chaque nuit)
PIPELINE_CRON=0 2 * * *
PIPELINE_RUN_ON_START=false
```

### 3. Infrastructure

```bash
docker-compose up -d
# Lance : MinIO, MongoDB, PostgreSQL, API FastAPI, Scheduler automatique
```

### 4. Pipeline manuel (premier lancement ou rejeu)

```bash
pip install -r requirements.txt

# 1. Init schéma Gold
python pipeline/init_db.py

# 2. Ingestion Bronze → MinIO
python pipeline/bronze_feeder.py

# 3. Transformation Silver → MongoDB
python pipeline/silver_transformer.py

# 4. Agrégation Gold → PostgreSQL
python pipeline/gold_aggregator.py
```

> Le scheduler Docker reprend ensuite automatiquement chaque nuit.  
> Pour forcer une exécution immédiate via Docker : `PIPELINE_RUN_ON_START=true docker-compose up scheduler`

### 5. API

```bash
uvicorn api.main:app --reload --port 8000
# Docs interactives : http://localhost:8000/docs
```

Le port PostgreSQL est `5433` dans `docker-compose.yml` pour éviter les conflits avec un PostgreSQL local sur `5432`.

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
| CSV local (auto-sep) | fibre, base_imb | `_detect_sep()` + `pd.read_csv()` |
| CSV SDMXmeta | INSEE Filosofi 2021 | `pd.read_csv()` chunked + filtre ARM |
| JSON local | qualité de l'air | `json.load()` + `pd.json_normalize()` |
| API REST paginée (OpenDataSoft) | sanisettes, chantiers | `fetch_api()` |
| API REST paginée (IDF / transport) | cinémas, musées, gares | `fetch_api_generic()` |
| GeoJSON (FeatureCollection) | arrondissements | `requests.get()` + parsing |

### Datasets par indicateur

#### Indicateur 1 — Qualité de vie
| Dataset | Source | Format |
|---------|--------|--------|
| Îlots de fraîcheur — espaces verts | parisdata | Parquet local |
| Îlots de fraîcheur — équipements | parisdata | Parquet local |
| Arbres de Paris | parisdata | Parquet local |
| Qualité de l'air NO2/PM2.5 (Airparif) | datagouv | JSON local |
| Fibre — déploiement actuel (75) | datagouv | CSV local |
| Fibre — base immeubles / coaxiale / débit | datagouv | CSV local |
| Sanisettes publiques | parisdata | API |
| Chantiers à Paris | parisdata | API |
| Anomalies (Dans ma rue) | parisdata | API |

#### Indicateur 2 — Transports
| Dataset | Source | Format |
|---------|--------|--------|
| Comptages multimodaux permanents | parisdata | API |
| Vélib — stations disponibilité | parisdata | API |
| Gares de voyageurs IDF | idfm | API |
| Arrêts de bus IDF | idfm | API |

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
| DVF géolocalisées Paris (2014–2025) | data.gouv.fr / Etalab | CSV.gz annuel |
| **Revenus médians INSEE Filosofi 2021** | data.gouv.fr / INSEE | CSV SDMXmeta |

---

## KPIs calculés

### Nouveaux indicateurs immobiliers (v1.1+)

| KPI | Formule | Couche |
|-----|---------|--------|
| `revenu_median_uc` | `OBS_VALUE` Filosofi `MED_SL` (EUR/an) | Silver → Gold |
| `taux_effort_achat` | `prix_m2_median × 50 / revenu_median_uc` (années de revenu) | Gold |
| `surface_mediane` | médiane `surface_reelle_bati` DVF (m²) | Gold |
| `nb_appartements` / `nb_maisons` | comptage par `type_local` DVF | Gold |
| `pct_appartements` | `nb_appartements / (nb_appartements + nb_maisons) × 100` | Gold |
| `nb_t1` | transactions DVF avec surface ≤ 25 m² | Gold |
| `nb_t2` | 26–45 m² | Gold |
| `nb_t3` | 46–65 m² | Gold |
| `nb_t4plus` | ≥ 66 m² | Gold |

### 4 scores composites (0–100)

**Qualité de vie** : espaces verts, arbres, fibre, sanisettes, qualité air — pénalité chantiers/anomalies

**Transports** : `0.6 × offre + 0.4 × intensité`
- Offre : gares, Vélib, lignes, modes lourds, arrêts bus, accessibilité
- Intensité : flux multimodal, vélo/trottinette, bus, voies cyclables

**Loisirs** : densité événements, cinémas, terrasses, musées

**Services publics** : écoles, collèges, bibliothèques, bureaux de poste, enseignement supérieur

**Score global** = moyenne des 4 scores.

---

## API REST

| Méthode | Route | Description |
|---------|-------|-------------|
| GET | `/` | Accueil API |
| POST | `/api/auth/login` | Authentification → JWT |
| GET | `/api/auth/me` | Vérification token |
| GET | `/api/geo/arrondissements` | GeoJSON 20 arrondissements + KPIs |
| GET | `/api/geo/quartiers` | GeoJSON 80 quartiers + KPIs |
| **GET** | **`/api/geo/points?type=`** | **GeoJSON points Silver (gares\|velib\|espaces_verts\|musees\|cinemas\|bibliotheques)** |
| GET | `/api/kpis/{1-20}` | KPIs arrondissement |
| GET | `/api/kpis/quartier/{id}` | KPIs quartier |
| GET | `/api/timeline/{1-20}` | Évolution temporelle arrondissement |
| GET | `/api/timeline/quartier/{id}` | Évolution temporelle quartier |
| GET | `/api/compare?arr1=X&arr2=Y` | Comparaison côte à côte |
| GET | `/api/health` | Santé API |

Toutes les routes métier exigent `Authorization: Bearer <jwt>`.  
Routes publiques : `/`, `/api/auth/login`, `/api/health`, `/favicon.ico`.

Docs Swagger : `http://localhost:8000/docs`

---

## Stack technologique

| Couche | Technologie | Justification |
|--------|-------------|---------------|
| Ingestion | Python + pandas + boto3 | Multi-formats, upload S3 |
| Bronze | MinIO (S3) + Parquet | Stockage immuable, columnar, partitionné |
| Silver | MongoDB 7 + PyMongo | Schéma flexible, index `2dsphere` |
| Gold | PostgreSQL 16 + PostGIS | KPIs tabulaires, jointures géospatiales |
| Scheduler | APScheduler | Cron Python natif, aucune dépendance externe |
| API | FastAPI + SQLAlchemy + PyMongo | Performance, docs auto, dual DB |
| Auth | JWT (python-jose) | Stateless, compatible multi-instance |
| Carte | MapLibre GL JS | Open-source, choroplèthe + cercles natifs |
| Graphiques | Chart.js | Donut, radar, ligne — léger |
| Géocodage | API BAN (data.gouv.fr) | Gratuit, officiel France |
| Build frontend | Vite | Rapide, proxy API intégré |

---

## Structure du projet

```
.
├── pipeline/
│   ├── bronze_feeder.py       # Ingestion → MinIO bronze (25+ sources)
│   ├── silver_transformer.py  # Nettoyage → MongoDB silver
│   ├── gold_aggregator.py     # Agrégation KPIs → PostgreSQL
│   ├── init_db.py             # DDL PostgreSQL (CREATE TABLE + migrations)
│   └── scheduler.py           # Scheduler APScheduler (cron Bronze→Silver→Gold)
├── api/
│   ├── main.py                # FastAPI app
│   ├── database.py            # SQLAlchemy engine (PostgreSQL)
│   ├── mongo.py               # Client MongoDB (Silver read)
│   ├── models.py              # Schémas Pydantic (KPIs, GeoFeature, Timeline…)
│   ├── security.py            # JWT, Bearer, auth .env
│   └── routers/               # auth, geo (+ /points), kpis, timeline, compare
├── frontend/
│   ├── index.html             # Contrôles carte (indicateur, année, points, comparaison)
│   └── src/
│       ├── main.js            # Orchestration, toggles couches de points
│       ├── map.js             # MapLibre : choroplèthe, highlight, togglePointLayer()
│       ├── sidebar.js         # KPIs, donut surfaces, timeline chart
│       ├── compare.js         # Radar comparaison
│       ├── geocode.js         # Recherche BAN
│       └── style.css
├── docs/
│   ├── carte-detaillee-paris.md
│   ├── data_catalog.md        # Catalogue sources (lignage, licence, justification)
│   ├── performance.md         # Benchmarks pipeline + EXPLAIN ANALYZE
│   └── architecture_decisions.md  # ADR batch/streaming, Medallion, REST JWT
├── Dockerfile.api
├── Dockerfile.scheduler
├── docker-compose.yml
├── requirements.txt
├── TODO_GAPS.md               # Analyse des écarts vs cahier des charges
└── .env.example
```

---

## Logique cartographique

**Niveau affiché** : 80 quartiers administratifs (source : Paris Open Data)  
**Fond de carte** : tuiles vectorielles CartoDB Positron / Dark Matter  
**Choroplèthe** : échelle dynamique calculée sur les valeurs réelles de chaque indicateur

**Couches de points Silver** (togglables via les checkboxes) :
- Gares IDFM (jaune)
- Stations Vélib (bleu)
- Espaces verts (vert)
- Musées (violet)
- Cinémas (rose)
- Bibliothèques (teal)

**Comparaison** : arrondissement vs arrondissement, ou quartier vs quartier (radar + tableau).

Pour la logique détaillée : `docs/carte-detaillee-paris.md`

---

## Versioning

| Tag | Contenu |
|-----|---------|
| `v0.1.0` | Bronze + Silver initiaux (15 datasets) |
| `v0.2.0` | Bronze + Silver étendus (27 datasets, 4 indicateurs) |
| `v0.3.0` | Gold layer (PostgreSQL/PostGIS) |
| `v0.4.0` | API FastAPI + Docker |
| `v1.0.0` | Frontend complet (MapLibre + timeline + comparaison + géocodage BAN) |
| `v1.1.0` | Revenus médians INSEE Filosofi + taux_effort_achat |
| `v1.2.0` | Répartition types de logement + donut chart |
| `v1.3.0` | Couches de points togglables sur la carte |
| `v1.4.0` | Scheduler automatique pipeline (APScheduler + Docker) |
| `v1.5.0` | Data catalog + benchmarks + ADR architecture |

---

## Conformité RNCP40875 Bloc 1

| Compétence | Implémentation |
|-----------|----------------|
| C1.1 — Collecter données multi-sources, multi-formats | 28 datasets : Parquet, CSV, CSV SDMXmeta (Filosofi), JSON, API REST, GeoJSON |
| C1.2 — Stocker en format optimisé | Parquet columnar (Bronze MinIO) + documents MongoDB (Silver) |
| C1.3 — Nettoyer, normaliser, géocoder | WKB→GeoJSON, Lambert93→WGS84, filtrage ARM codes INSEE, géocodage BAN |
| C1.4 — Architecture en zones (Medallion) | Bronze (brut) → Silver (enrichi) → Gold (agrégé) |
| C2.1 — Versioning & traçabilité | Partition `ingestion_date=`, `_ingested_at`, `_meta.json`, tags git |
| C2.2 — Bases NoSQL | MongoDB Silver (document store, index `2dsphere`) |
| C2.3 — Bases SQL | PostgreSQL + PostGIS Gold (ST_Within, ST_AsGeoJSON) |
| C2.4 — Automatisation pipeline | Scheduler APScheduler cron + service Docker `scheduler` |
| C2.5 — API performante et filtrable | FastAPI + SQLAlchemy, `?annee=`, `?indicateur=`, `?type=`, JWT |
| C2.6 — Dashboard cartographique interactif | MapLibre choroplèthe + points Silver + slider + comparaison + géocodage |
| C2.7 — Dataviz accessible | ARIA labels, Chart.js donut/radar/ligne, contraste couleurs |
| C2.8 — Indicateurs composites originaux | Qualité vie / Transports / Loisirs / Services + taux_effort_achat |
