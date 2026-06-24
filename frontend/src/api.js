const BASE = "/api";
const TOKEN_KEY = "lebonplan_jwt";

export function getAccessToken() {
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setAccessToken(token) {
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearAccessToken() {
  window.localStorage.removeItem(TOKEN_KEY);
}

async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = getAccessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");

  const response = await fetch(`${BASE}${path}`, { ...options, headers });
  if (response.status === 401) {
    clearAccessToken();
    window.dispatchEvent(new CustomEvent("auth-required"));
  }
  return response;
}

export async function login(username, password) {
  const r = await apiFetch("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  if (!r.ok) throw new Error(`Login failed: ${r.status}`);
  return r.json();
}

export async function fetchCurrentUser() {
  const r = await apiFetch("/auth/me");
  if (!r.ok) throw new Error(`Auth check failed: ${r.status}`);
  return r.json();
}

function areaGeoPath(level) {
  if (level === "arrondissement") return "/geo/arrondissements";
  if (level === "iris") return "/geo/iris";
  return "/geo/quartiers";
}

function areaKpiPath(level, areaId) {
  if (level === "arrondissement") return `/kpis/${encodeURIComponent(areaId)}`;
  if (level === "iris") return `/kpis/iris/${encodeURIComponent(areaId)}`;
  return `/kpis/quartier/${encodeURIComponent(areaId)}`;
}

function areaTimelinePath(level, areaId) {
  if (level === "arrondissement") return `/timeline/${encodeURIComponent(areaId)}`;
  if (level === "iris") return `/timeline/iris/${encodeURIComponent(areaId)}`;
  return `/timeline/quartier/${encodeURIComponent(areaId)}`;
}

export async function fetchGeoJSON(annee, indicateur, level = "quartier") {
  const params = new URLSearchParams({ annee, indicateur });
  const r = await apiFetch(`${areaGeoPath(level)}?${params}`);
  if (!r.ok) throw new Error(`GeoJSON fetch failed: ${r.status}`);
  return r.json();
}

export async function fetchKPIs(areaId, annee, level = "quartier") {
  const params = annee ? `?annee=${annee}` : "";
  const r = await apiFetch(`${areaKpiPath(level, areaId)}${params}`);
  if (!r.ok) throw new Error(`KPIs fetch failed: ${r.status}`);
  return r.json();
}

export async function fetchTimeline(areaId, level = "quartier") {
  const r = await apiFetch(areaTimelinePath(level, areaId));
  if (!r.ok) throw new Error(`Timeline fetch failed: ${r.status}`);
  return r.json();
}

export async function fetchCompare(arr1, arr2, annee) {
  const params = new URLSearchParams({ arr1, arr2 });
  if (annee) params.set("annee", annee);
  const r = await apiFetch(`/compare?${params}`);
  if (!r.ok) throw new Error(`Compare fetch failed: ${r.status}`);
  return r.json();
}

export async function fetchStreamingStatus() {
  const r = await apiFetch("/streaming/status");
  if (!r.ok) return null;
  return r.json();
}

export async function fetchPointsGeoJSON(type) {
  const r = await apiFetch(`/geo/points?type=${encodeURIComponent(type)}`);
  if (!r.ok) throw new Error(`Points fetch failed: ${r.status}`);
  return r.json();
}

/**
 * Géocodage via l'API Base Adresse Nationale (data.gouv.fr)
 * Filtre sur Paris (code postal 75xxx)
 */
export async function geocodeAddress(query) {
  if (!query || query.length < 3) return [];
  const params = new URLSearchParams({ q: query, citycode: "75056", limit: 5 });
  const r = await fetch(`https://api-adresse.data.gouv.fr/search/?${params}`);
  if (!r.ok) return [];
  const data = await r.json();
  return data.features || [];
}
