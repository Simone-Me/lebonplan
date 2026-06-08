"""
Gold Layer Aggregator — Urban Data Explorer Paris
Silver (MongoDB) → Gold (PostgreSQL/PostGIS)

Pour chaque arrondissement (1-20) :
  1. COUNT/AVG des entités Silver par collection
  2. Normalisation min-max → score 0-100
  3. Score composite par indicateur (moyenne pondérée)
  4. Score global = moyenne des 4 indicateurs composites
  5. Upsert dans gold.arrondissement_kpis
"""

import logging
import math
import requests
from pymongo import MongoClient
from sqlalchemy import create_engine, text

from config import MONGO_URI, MONGO_DB, POSTGRES_DSN, BAN_API

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("gold_aggregator")

ARRONDISSEMENTS = list(range(1, 21))

# ─── Connexions ───────────────────────────────────────────────────────────────

def get_mongo():
    client = MongoClient(MONGO_URI)
    return client[MONGO_DB]


def get_engine():
    return create_engine(POSTGRES_DSN)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def minmax_normalize(values: dict) -> dict:
    """Normalise un dict {arrondissement: valeur} → scores 0-100."""
    vals = [v for v in values.values() if v is not None and not math.isnan(v)]
    if not vals:
        return {k: 0.0 for k in values}
    vmin, vmax = min(vals), max(vals)
    if vmax == vmin:
        return {k: 50.0 for k in values}
    return {
        k: round((v - vmin) / (vmax - vmin) * 100, 2) if v is not None else 0.0
        for k, v in values.items()
    }


def count_by_arr(coll, arr_field: str = "arrondissement") -> dict:
    """Compte les documents par arrondissement dans une collection MongoDB."""
    pipeline = [
        {"$match": {arr_field: {"$in": ARRONDISSEMENTS}}},
        {"$group": {"_id": f"${arr_field}", "count": {"$sum": 1}}},
    ]
    return {doc["_id"]: doc["count"] for doc in coll.aggregate(pipeline)}


def avg_by_arr(coll, value_field: str, arr_field: str = "arrondissement") -> dict:
    """Moyenne d'un champ numérique par arrondissement."""
    pipeline = [
        {"$match": {arr_field: {"$in": ARRONDISSEMENTS}, value_field: {"$exists": True, "$ne": None}}},
        {"$group": {"_id": f"${arr_field}", "avg": {"$avg": f"${value_field}"}}},
    ]
    return {doc["_id"]: doc["avg"] for doc in coll.aggregate(pipeline)}


def safe_get(d: dict, arr: int, default=0):
    v = d.get(arr, default)
    return v if v is not None and not (isinstance(v, float) and math.isnan(v)) else default


def weighted_avg(scored_pairs: list, skip_zero_collections: bool = True) -> dict:
    """
    Moyenne pondérée de sous-scores normalisés par arrondissement.

    scored_pairs : liste de (scores_dict {arr: float}, poids int)
    skip_zero_collections : si True, ignore les collections où tous les arrondissements = 0
      (évite de pénaliser un arrondissement quand toute une source est vide)
    """
    result = {arr: {"num": 0.0, "den": 0} for arr in ARRONDISSEMENTS}
    for scores, poids in scored_pairs:
        if skip_zero_collections and all(v == 0 for v in scores.values()):
            continue
        for arr in ARRONDISSEMENTS:
            result[arr]["num"] += scores.get(arr, 0) * poids
            result[arr]["den"] += poids
    return {
        arr: round(result[arr]["num"] / result[arr]["den"], 2) if result[arr]["den"] > 0 else 0.0
        for arr in ARRONDISSEMENTS
    }


def invert(scores: dict) -> dict:
    """Inverse un dict de scores normalisés 0-100 → (100 - score)."""
    return {arr: round(100 - v, 2) for arr, v in scores.items()}


# ─── Agrégations par indicateur ───────────────────────────────────────────────

