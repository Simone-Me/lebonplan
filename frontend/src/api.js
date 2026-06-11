const BASE = "/api";

export async function fetchGeoJSON(annee, indicateur) {
  const params = new URLSearchParams({ annee, indicateur });
  const r = await fetch(`${BASE}/geo/quartiers?${params}`);
  if (!r.ok) throw new Error(`GeoJSON fetch failed: ${r.status}`);
  return r.json();
}

export async function fetchKPIs(quartierId, annee) {
  const params = annee ? `?annee=${annee}` : "";
  const r = await fetch(`${BASE}/kpis/quartier/${encodeURIComponent(quartierId)}${params}`);
  if (!r.ok) throw new Error(`KPIs fetch failed: ${r.status}`);
  return r.json();
}

export async function fetchTimeline(quartierId) {
  const r = await fetch(`${BASE}/timeline/quartier/${encodeURIComponent(quartierId)}`);
  if (!r.ok) throw new Error(`Timeline fetch failed: ${r.status}`);
  return r.json();
}

export async function fetchCompare(arr1, arr2, annee) {
  const params = new URLSearchParams({ arr1, arr2 });
  if (annee) params.set("annee", annee);
  const r = await fetch(`${BASE}/compare?${params}`);
  if (!r.ok) throw new Error(`Compare fetch failed: ${r.status}`);
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
