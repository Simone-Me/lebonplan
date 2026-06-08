# Urban Data Explorer — Paris

Explorer, comprendre et comparer les dynamiques du logement et de la qualité de vie au cœur de Paris.

---

## Architecture

```
APIs / fichiers locaux (Parquet, CSV, JSON, GeoJSON)
              ↓  bronze_feeder.py
  [Bronze — MinIO Parquet]       ← Object Store S3-compatible, partitionné par ingestion_date
              ↓  silver_transformer.py
  [Silver — MongoDB]             ← NoSQL Document Store, index géospatial 2dsphere
              ↓  gold_aggregator.py
  [Gold — PostgreSQL / PostGIS]  ← SQL relationnel, KPIs agrégés, géométries arrondissements
              ↓  FastAPI
  [API REST]                     ← /api/geo, /api/kpis, /api/timeline, /api/compare
              ↓  MapLibre GL JS
  [Dashboard web interactif]     ← Choroplèthe + timeline + comparaison + géocodage BAN
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

# Gold : agrégation KPIs → PostgreSQL
python pipeline/gold_aggregator.py
```

### 5. API

```bash
uvicorn api.main:app --reload --port 8000
# Docs interactives : http://localhost:8000/docs
```

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
| Comptages multimodaux permanents | parisdata | API |
| Vélib — stations disponibilité | parisdata | API |
| Gares de voyageurs IDF | data.iledefrance-mobilites.fr | API |

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

---

## 4 Indicateurs composites

### Qualité de vie (score 0–100)
Agrège : espaces verts, arbres, couverture fibre, sanisettes, qualité de l'air.
Soustrait : chantiers actifs et anomalies signalées.

### Transports (score 0–100)
Nombre de gares, stations Vélib, flux multimodal par arrondissement.

### Loisirs (score 0–100)
Densité d'événements culturels, cinémas, terrasses, musées.

### Services publics (score 0–100)
Écoles, collèges, bibliothèques, bureaux de poste, enseignement supérieur.

**Score global** = moyenne des 4 indicateurs.

---

## API REST

| Méthode | Route | Description |
|---------|-------|-------------|
| GET | `/api/geo/arrondissements` | GeoJSON 20 arrondissements + KPIs |
| GET | `/api/kpis/{1-20}` | KPIs d'un arrondissement |
| GET | `/api/timeline/{1-20}` | Évolution temporelle |
| GET | `/api/compare?arr1=X&arr2=Y` | Comparaison côte à côte |
| GET | `/api/health` | Santé de l'API |

Docs Swagger : `http://localhost:8000/docs`

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
│   ├── gold_aggregator.py    # Agrégation KPIs → PostgreSQL
│   └── init_db.py            # DDL PostgreSQL (CREATE TABLE)
├── api/
│   ├── main.py               # FastAPI app
│   ├── database.py           # SQLAlchemy engine
│   ├── models.py             # Schemas Pydantic
│   └── routers/              # geo, kpis, timeline, compare
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