# Scoring Indicateur 1 — Qualité de vie
#
# Source                  | Collection            | Champ      | Poids | Signe
# ----------------------- | --------------------- | ---------- | ----- | ------
# Espaces verts / îlots   | silver_espaces_verts  | COUNT      |   2   | +
# Arbres                  | silver_arbres         | COUNT      |   1   | +
# Sanisettes              | silver_sanisettes     | COUNT      |   1   | +
# Fibre (% déployé)       | silver_fibre_imb      | % déployé  |   1   | +
# Chantiers               | silver_chantiers      | COUNT      |   1   | - (inversé)
# Anomalies signalées     | silver_anomalies      | COUNT      |   1   | - (inversé)
# Qualité air NO2         | silver_qualite_air    | no2 (avg)  |   1   | - (inversé)

def agg_qualite_vie(db) -> dict:
    espaces_verts = count_by_arr(db["silver_espaces_verts"])
    arbres        = count_by_arr(db["silver_arbres"])
    sanisettes    = count_by_arr(db["silver_sanisettes"])
    chantiers     = count_by_arr(db["silver_chantiers"])
    anomalies     = count_by_arr(db["silver_anomalies"])

    # Air : données Paris-wide → valeur propagée à tous les arrondissements
    air_doc        = db["silver_qualite_air"].find_one(sort=[("annee", -1)])
    score_air_no2  = float(air_doc.get("no2", 0))   if air_doc else 0.0
    score_air_pm25 = float(air_doc.get("pm2_5", 0)) if air_doc else 0.0
    # NO2 normalisé sur 100 µg/m³ (seuil OMS annuel) puis inversé
    no2_norm = {a: min(score_air_no2 / 100.0 * 100, 100) for a in ARRONDISSEMENTS}

    # Fibre : % immeubles avec statut "Déployé" par arrondissement
    pct_fibre = {}
    for arr in ARRONDISSEMENTS:
        total    = db["silver_fibre_imb"].count_documents({"arrondissement": arr})
        deployes = db["silver_fibre_imb"].count_documents(
            {"arrondissement": arr, "statut_immeuble": {"$regex": "Déployé", "$options": "i"}}
        ) if total else 0
        pct_fibre[arr] = round(deployes / total * 100, 1) if total else 0

    result = {}
    for arr in ARRONDISSEMENTS:
        result[arr] = {
            "nb_espaces_verts":    safe_get(espaces_verts, arr),
            "nb_arbres":           safe_get(arbres, arr),
            "nb_sanisettes":       safe_get(sanisettes, arr),
            "nb_chantiers_actifs": safe_get(chantiers, arr),
            "nb_anomalies":        safe_get(anomalies, arr),
            "score_air_no2":       score_air_no2,
            "score_air_pm25":      score_air_pm25,
            "pct_fibre":           pct_fibre.get(arr, 0),
        }

    s_ev  = minmax_normalize({a: result[a]["nb_espaces_verts"]    for a in ARRONDISSEMENTS})
    s_arb = minmax_normalize({a: result[a]["nb_arbres"]           for a in ARRONDISSEMENTS})
    s_san = minmax_normalize({a: result[a]["nb_sanisettes"]       for a in ARRONDISSEMENTS})
    s_fib = minmax_normalize({a: result[a]["pct_fibre"]           for a in ARRONDISSEMENTS})
    s_cha = invert(minmax_normalize({a: result[a]["nb_chantiers_actifs"] for a in ARRONDISSEMENTS}))
    s_ano = invert(minmax_normalize({a: result[a]["nb_anomalies"]        for a in ARRONDISSEMENTS}))
    s_air = invert(no2_norm)  # NO2 bas = meilleur

    scores = weighted_avg([
        (s_ev,  2),
        (s_arb, 1),
        (s_san, 1),
        (s_fib, 1),
        (s_cha, 1),
        (s_ano, 1),
        (s_air, 1),
    ])
    for arr in ARRONDISSEMENTS:
        result[arr]["score_qualite_vie"] = scores[arr]

    return result


