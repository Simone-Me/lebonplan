@echo off
setlocal enabledelayedexpansion

set "PROJECT_DIR=%~dp0"
set "LOG_DIR=%PROJECT_DIR%logs"
set "LOG_FILE=%LOG_DIR%\pipeline_%date:~-4,4%%date:~-7,2%%date:~-10,2%_%time:~0,2%%time:~3,2%.log"
set "LOG_FILE=%LOG_FILE: =0%"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo [%date% %time%] === Demarrage pipeline LeBonPlan === >> "%LOG_FILE%"
echo [%date% %time%] === Demarrage pipeline LeBonPlan ===

:: Verifier que Docker Desktop est lance
docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo [%date% %time%] Docker n'est pas disponible. Tentative de demarrage... >> "%LOG_FILE%"
    echo [%date% %time%] Docker n'est pas disponible. Tentative de demarrage...
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    echo Attente demarrage Docker ^(60s^)...
    timeout /t 60 /nobreak >nul
    docker info >nul 2>&1
    if %errorlevel% neq 0 (
        echo [%date% %time%] ERREUR : Docker toujours indisponible. Abandon. >> "%LOG_FILE%"
        echo ERREUR : Docker toujours indisponible. Verifiez que Docker Desktop est installe.
        exit /b 1
    )
)

echo [%date% %time%] Docker OK >> "%LOG_FILE%"

:: Aller dans le dossier du projet
cd /d "%PROJECT_DIR%"

:: Demarrer les services d'infrastructure si pas encore actifs (sans recreer)
echo [%date% %time%] Demarrage des services infra... >> "%LOG_FILE%"
echo Demarrage des services infra ^(minio, mongodb, postgres, kafka^)...
docker compose up -d minio mongodb postgres kafka >> "%LOG_FILE%" 2>&1

:: Attendre que les healthchecks passent
echo Attente healthchecks ^(30s^)...
timeout /t 30 /nobreak >nul

:: Lancer le pipeline complet via le container scheduler avec RUN_ON_START
echo [%date% %time%] Lancement du pipeline Bronze -> Silver -> Gold... >> "%LOG_FILE%"
echo Lancement du pipeline Bronze -^> Silver -^> Gold...

docker compose run --rm ^
    -e RUN_ON_START=true ^
    -e PIPELINE_CRON="0 2 * * *" ^
    scheduler ^
    python -c "import pipeline.bronze_feeder as b; import pipeline.silver_transformer as s; import pipeline.gold_aggregator as g; b.run(); s.run(); g.run()" >> "%LOG_FILE%" 2>&1

if %errorlevel% equ 0 (
    echo [%date% %time%] Pipeline termine avec succes >> "%LOG_FILE%"
    echo Pipeline termine avec succes !
) else (
    echo [%date% %time%] ERREUR : Pipeline echoue ^(code %errorlevel%^) >> "%LOG_FILE%"
    echo ERREUR : Pipeline echoue. Consultez les logs : %LOG_FILE%
    exit /b 1
)

echo [%date% %time%] Log : %LOG_FILE% >> "%LOG_FILE%"
echo Log disponible : %LOG_FILE%
exit /b 0
