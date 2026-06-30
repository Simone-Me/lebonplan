# Urban Data Explorer — Paris

Explorer, comprendre et comparer les dynamiques du logement et de la qualité de vie au cœur de Paris.

---

## Documentation complémentaire

- Guide de démarrage : ce README
- Logique cartographique détaillée : `docs/carte-detaillee-paris.md`
- Catalogue des 30 sources de données : `docs/data_catalog.md`
- Rapport de performance pipeline : `docs/perf_report.md`

---

## Architecture

```
─── PIPELINE BATCH (nocturne, idempotent) ──────────────────────────────────────────
scheduler.py (APScheduler, 2h chaque nuit)
    ↓  bronze_feeder.py          (skip si déjà ingéré aujourd'hui)
  [Bronze — MinIO Parquet]       ← copie brute immuable, partition ingestion_date/
    ↓  silver_transformer.py     (skip si déjà transformé aujourd'hui)
  [Silver — MinIO Parquet]       ← clean.parquet enrichi WGS84 + IRIS/quartier (geopandas)
    ↓  gold_aggregator.py
  [Gold — Phase 1]  MinIO Silver → MongoDB gold   ← documents complets par collection
  [Gold — Phase 2]  MongoDB gold → PostgreSQL      ← KPIs agrégés × arrondissement/quartier/IRIS × année

─── PIPELINE STREAMING (near real-time, toutes les 5 min) ──────────────────────────
APIs volatiles — Vélib · Sanisettes · Chantiers · Anomalies · Voies (100 000 derniers enregistrements)
              ↓  kafka_producer.py  (fetch + publish)
  [Kafka — topics urban.*]       ← broker KRaft, rétention 24h, 5 partitions
              ↓  kafka_consumer.py  (upsert en continu)
  [Gold — MongoDB]               ← collections velib/sanisettes/chantiers/anomalies/voies en temps réel

─── API & FRONTEND ──────────────────────────────────────────────────────────────────
  [FastAPI (JWT)]                ← /api/geo, /api/kpis (fallback année/section), /api/streaming/status …
              ↓
  [Dashboard MapLibre + Chart.js] ← badges année données · countdown Kafka · timeline · comparaison
```

**Deux modes de mise à jour coexistent :**
- **Batch** : DVF, Filosofi INSEE, espaces verts, écoles… (25 datasets) — pipeline nocturne idempotent.
- **Streaming** : Vélib, Sanisettes, Chantiers, Anomalies, Voies (5 datasets) — Kafka producer/consumer toutes les 5 min, 100 000 derniers enregistrements, upsert MongoDB gold.

### Pourquoi cette stack ?

| Technologie | Type | Rôle |
|---|---|---|
| **MinIO** | Object Store (S3) | Bronze (raw Parquet) + Silver (clean Parquet) — immuable, partitionné par date |
| **MongoDB** | Document Store NoSQL | Gold documents — collections complètes, index `2dsphere`, cible des upserts Kafka |
| **PostgreSQL + PostGIS** | SQL Relationnel | Gold KPIs — agrégats typés par arrondissement/quartier/IRIS × année |
| **Apache Kafka** | Broker de streaming | KRaft (sans Zookeeper), rétention 24h, topics `urban.*` par dataset |
| **APScheduler** | Scheduler Python | Cron nocturne pour le pipeline batch (DVF, Filosofi…) |
| **FastAPI** | API REST | Performance, docs auto, dual DB (PostgreSQL + MongoDB) |
| **python-jose** | Auth JWT | Stateless, algorithme HS256 fixé, compatible multi-instance |
| **tenacity** | Retry HTTP | Backoff exponentiel sur tous les appels API réseau |
| **MapLibre GL JS** | Carte | Open-source, choroplèthe + cercles natifs |
| **Chart.js** | Graphiques | Donut, radar, ligne — léger |

---

## Lancement complet