# Scoring Indicateur 2 — Transports
#
# Source              | Collection                  | Champ        | Poids | Signe
# ------------------- | --------------------------- | ------------ | ----- | -----
# Gares               | silver_gares                | COUNT        |   2   | +
# Vélib stations      | silver_velib                | COUNT        |   2   | +
# Flux multimodal     | silver_comptage_multimodal  | SUM(q)       |   1   | +

def agg_transports(db) -> dict:
    gares = count_by_arr(db["silver_gares"])
    velib = count_by_arr(db["silver_velib"])
    flux  = {doc["_id"]: doc["flux"] for doc in db["silver_comptage_multimodal"].aggregate([
        {"$match": {"arrondissement": {"$in": ARRONDISSEMENTS}}},
        {"$group": {"_id": "$arrondissement", "flux": {"$sum": {"$ifNull": ["$q", 1]}}}},
    ])}

    result = {}
    for arr in ARRONDISSEMENTS:
        result[arr] = {
            "nb_gares":          safe_get(gares, arr),
            "nb_stations_velib": safe_get(velib, arr),
            "flux_multimodal":   safe_get(flux, arr),
        }

    s_gar  = minmax_normalize({a: result[a]["nb_gares"]           for a in ARRONDISSEMENTS})
    s_vel  = minmax_normalize({a: result[a]["nb_stations_velib"]  for a in ARRONDISSEMENTS})
    s_flux = minmax_normalize({a: result[a]["flux_multimodal"]    for a in ARRONDISSEMENTS})

    scores = weighted_avg([(s_gar, 2), (s_vel, 2), (s_flux, 1)])
    for arr in ARRONDISSEMENTS:
        result[arr]["score_transports"] = scores[arr]

    return result


# Scoring Indicateur 3 — Loisirs
#
# Source          | Collection          | Champ  | Poids | Signe
# --------------- | ------------------- | ------ | ----- | -----
# Événements      | silver_evenements   | COUNT  |   2   | +
# Terrasses       | silver_terrasses    | COUNT  |   1   | +
# Cinémas         | silver_cinemas      | COUNT  |   1   | +
# Musées          | silver_musees       | COUNT  |   1   | +

def agg_loisirs(db) -> dict:
    evenements = count_by_arr(db["silver_evenements"])
    terrasses  = count_by_arr(db["silver_terrasses"])
    cinemas    = count_by_arr(db["silver_cinemas"])
    musees     = count_by_arr(db["silver_musees"])

    result = {}
    for arr in ARRONDISSEMENTS:
        result[arr] = {
            "nb_evenements": safe_get(evenements, arr),
            "nb_terrasses":  safe_get(terrasses, arr),
            "nb_cinemas":    safe_get(cinemas, arr),
            "nb_musees":     safe_get(musees, arr),
        }

    s_evt = minmax_normalize({a: result[a]["nb_evenements"] for a in ARRONDISSEMENTS})
    s_ter = minmax_normalize({a: result[a]["nb_terrasses"]  for a in ARRONDISSEMENTS})
    s_cin = minmax_normalize({a: result[a]["nb_cinemas"]    for a in ARRONDISSEMENTS})
    s_mus = minmax_normalize({a: result[a]["nb_musees"]     for a in ARRONDISSEMENTS})

    scores = weighted_avg([(s_evt, 2), (s_ter, 1), (s_cin, 1), (s_mus, 1)])
    for arr in ARRONDISSEMENTS:
        result[arr]["score_loisirs"] = scores[arr]

    return result


# Scoring Indicateur 4 — Services publics
#
# Source                  | Collection              | Champ  | Poids | Signe
# ----------------------- | ----------------------- | ------ | ----- | -----
# Écoles élémentaires     | silver_ecoles_elem      | COUNT  |   2   | +
# Maternelles             | silver_maternelles      | COUNT  |   2   | +
# Collèges                | silver_colleges         | COUNT  |   1   | +
# Bibliothèques           | silver_bibliotheques    | COUNT  |   2   | +
# Bureaux de poste        | silver_bureaux_poste    | COUNT  |   1   | +
# Enseignement supérieur  | silver_ensup            | COUNT  |   1   | +

