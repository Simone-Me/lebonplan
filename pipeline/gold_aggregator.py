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


# ─── Agrégations par indicateur ───────────────────────────────────────────────

def agg_qualite_vie(db) -> dict:
    """Retourne un dict {arr: {champs...}} pour l'indicateur qualité de vie."""
    espaces_verts = count_by_arr(db["silver_espaces_verts"])
    arbres        = count_by_arr(db["silver_arbres"])
    sanisettes    = count_by_arr(db["silver_sanisettes"])
    chantiers     = count_by_arr(db["silver_chantiers"])
    anomalies     = count_by_arr(db["silver_anomalies"])

    # Air quality : données Paris-wide (pas par arrondissement) → valeur unique propagée
    air_doc = db["silver_qualite_air"].find_one(sort=[("annee", -1)])
    score_air_no2  = float(air_doc.get("no2", 0)) if air_doc else 0.0
    score_air_pm25 = float(air_doc.get("pm2_5", 0)) if air_doc else 0.0

    # Fibre : % couverture par arrondissement (base_imb)
    pct_fibre = {}
    for arr in ARRONDISSEMENTS:
        total = db["silver_fibre_imb"].count_documents({"arrondissement": arr})
        couverts = db["silver_fibre_imb"].count_documents({"arrondissement": arr, "statut_immeuble": {"$regex": "Déployé", "$options": "i"}}) if total else 0
        pct_fibre[arr] = round(couverts / total * 100, 1) if total else None

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
            "pct_fibre":           pct_fibre.get(arr),
        }

    # Score composite : positif = espaces_verts + arbres + sanisettes + fibre
    #                   négatif = chantiers + anomalies
    pos_espaces = minmax_normalize({a: result[a]["nb_espaces_verts"] for a in ARRONDISSEMENTS})
    pos_arbres  = minmax_normalize({a: result[a]["nb_arbres"]        for a in ARRONDISSEMENTS})
    pos_san     = minmax_normalize({a: result[a]["nb_sanisettes"]    for a in ARRONDISSEMENTS})
    # Air : NO2 plus bas = meilleur → inversion
    inv_no2     = {a: 100 - min(score_air_no2, 100) for a in ARRONDISSEMENTS}
    neg_chant   = minmax_normalize({a: result[a]["nb_chantiers_actifs"] for a in ARRONDISSEMENTS})
    neg_anom    = minmax_normalize({a: result[a]["nb_anomalies"]        for a in ARRONDISSEMENTS})

    for arr in ARRONDISSEMENTS:
        pos = (pos_espaces[arr] + pos_arbres[arr] + pos_san[arr] + inv_no2[arr]) / 4
        neg = (neg_chant[arr] + neg_anom[arr]) / 2
        result[arr]["score_qualite_vie"] = round((pos * 0.7 + (100 - neg) * 0.3), 2)

    return result


def agg_transports(db) -> dict:
    gares  = count_by_arr(db["silver_gares"])
    velib  = count_by_arr(db["silver_velib"])
    flux   = {doc["_id"]: doc["flux"] for doc in db["silver_comptage_multimodal"].aggregate([
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

    s_gares = minmax_normalize({a: result[a]["nb_gares"]          for a in ARRONDISSEMENTS})
    s_velib = minmax_normalize({a: result[a]["nb_stations_velib"]  for a in ARRONDISSEMENTS})
    s_flux  = minmax_normalize({a: result[a]["flux_multimodal"]    for a in ARRONDISSEMENTS})

    for arr in ARRONDISSEMENTS:
        result[arr]["score_transports"] = round((s_gares[arr] + s_velib[arr] + s_flux[arr]) / 3, 2)

    return result


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

    s_evt  = minmax_normalize({a: result[a]["nb_evenements"] for a in ARRONDISSEMENTS})
    s_ter  = minmax_normalize({a: result[a]["nb_terrasses"]  for a in ARRONDISSEMENTS})
    s_cin  = minmax_normalize({a: result[a]["nb_cinemas"]    for a in ARRONDISSEMENTS})
    s_mus  = minmax_normalize({a: result[a]["nb_musees"]     for a in ARRONDISSEMENTS})

    for arr in ARRONDISSEMENTS:
        result[arr]["score_loisirs"] = round((s_evt[arr] + s_ter[arr] + s_cin[arr] + s_mus[arr]) / 4, 2)

    return result


def agg_services(db) -> dict:
    ecoles    = count_by_arr(db["silver_ecoles_elem"])
    colleges  = count_by_arr(db["silver_colleges"])
    biblio    = count_by_arr(db["silver_bibliotheques"])
    poste     = count_by_arr(db["silver_bureaux_poste"])
    ensup     = count_by_arr(db["silver_ensup"])

    result = {}
    for arr in ARRONDISSEMENTS:
        result[arr] = {
            "nb_ecoles":        safe_get(ecoles, arr),
            "nb_colleges":      safe_get(colleges, arr),
            "nb_bibliotheques": safe_get(biblio, arr),
            "nb_bureaux_poste": safe_get(poste, arr),
            "nb_ensup":         safe_get(ensup, arr),
        }

    s_eco = minmax_normalize({a: result[a]["nb_ecoles"]        for a in ARRONDISSEMENTS})
    s_col = minmax_normalize({a: result[a]["nb_colleges"]      for a in ARRONDISSEMENTS})
    s_bib = minmax_normalize({a: result[a]["nb_bibliotheques"] for a in ARRONDISSEMENTS})
    s_pos = minmax_normalize({a: result[a]["nb_bureaux_poste"] for a in ARRONDISSEMENTS})
    s_ens = minmax_normalize({a: result[a]["nb_ensup"]         for a in ARRONDISSEMENTS})

    for arr in ARRONDISSEMENTS:
        result[arr]["score_services"] = round((s_eco[arr] + s_col[arr] + s_bib[arr] + s_pos[arr] + s_ens[arr]) / 5, 2)

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

    # Fallback : année la plus récente disponible si l'année demandée n'existe pas
    if not prix_m2:
        latest = db["silver_dvf"].find_one(
            {"arrondissement": {"$in": ARRONDISSEMENTS}, "prix_m2_median": {"$exists": True}},
            sort=[("annee", -1)],
        )
        if latest:
            for doc in db["silver_dvf"].find(
                {"annee": latest["annee"], "arrondissement": {"$in": ARRONDISSEMENTS}}
            ):
                arr = doc.get("arrondissement")
                val = doc.get("prix_m2_median")
                if arr and val is not None:
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
    url = "https://parisdata.opendatasoft.com/api/explore/v2.1/catalog/datasets/arrondissements-paris-1arron/exports/geojson"
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
            score_services, nb_ecoles, nb_colleges, nb_bibliotheques,
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
            :score_services, :nb_ecoles, :nb_colleges, :nb_bibliotheques,
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