> Suivre les étapes dans l'ordre. Prévoir ~20 minutes pour le premier pipeline complet (dépend du réseau).

### Prérequis

| Outil | Version minimale | Vérification |
|---|---|---|
| Docker + Docker Compose | 24+ / 2.20+ | `docker compose version` |
| Python | 3.11+ | `python --version` |
| Node.js | 18+ | `node --version` |

---

### Étape 1 — Configurer l'environnement

```bash
cp .env.example .env
```

**Générer le hash du mot de passe API** (obligatoire — l'API refuse de démarrer sans ça) :

```bash
python -c "import hashlib, os; s = os.urandom(16).hex(); h = hashlib.pbkdf2_hmac('sha256', b'VOTRE_MOT_DE_PASSE', s.encode(), 260000).hex(); print(f'API_AUTH_PASSWORD_HASH=pbkdf2_sha256$260000${s}${h}')"
# Copier la ligne entière dans .env
```

Éditer `.env` et vérifier ces variables :

```env
# Auth API
API_AUTH_USER=admin
API_AUTH_PASSWORD_HASH=pbkdf2_sha256$260000$<salt>$<hash>   # généré ci-dessus
API_JWT_SECRET=remplacer-par-une-valeur-aleatoire-longue
API_JWT_EXPIRE_MINUTES=120
API_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173

# Infrastructure (laisser les valeurs par défaut pour Docker)
MINIO_ACCESS_KEY=admin
MINIO_SECRET_KEY=password123
POSTGRES_DB=urban_data
POSTGRES_USER=admin
POSTGRES_PASSWORD=password123

# Scheduler batch (optionnel)
PIPELINE_CRON=0 2 * * *
PIPELINE_RUN_ON_START=false
```

---

### Étape 2 — Lancer l'infrastructure

```bash
docker-compose up -d
```

Vérifier que tous les services sont `healthy` :

```bash
docker-compose ps
# Attendre que minio, mongodb, postgres soient en "healthy" avant de continuer
# (environ 30 secondes au premier démarrage)
```

Services lancés :

| Service | Port | Description |
|---|---|---|
| MinIO | `9000` (API S3) · `9001` (console web) | Data lake Bronze + Silver (Parquet) |
| MongoDB | `27017` | Gold documents (collections complètes + upserts Kafka) |
| PostgreSQL | `5433` | Gold KPIs (5433 pour éviter les conflits locaux) |
| API FastAPI | `8000` | REST API |
| Scheduler | — | Pipeline batch nocturne (APScheduler) |
| Kafka | `9092` | Broker KRaft — streaming 5 datasets |
| kafka-producer | — | Fetch 100k enregistrements/5 min par dataset |
| kafka-consumer | — | Upsert MongoDB gold en continu |

---

### Étape 3 — Installer les dépendances Python

```bash
pip install -r requirements.txt
```

---

### Étape 4 — Initialiser le schéma PostgreSQL (Gold)

À faire **une seule fois** au premier lancement (ou après un `docker-compose down -v`) :

```bash
python pipeline/init_db.py
# Crée les tables gold.*, les index GIST et btree, active PostGIS
```

---

### Étape 5 — Lancer le pipeline batch (Bronze → Silver → Gold)

```bash
# Bronze : ingestion 25 sources batch → MinIO bronze/ (8–15 min selon réseau)
# Skip automatique si la partition du jour existe déjà (idempotent)
python pipeline/bronze_feeder.py

# Silver : transformation + PIP vectorisé geopandas → MinIO silver/ (3–5 min)
# Skip automatique si clean.parquet du jour existe déjà (idempotent)
set SILVER_WORKERS=10  # optionnel, parallélisme (défaut 5)
python pipeline/silver_transformer.py

# Gold Phase 1 : Silver Parquet → MongoDB gold (documents complets, upsert)
# Gold Phase 2 : MongoDB gold → PostgreSQL (KPIs agrégés par zone × année)
python pipeline/gold_aggregator.py
```

> Bronze et Silver affichent des barres `tqdm`. En cas d'API source indisponible, le dataset est ignoré.  
> Le scheduler Docker relance automatiquement le pipeline chaque nuit à 2h.  
> Les 5 datasets Kafka (Vélib, Sanisettes, Chantiers, Anomalies, Voies) ne passent **pas** par Bronze/Silver batch.

---

### Étape 6 — Vérifier le streaming Kafka (si activé)

```bash
# Voir les logs du producer — doit afficher des messages toutes les 5 min
docker-compose logs -f kafka-producer

# Sortie attendue (100 000 derniers enregistrements par dataset) :
# ✓ velib      — 100 000 messages → topic urban.velib
# ✓ sanisettes — 100 000 messages → topic urban.sanisettes
# ✓ chantiers  — 100 000 messages → topic urban.chantiers
# ✓ anomalies  — 100 000 messages → topic urban.anomalies
# ✓ voies      — 100 000 messages → topic urban.voies

# Voir les logs du consumer — upserts MongoDB gold
docker-compose logs -f kafka-consumer
# ✓ velib      — N upserts dans gold.velib
# ✓ voies      — N upserts dans gold.voies
```

---

### Étape 7 — Lancer le frontend

```bash
cd frontend
npm install
npm run dev
# Dashboard disponible : http://localhost:5173
```

---

### Récapitulatif des URLs

| Service | URL | Identifiants |
|---|---|---|
| **Dashboard** | http://localhost:5173 | login via l'écran d'auth |
| **API REST** | http://localhost:8000 | — |
| **Swagger UI** | http://localhost:8000/docs | — |
| **MinIO console** | http://localhost:9001 | `admin` / `password123` |

---

### Tests

```bash
pytest tests/ -v
```

| Fichier | Couverture |
|---|---|
| `tests/test_pipeline.py` | 20 tests — `parse_arrondissement`, `transform_dvf` (prix/m², surface zéro, années), `transform_revenus` |
| `tests/test_api.py` | 8 tests — health, login, JWT, protection endpoints, structure GeoJSON, structure KPI |

> Les tests API nécessitent une connexion PostgreSQL et MongoDB. En CI sans Docker, mocker `api.database` et `api.mongo`.

---

### Migration vers Kafka (si le projet tournait sans Kafka)

Si vous avez déjà le stack de base qui tourne et vous ajoutez Kafka :

```bash
# 1. Arrêter les services existants
docker-compose down

# 2. Installer la dépendance Python Kafka
pip install -r requirements.txt
# kafka-python-ng est déjà dans requirements.txt

# 3. Relancer (Kafka est maintenant dans docker-compose.yml)
docker-compose up -d

# 4. Vérifier que Kafka est healthy
docker-compose ps kafka
# Status doit être "healthy" (peut prendre 30-60 secondes)

# 5. Vérifier les topics créés automatiquement
docker-compose exec kafka /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list
# urban.velib
# urban.sanisettes
# urban.chantiers
# urban.anomalies
```

**Ce qui change avec Kafka :**
- Vélib, Sanisettes, Chantiers, Anomalies ne passent **plus** par le pipeline batch Bronze/Silver pour ces 4 datasets — ils sont maintenant mis à jour directement via Kafka toutes les 5 minutes.
- Les données dans MongoDB Silver pour ces collections sont désormais beaucoup plus fraîches (5 min vs 24h).
- L'API sert les mêmes endpoints — aucun changement côté frontend.
- Si Kafka est arrêté, le pipeline batch reprend le relais au prochain run nocturne.

---

## Résilience et tolérance aux pannes

| Mécanisme | Implémentation | Effet |
|---|---|---|
| **Ingestion idempotente** | `head_object` MinIO au début de Bronze et Silver — skip si la partition du jour existe | Relancer le pipeline ne ré-ingère ni ne ré-transforme ce qui est déjà fait |
| **Retry HTTP tenacity** | `_http_get_with_retry()` dans `bronze_feeder.py` — 3 tentatives, backoff exponentiel 2s→10s | Une source API indisponible est retenté 3 fois puis ignorée ; le pipeline continue |
| **Healthchecks Docker** | `healthcheck` sur MinIO, MongoDB, PostgreSQL dans `docker-compose.yml` | L'API et le scheduler ne démarrent qu'après que les services de données sont prêts |
| **Restart policies** | `restart: on-failure` (API), `restart: unless-stopped` (scheduler) | Redémarrage automatique en cas d'erreur inattendue |
| **Silver immuable (Parquet)** | Silver = fichiers Parquet MinIO, Gold lit le Silver déjà persisté | Une interruption pendant Gold ne perd pas les données transformées |
| **Kafka consumer restart** | `restart: unless-stopped` sur le consumer Docker | Si le consumer crash, il redémarre et reprend depuis le dernier offset commité — aucun message perdu |

---

## Sources de données

### Formats d'ingestion (variété RNCP)

| Format | Exemple | Loader |
|---|---|---|
| Parquet local | arbres, espaces verts | `pd.read_parquet()` |
| CSV local (auto-sep) | fibre, base_imb | `_detect_sep()` + `pd.read_csv()` |
| CSV SDMXmeta | INSEE Filosofi 2021 | `pd.read_csv()` chunked + filtre ARM |
| JSON local | qualité de l'air | `json.load()` + `pd.json_normalize()` |
| API REST paginée (OpenDataSoft) | sanisettes, chantiers, anomalies | `_http_get_with_retry()` + export streaming |
| API REST paginée (IDF / transport) | cinémas, musées, gares, bus | `_http_get_with_retry()` |
| API DVF+ Etalab | DVF prix/m² 2021–2025 | pagination sur années |

### Datasets par indicateur (25 batch + 5 streaming = 30 sources)

#### Indicateur 1 — Qualité de vie (8 batch + 3 streaming)
| Dataset | Source | Format | Mode |
|---|---|---|---|
| Îlots de fraîcheur — espaces verts | Paris Open Data | Parquet local | Batch |
| Îlots de fraîcheur — équipements | Paris Open Data | Parquet local | Batch |
| Arbres de Paris | Paris Open Data | Parquet local | Batch |
| Qualité de l'air NO2/PM2.5/PM10/O3 | data.gouv.fr | JSON local | Batch |
| Fibre — déploiement actuel (75) | data.gouv.fr | CSV local | Batch |
| Fibre — base immeubles Paris 75 | data.gouv.fr | CSV local | Batch |
| Fibre — base immeubles coaxiale | data.gouv.fr | CSV local | Batch |
| Fibre — débit filaire par département | data.gouv.fr | CSV local | Batch |
| Sanisettes publiques | Paris Open Data | Kafka streaming | **Streaming** |
| Chantiers à Paris | Paris Open Data | Kafka streaming | **Streaming** |
| Anomalies (Dans ma rue) | Paris Open Data | Kafka streaming | **Streaming** |

#### Indicateur 2 — Transports (2 batch + 2 streaming)
| Dataset | Source | Format | Mode |
|---|---|---|---|
| Gares de voyageurs IDF | IDF Mobilités | API paginée | Batch |
| Arrêts de bus IDF | IDF Mobilités | API paginée | Batch |
| Vélib — stations disponibilité | Paris Open Data | Kafka streaming | **Streaming** |
| Comptages multimodaux (Voies) | Paris Open Data | Kafka streaming | **Streaming** |

#### Indicateur 3 — Loisirs (4 datasets)
| Dataset | Source | Format |
|---|---|---|
| Que faire à Paris — événements | Paris Open Data | API paginée |
| Terrasses autorisées | Paris Open Data | API paginée |
| Salles de cinéma IDF | IDF Open Data | API paginée |
| Musées IDF | IDF Open Data | API paginée |

#### Indicateur 4 — Services publics (6 datasets)
| Dataset | Source | Format |
|---|---|---|
| Écoles élémentaires Paris | Paris Open Data | API paginée |
| Secteurs scolaires maternelles | Paris Open Data | API paginée |
| Secteurs scolaires collèges | Paris Open Data | API paginée |
| Bibliothèques — postes publics | Paris Open Data | API paginée |
| Enseignement supérieur IDF | IDF Open Data | API paginée |
| Bureaux de poste IDF | IDF Open Data | API paginée |

#### Immobilier (3 datasets)
| Dataset | Source | Format |
|---|---|---|
| Revenus médians INSEE Filosofi 2021 | data.gouv.fr / INSEE | CSV SDMXmeta |
| Logements sociaux financés Paris | Paris Open Data | API paginée |
| DVF+ prix/m² Paris 2021–2025 | data.gouv.fr / Etalab | API DVF+ annuelle |

Pour le détail complet (qualité, colonnes clés, limites) : `docs/data_catalog.md`

---

## KPIs calculés

### Indicateurs immobiliers

| KPI | Formule | Couche |
|---|---|---|
| `prix_m2_median` | médiane `valeur_fonciere / surface_reelle_bati` DVF | Gold |
| `revenu_median_uc` | `OBS_VALUE` Filosofi `MED_SL` (EUR/an) | Silver → Gold |
| `taux_effort_achat` | `prix_m2_median × 50 / revenu_median_uc` (années de revenu) | Gold |
| `surface_mediane` | médiane `surface_reelle_bati` DVF (m²) | Gold |
| `nb_appartements` / `nb_maisons` | comptage par `type_local` DVF | Gold |
| `pct_appartements` | `nb_appartements / (nb_appartements + nb_maisons) × 100` | Gold |
| `nb_t1` / `nb_t2` / `nb_t3` / `nb_t4plus` | tranches de surface DVF (≤25 / 26–45 / 46–65 / ≥66 m²) | Gold |

### 4 scores composites (0–100)

**Qualité de vie** : score de fraîcheur détaillé par espace vert/îlot de fraîcheur (végétation haute <25%→0.5pt · 25-50%→1pt · >50%→2pts, + ouverture 24h/24 + bonus amplitude horaire hebdomadaire vs les autres espaces verts), arbres, fibre, sanisettes, qualité air — pénalité chantiers/anomalies

**Transports** : `0.6 × offre + 0.4 × intensité`
- Offre : gares, Vélib, lignes, modes lourds, arrêts bus, accessibilité
- Intensité : flux multimodal, vélo/trottinette, bus, voies cyclables

**Loisirs** : densité événements, cinémas, terrasses, musées

**Services publics** : écoles, collèges, bibliothèques, bureaux de poste, enseignement supérieur

**Score global** = moyenne des 4 scores.

---

## API REST

| Méthode | Route | Auth | Description |
|---|---|---|---|
| GET | `/` | — | Accueil API |
| GET | `/api/health` | — | Santé API |
| GET | `/api/rate-limit` | — | Quota restant (100 req/min par IP) + secondes avant reset |
| POST | `/api/auth/login` | — | Authentification → JWT |
| GET | `/api/auth/me` | JWT | Vérification token |
| GET | `/api/geo/arrondissements?annee=&indicateur=` | JWT | GeoJSON 20 arrondissements + KPIs |
| GET | `/api/geo/quartiers?annee=&indicateur=` | JWT | GeoJSON 80 quartiers + KPIs |
| GET | `/api/geo/iris?annee=&indicateur=` | JWT | GeoJSON ~1000 IRIS + KPIs |
| GET | `/api/geo/points?type=` | JWT | GeoJSON points Silver (gares\|velib\|espaces_verts\|musees\|cinemas\|bibliotheques) |
| GET | `/api/kpis/{1-20}?annee=` | JWT | KPIs arrondissement (fallback année par section si NULL) |
| GET | `/api/kpis/quartier/{id}?annee=` | JWT | KPIs quartier |
| GET | `/api/kpis/iris/{id}?annee=` | JWT | KPIs IRIS |
| GET | `/api/timeline/{1-20}` | JWT | Évolution temporelle arrondissement |
| GET | `/api/timeline/quartier/{id}` | JWT | Évolution temporelle quartier |
| GET | `/api/compare?arr1=X&arr2=Y` | JWT | Comparaison côte à côte |
| GET | `/api/streaming/status` | JWT | Dernière mise à jour Kafka + intervalle refresh |

**Note pagination :** Les endpoints GeoJSON (`/geo/arrondissements`, `/geo/quartiers`, `/geo/iris`) renvoient le jeu complet intentionnellement. MapLibre charge une seule fois la géométrie en mémoire client pour un rendu choroplèthe fluide. Charge réseau estimée : ~150 KB gzippé pour les quartiers, ~800 KB pour les IRIS.

Docs Swagger : `http://localhost:8000/docs`

---

## Tests

```bash
pytest tests/ -v
```

| Fichier | Couverture |
|---|---|
| `tests/test_pipeline.py` | 20 tests — `parse_arrondissement`, `transform_dvf` (prix/m², années, surface zéro), `transform_revenus` (filtrage, normalisation) |
| `tests/test_api.py` | 8 tests — health check, login, rejet mot de passe incorrect, protection JWT, structure GeoJSON, structure KPI |

---

## Structure du projet

```
.
├── pipeline/
│   ├── bronze_feeder.py       # Ingestion 25 sources → MinIO bronze (idempotent, retry tenacity)
│   ├── silver_transformer.py  # Nettoyage → MinIO silver Parquet (idempotent, geopandas PIP)
│   ├── gold_aggregator.py     # Phase 1 : Silver → MongoDB gold | Phase 2 : MongoDB → PostgreSQL
│   ├── init_db.py             # DDL PostgreSQL (CREATE TABLE + index GIST + migrations)
│   ├── scheduler.py           # Scheduler APScheduler — pipeline batch nocturne
│   ├── kafka_producer.py      # Producer Kafka — 5 datasets × 100k enregistrements/5 min
│   ├── kafka_consumer.py      # Consumer Kafka — upsert MongoDB gold en continu
│   └── config.py              # Variables de connexion (MinIO, MongoDB, PostgreSQL)
├── api/
│   ├── main.py                # FastAPI app
│   ├── database.py            # SQLAlchemy engine (PostgreSQL)
│   ├── mongo.py               # Client MongoDB gold (read)
│   ├── models.py              # Schémas Pydantic (KPIs + annee_* par section, GeoFeature…)
│   ├── security.py            # JWT python-jose, PBKDF2, Bearer, rate limiter (100 req/min)
│   └── routers/               # auth, geo, kpis (fallback année), timeline, compare, streaming
├── tests/
│   ├── test_pipeline.py       # Tests unitaires pipeline (20 tests)
│   └── test_api.py            # Tests intégration API (8 tests)
├── frontend/
│   ├── index.html
│   └── src/
│       ├── main.js
│       ├── map.js
│       ├── sidebar.js
│       ├── compare.js
│       ├── geocode.js
│       └── style.css
├── docs/
│   ├── carte-detaillee-paris.md
│   ├── data_catalog.md        # Catalogue 30 datasets (source, qualité, colonnes clés)
│   └── perf_report.md         # Benchmarks pipeline + optimisations documentées
├── docker-compose.yml         # MinIO + MongoDB + PostgreSQL + API + Scheduler batch
├── Dockerfile.api
├── Dockerfile.scheduler
├── requirements.txt
├── pytest.ini
└── .env.example
```

---

## Logique cartographique

**Niveau affiché** : 80 quartiers administratifs (source : Paris Open Data)  
**Fond de carte** : tuiles vectorielles CartoDB Positron / Dark Matter  
**Choroplèthe** : échelle dynamique calculée sur les valeurs réelles de chaque indicateur

**Couches de points Silver** (togglables) :
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
|---|---|
| `v0.1.0` | Bronze + Silver initiaux (15 datasets) |
| `v0.2.0` | Bronze + Silver étendus (27 datasets, 4 indicateurs) + Gold + API + Frontend |
| `v0.3.0` | JWT auth, tqdm, corrections Bronze (max_records, fallback exports/json) |
| `v0.4.0` | Revenus INSEE Filosofi, types logement, points carte, scheduler, docs |
| `v0.5.0` | Kafka streaming (vélib/sanisettes/chantiers/anomalies), PBKDF2 sécurité, tests API, data catalog, perf report |
| `v0.6.0` | Architecture médaillon stricte : Silver = MinIO Parquet only → MongoDB déplacé en Gold (Phase 1 silver→mongo, Phase 2 mongo→postgres). Kafka étendu à 5 datasets + Voies 100k. Ingestion idempotente Bronze + Silver. PIP vectorisé geopandas.sjoin (20–50× plus rapide). |
| `v0.7.0` | Badges année par section (fallback API si NULL) + countdown temps réel Kafka dans le dashboard. Endpoint `/api/streaming/status`. |
| **`v0.8.0`** | **Score de fraîcheur détaillé par espace vert (`score_fraicheur_espaces_verts`) : palier végétation haute, ouverture 24h/24, bonus amplitude horaire — remplace le simple comptage dans `score_qualite_vie` aux 3 granularités (arrondissement/quartier/IRIS). Silver conserve `proportion_vegetation_haute`, `p_vegetation_h`, `horaires_*`.** |

---

## Conformité RNCP40875 Bloc 1

| Compétence | Implémentation |
|---|---|
| C1.1 — Collecter données multi-sources, multi-formats | 30 datasets : Parquet, CSV, CSV SDMXmeta, JSON, API REST paginées, export streaming |
| C1.2 — Stocker en format optimisé | Parquet columnar (Bronze MinIO) + documents MongoDB (Silver) + PostgreSQL/PostGIS (Gold) |
| C1.3 — Data Lake sécurisé avec catalog | MinIO bucket `bronze` + `docs/data_catalog.md` (30 datasets documentés, qualité, sources) |
| C1.4 — Architecture scalable et résiliente | Docker healthchecks + restart policies + retry tenacity + pipeline Medallion documenté |
| C2.1 — API interopérable et sécurisée | FastAPI + JWT python-jose (HS256) + PBKDF2 passwords + endpoints GeoJSON |
| C2.2 — Système de streaming | Apache Kafka KRaft — 5 topics urban.*, 100k enregistrements/5 min, consumer upsert MongoDB gold, rétention 24h |
| C2.3 — Transformation multi-sources | Silver : WGS84, PIP **vectorisé geopandas.sjoin** (20–50× vs ray-casting), enrichissement IRIS/quartier, 25 sources batch |
| C2.4 — Optimisation + mesure performance | `docs/perf_report.md` : geopandas vs ray-casting, ingestion idempotente, index GIST, logs `[PERF]` mesurés |
| C2.5 — API filtrable + fallback temporel | FastAPI `?annee=`, `?indicateur=`, `?type=` ; fallback automatique par section si données NULL pour l'année demandée |
| C2.6 — Dashboard cartographique interactif | MapLibre choroplèthe + 6 couches points + timeline + comparaison radar + géocodage BAN |
| C2.7 — Dataviz accessible | Chart.js donut/radar/ligne, ARIA labels, contraste couleurs |
| C2.8 — Indicateurs composites originaux | 4 scores (qualité vie / transports / loisirs / services) + taux_effort_achat |
