from fastapi.testclient import TestClient
from api.main import app

client = TestClient(app)
r = client.get("/api/geo/arrondissements?annee=2026&indicateur=score_global")
print("Status:", r.status_code)
print("Body:", r.text[:2000])