def agg_services(db) -> dict:
    ecoles      = count_by_arr(db["silver_ecoles_elem"])
    maternelles = count_by_arr(db["silver_maternelles"])
    colleges    = count_by_arr(db["silver_colleges"])
    biblio      = count_by_arr(db["silver_bibliotheques"])
    poste       = count_by_arr(db["silver_bureaux_poste"])
    ensup       = count_by_arr(db["silver_ensup"])

    result = {}
    for arr in ARRONDISSEMENTS:
        result[arr] = {
            "nb_ecoles":        safe_get(ecoles, arr),
            "nb_maternelles":   safe_get(maternelles, arr),
            "nb_colleges":      safe_get(colleges, arr),
            "nb_bibliotheques": safe_get(biblio, arr),
            "nb_bureaux_poste": safe_get(poste, arr),
            "nb_ensup":         safe_get(ensup, arr),
        }

    s_eco = minmax_normalize({a: result[a]["nb_ecoles"]        for a in ARRONDISSEMENTS})
    s_mat = minmax_normalize({a: result[a]["nb_maternelles"]   for a in ARRONDISSEMENTS})
    s_col = minmax_normalize({a: result[a]["nb_colleges"]      for a in ARRONDISSEMENTS})
    s_bib = minmax_normalize({a: result[a]["nb_bibliotheques"] for a in ARRONDISSEMENTS})
    s_pos = minmax_normalize({a: result[a]["nb_bureaux_poste"] for a in ARRONDISSEMENTS})
    s_ens = minmax_normalize({a: result[a]["nb_ensup"]         for a in ARRONDISSEMENTS})

    scores = weighted_avg([(s_eco, 2), (s_mat, 2), (s_col, 1), (s_bib, 2), (s_pos, 1), (s_ens, 1)])
    for arr in ARRONDISSEMENTS:
        result[arr]["score_services"] = scores[arr]

    return result


def agg_immobilier(db, annee: int) -> dict:
    """Prix m² médian (DVF+) + logements sociaux par arrondissement."""
    ls = count_by_arr(db["silver_logements_sociaux"])

    # Prix m² médian depuis silver_dvf pour l'année demandée
    prix_m2 = {}
    for doc in db["silver_dvf"].find({"annee": annee, "arrondissement": {"$in": ARRONDISSEMENTS}}):
        arr = doc.get("arrondissement")
        val = doc.get("prix_m2_median")
        if arr and val is not None:
            prix_m2[arr] = float(val)

    # Fallback : toutes les lignes disponibles (pas de filtre année)
    if not prix_m2:
        for doc in db["silver_dvf"].find(
            {"arrondissement": {"$in": ARRONDISSEMENTS}, "prix_m2_median": {"$exists": True}}
        ):
            arr = doc.get("arrondissement")
            val = doc.get("prix_m2_median")
            if arr and val is not None and arr not in prix_m2:
                prix_m2[arr] = float(val)

    result = {}
    for arr in ARRONDISSEMENTS:
        result[arr] = {
            "nb_logements_sociaux":  safe_get(ls, arr),
            "pct_logements_sociaux": None,
            "prix_m2_median":        prix_m2.get(arr),
        }
    return result


# ─── Géométries arrondissements ───────────────────────────────────────────────

