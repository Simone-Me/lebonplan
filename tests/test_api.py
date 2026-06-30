"""
Tests d'intégration API — Urban Data Explorer
Couvre : health check, auth, endpoints GeoJSON et KPIs
"""

import os
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("API_JWT_SECRET", "test-secret-not-for-production")
os.environ.setdefault("API_AUTH_USER", "admin")
os.environ.setdefault(
    "API_AUTH_PASSWORD_HASH",
    "pbkdf2_sha256$260000$testsalt$"
    + __import__("hashlib").pbkdf2_hmac(
        "sha256", b"testpass", b"testsalt", 260000
    ).hex(),
)

from api.main import app

client = TestClient(app)


def _get_token() -> str:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "testpass"})
    assert r.status_code == 200, f"Login failed: {r.text}"
    return r.json()["access_token"]


# ─── Endpoints publics ────────────────────────────────────────────────────────

def test_root_returns_200():
    r = client.get("/")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_returns_ok():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ─── Auth ─────────────────────────────────────────────────────────────────────

def test_login_with_wrong_password_returns_401():
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_protected_endpoint_without_token_returns_401():
    r = client.get("/api/geo/arrondissements?annee=2024&indicateur=score_global")
    assert r.status_code == 401


def test_login_success():
    token = _get_token()
    assert isinstance(token, str)
    assert len(token) > 20

# ─── Endpoints protégés ───────────────────────────────────────────────────────

@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {_get_token()}"}


def test_kpis_arrondissement_structure(auth_headers):
    r = client.get("/api/kpis/1", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert "arrondissement" in data
    assert data["arrondissement"] == 1


def test_arrondissements_geojson_structure(auth_headers):
    r = client.get(
        "/api/geo/arrondissements?annee=2024&indicateur=score_global",
        headers=auth_headers,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["type"] == "FeatureCollection"
    assert "features" in data
    assert len(data["features"]) > 0


def test_geo_arrondissements_feature_has_properties(auth_headers):
    r = client.get(
        "/api/geo/arrondissements?annee=2024&indicateur=score_global",
        headers=auth_headers,
    )
    assert r.status_code == 200
    feature = r.json()["features"][0]
    assert "properties" in feature
    assert "geometry" in feature
    assert feature["type"] == "Feature"
