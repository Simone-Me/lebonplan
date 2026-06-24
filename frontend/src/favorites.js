const KEY = "lebonplan_favorites";

export function getFavorites() {
  try { return JSON.parse(localStorage.getItem(KEY) || "[]"); }
  catch { return []; }
}

export function isFavorite(areaId, level) {
  return getFavorites().some(
    (f) => String(f.area_id) === String(areaId) && f.level === level
  );
}

export function addFavorite(fav) {
  const favs = getFavorites().filter(
    (f) => !(String(f.area_id) === String(fav.area_id) && f.level === fav.level)
  );
  favs.unshift(fav);
  localStorage.setItem(KEY, JSON.stringify(favs.slice(0, 25)));
  dispatch();
}

export function removeFavorite(areaId, level) {
  const favs = getFavorites().filter(
    (f) => !(String(f.area_id) === String(areaId) && f.level === level)
  );
  localStorage.setItem(KEY, JSON.stringify(favs));
  dispatch();
}

export function toggleFavorite(fav) {
  const was = isFavorite(fav.area_id, fav.level);
  if (was) removeFavorite(fav.area_id, fav.level);
  else addFavorite(fav);
  return !was;
}

function dispatch() {
  window.dispatchEvent(new CustomEvent("lebonplan:favorites-changed"));
}