def load_arrondissements_geo(engine):
    """Télécharge et insère le GeoJSON des arrondissements depuis parisdata."""
    url = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/arrondissements/exports/geojson?lang=fr&timezone=Europe%2FParis"
    log.info(f"  Téléchargement GeoJSON arrondissements...")
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        geojson = r.json()
    except Exception as e:
        log.warning(f"  Impossible de récupérer le GeoJSON arrondissements : {e}")
        return

    rows = []
    for feature in geojson.get("features", []):
        props = feature.get("properties", {})
        arr_num = None
        for k in ["c_ar", "c_arinsee", "l_ar", "arrondissement"]:
            if k in props:
                try:
                    arr_num = int(str(props[k]).replace("e", "").replace("er", "").strip())
                except Exception:
                    pass
                if arr_num:
                    break
        if not arr_num or not (1 <= arr_num <= 20):
            continue
        import json as _json
        geom_str = _json.dumps(feature["geometry"])
        nom = props.get("l_ar", f"{arr_num}e arrondissement")
        rows.append((arr_num, nom, geom_str))

    if not rows:
        log.warning("  Aucune géométrie extraite du GeoJSON")
        return

    upsert_sql = text("""
        INSERT INTO gold.arrondissements_geo (arrondissement, nom, geom)
        VALUES (:arr, :nom, ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326))
        ON CONFLICT (arrondissement) DO UPDATE
          SET nom  = EXCLUDED.nom,
              geom = EXCLUDED.geom
    """)
    with engine.connect() as conn:
        for arr_num, nom, geom_str in rows:
            conn.execute(upsert_sql, {"arr": arr_num, "nom": nom, "geom": geom_str})
        conn.commit()
    log.info(f"  ✓ {len(rows)} géométries arrondissements upsertées")


# ─── Upsert Gold ──────────────────────────────────────────────────────────────

