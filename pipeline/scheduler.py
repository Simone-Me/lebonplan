"""
Scheduler automatique du pipeline LeBonPlan.

Exécute le pipeline complet (Bronze → Silver → Gold) selon un planning
configurable via la variable d'environnement PIPELINE_CRON
(format cron standard, défaut : chaque nuit à 2 h).

Usage :
    python pipeline/scheduler.py
    PIPELINE_CRON="0 3 * * 0" python pipeline/scheduler.py  # dimanche 3 h
"""

import logging
import os
import sys
import time
from pathlib import Path
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

# Permet l'execution directe via `python pipeline/scheduler.py`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pipeline.bronze_feeder   as bronze
import pipeline.silver_transformer as silver
import pipeline.gold_aggregator as gold

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
log = logging.getLogger("scheduler")

DEFAULT_CRON = os.getenv("PIPELINE_CRON", "0 2 * * *")  # chaque nuit à 2 h


def run_pipeline():
    start = datetime.now()
    log.info("=== Démarrage pipeline automatique ===")
    try:
        log.info("Étape 1/3 — Bronze (ingestion sources)")
        bronze.run()
        log.info("Étape 2/3 — Silver (transformation MongoDB)")
        silver.run()
        log.info("Étape 3/3 — Gold (agrégation PostgreSQL)")
        gold.run()
        elapsed = (datetime.now() - start).total_seconds()
        log.info(f"=== Pipeline terminé en {elapsed:.1f} s ===")
    except Exception as exc:
        elapsed = (datetime.now() - start).total_seconds()
        log.error(f"=== Pipeline ÉCHOUÉ après {elapsed:.1f} s : {exc} ===", exc_info=True)
        raise


def main():
    cron_expr = DEFAULT_CRON
    log.info(f"Scheduler démarré — cron : '{cron_expr}'")

    # Exécution immédiate optionnelle au démarrage
    if os.getenv("RUN_ON_START", "false").lower() == "true":
        log.info("RUN_ON_START=true — exécution immédiate du pipeline")
        try:
            run_pipeline()
        except Exception:
            log.warning("Exécution initiale échouée, le scheduler continue quand même")

    scheduler = BlockingScheduler(timezone="Europe/Paris")

    parts = cron_expr.strip().split()
    if len(parts) == 5:
        minute, hour, day, month, day_of_week = parts
        trigger = CronTrigger(
            minute=minute, hour=hour,
            day=day, month=month, day_of_week=day_of_week,
            timezone="Europe/Paris",
        )
    else:
        log.warning(f"PIPELINE_CRON invalide '{cron_expr}' → fallback 2 h chaque nuit")
        trigger = CronTrigger(hour=2, minute=0, timezone="Europe/Paris")

    scheduler.add_job(run_pipeline, trigger=trigger, id="pipeline", misfire_grace_time=3600)

    next_run = scheduler.get_jobs()[0].next_run_time
    log.info(f"Prochaine exécution planifiée : {next_run}")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler arrêté proprement.")


if __name__ == "__main__":
    main()