def upsert_kpis(engine, kpis_by_arr: dict, annee: int):
    sql = text("""
        INSERT INTO gold.arrondissement_kpis (
            arrondissement, annee,
            prix_m2_median, pct_logements_sociaux, nb_logements_sociaux,
            score_qualite_vie, nb_espaces_verts, nb_arbres,
            score_air_no2, score_air_pm25, pct_fibre,
            nb_sanisettes, nb_chantiers_actifs, nb_anomalies,
            score_transports, nb_gares, nb_stations_velib, flux_multimodal,
            score_loisirs, nb_evenements, nb_cinemas, nb_terrasses, nb_musees,
            score_services, nb_ecoles, nb_maternelles, nb_colleges, nb_bibliotheques,
            nb_bureaux_poste, nb_ensup,
            score_global
        ) VALUES (
            :arrondissement, :annee,
            :prix_m2_median, :pct_logements_sociaux, :nb_logements_sociaux,
            :score_qualite_vie, :nb_espaces_verts, :nb_arbres,
            :score_air_no2, :score_air_pm25, :pct_fibre,
            :nb_sanisettes, :nb_chantiers_actifs, :nb_anomalies,
            :score_transports, :nb_gares, :nb_stations_velib, :flux_multimodal,
            :score_loisirs, :nb_evenements, :nb_cinemas, :nb_terrasses, :nb_musees,
            :score_services, :nb_ecoles, :nb_maternelles, :nb_colleges, :nb_bibliotheques,
            :nb_bureaux_poste, :nb_ensup,
            :score_global
        )
        ON CONFLICT (arrondissement, annee) DO UPDATE SET
            prix_m2_median        = EXCLUDED.prix_m2_median,
            pct_logements_sociaux = EXCLUDED.pct_logements_sociaux,
            nb_logements_sociaux  = EXCLUDED.nb_logements_sociaux,
            score_qualite_vie     = EXCLUDED.score_qualite_vie,
            nb_espaces_verts      = EXCLUDED.nb_espaces_verts,
            nb_arbres             = EXCLUDED.nb_arbres,
            score_air_no2         = EXCLUDED.score_air_no2,
            score_air_pm25        = EXCLUDED.score_air_pm25,
            pct_fibre             = EXCLUDED.pct_fibre,
            nb_sanisettes         = EXCLUDED.nb_sanisettes,
            nb_chantiers_actifs   = EXCLUDED.nb_chantiers_actifs,
            nb_anomalies          = EXCLUDED.nb_anomalies,
            score_transports      = EXCLUDED.score_transports,
            nb_gares              = EXCLUDED.nb_gares,
            nb_stations_velib     = EXCLUDED.nb_stations_velib,
            flux_multimodal       = EXCLUDED.flux_multimodal,
            score_loisirs         = EXCLUDED.score_loisirs,
            nb_evenements         = EXCLUDED.nb_evenements,
            nb_cinemas            = EXCLUDED.nb_cinemas,
            nb_terrasses          = EXCLUDED.nb_terrasses,
            nb_musees             = EXCLUDED.nb_musees,
            score_services        = EXCLUDED.score_services,
            nb_ecoles             = EXCLUDED.nb_ecoles,
            nb_maternelles        = EXCLUDED.nb_maternelles,
            nb_colleges           = EXCLUDED.nb_colleges,
            nb_bibliotheques      = EXCLUDED.nb_bibliotheques,
            nb_bureaux_poste      = EXCLUDED.nb_bureaux_poste,
            nb_ensup              = EXCLUDED.nb_ensup,
            score_global          = EXCLUDED.score_global
    """)

    with engine.connect() as conn:
        for arr, kpis in kpis_by_arr.items():
            conn.execute(sql, {"arrondissement": arr, "annee": annee, **kpis})
        conn.commit()
    log.info(f"  ✓ {len(kpis_by_arr)} arrondissements upsertés (annee={annee})")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run(annee: int | None = None):
    from datetime import date
    if annee is None:
        annee = date.today().year

    log.info("=" * 60)
    log.info(f"Gold Aggregator — annee={annee}")
    log.info("=" * 60)

    db = get_mongo()
    engine = get_engine()

    log.info("\n[1/6] Géométries arrondissements")
    load_arrondissements_geo(engine)

    log.info("\n[2/6] Agrégation qualité de vie")
    qv = agg_qualite_vie(db)

    log.info("\n[3/6] Agrégation transports")
    tr = agg_transports(db)

    log.info("\n[4/6] Agrégation loisirs")
    lo = agg_loisirs(db)

    log.info("\n[5/6] Agrégation services publics")
    sv = agg_services(db)

    log.info("\n[6/6] Agrégation immobilier (DVF+ prix m²)")
    im = agg_immobilier(db, annee)

    # Fusion + score global
    kpis_by_arr = {}
    for arr in ARRONDISSEMENTS:
        row = {}
        row.update(qv.get(arr, {}))
        row.update(tr.get(arr, {}))
        row.update(lo.get(arr, {}))
        row.update(sv.get(arr, {}))
        row.update(im.get(arr, {}))

        s_qv = row.get("score_qualite_vie", 0) or 0
        s_tr = row.get("score_transports", 0)  or 0
        s_lo = row.get("score_loisirs", 0)     or 0
        s_sv = row.get("score_services", 0)    or 0
        row["score_global"] = round((s_qv + s_tr + s_lo + s_sv) / 4, 2)

        kpis_by_arr[arr] = row

    upsert_kpis(engine, kpis_by_arr, annee)
    engine.dispose()

    log.info("\n" + "=" * 60)
    log.info("RAPPORT GOLD")
    log.info("=" * 60)
    for arr in ARRONDISSEMENTS:
        k = kpis_by_arr[arr]
        log.info(
            f"  {arr:>2}e  QV={k.get('score_qualite_vie',0):5.1f}  "
            f"TR={k.get('score_transports',0):5.1f}  "
            f"LO={k.get('score_loisirs',0):5.1f}  "
            f"SV={k.get('score_services',0):5.1f}  "
            f"→ GLOBAL={k.get('score_global',0):5.1f}"
        )
    log.info("=" * 60)


if __name__ == "__main__":
    import sys
    annee_arg = int(sys.argv[1]) if len(sys.argv) > 1 else None
    run(annee_arg)
