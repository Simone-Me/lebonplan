import "maplibre-gl/dist/maplibre-gl.css";
import {
  initMap,
  updateMapData,
  setMapTheme,
  getIndicatorScale,
  clearCompareHighlights,
  togglePointLayer,
  clearAreaSelection,
  setMapLevelSyncHandler,
  setSelectionGeoJSONCache,
  syncSelectionFromArea,
  flyToZone,
} from "./map.js";
import { openSidebar, closeSidebar } from "./sidebar.js";
import { initCompare } from "./compare.js";
import { initGeocode } from "./geocode.js";
import { fetchGeoJSON } from "./api.js";
import { initAuth } from "./auth.js";
import { getFavorites, removeFavorite, toggleFavorite } from "./favorites.js";

const INDICATEUR_LABELS = {
  score_global:           "Score global",
  score_qualite_vie:      "Qualité de vie",
  score_transports:       "Transports",
  score_loisirs:          "Loisirs",
  score_services:         "Services publics",
  nb_logements_sociaux:   "Logements sociaux",
};

let currentAnnee = 2026;
let currentIndicateur = "score_global";
let currentScale = { min: 0, max: 100 };
let currentLevel = "quartier";
let hasLoadedData = false;
let currentAreaSelection = null;
let geoJSONCache = { arrondissement: null, quartier: null, iris: null };

const LEVEL_ORDER = {
  arrondissement: 0,
  quartier: 1,
  iris: 2,
};

function initSidebarUI() {
  const root = document.documentElement;
  const sidebar = document.getElementById("sidebar");
  const resizer = document.getElementById("sidebar-resizer");
  const collapseBtn = document.getElementById("sidebar-collapse");
  const reopenBtn = document.getElementById("sidebar-reopen");
  const minWidth = 280;
  const maxWidth = 560;

  function setCollapsed(collapsed) {
    sidebar.classList.toggle("collapsed", collapsed);
    reopenBtn.classList.toggle("hidden", !collapsed);
    resizer.classList.toggle("hidden", collapsed);
  }

  collapseBtn?.addEventListener("click", () => setCollapsed(true));
  reopenBtn?.addEventListener("click", () => setCollapsed(false));

  resizer?.addEventListener("pointerdown", (event) => {
    if (sidebar.classList.contains("collapsed")) return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = sidebar.getBoundingClientRect().width;
    document.body.classList.add("is-resizing");
    resizer.setPointerCapture(event.pointerId);

    const onMove = (moveEvent) => {
      const delta = moveEvent.clientX - startX;
      const nextWidth = Math.max(minWidth, Math.min(maxWidth, startWidth + delta));
      root.style.setProperty("--sidebar-w", `${nextWidth}px`);
    };

    const onUp = () => {
      document.body.classList.remove("is-resizing");
      resizer.removeEventListener("pointermove", onMove);
      resizer.removeEventListener("pointerup", onUp);
      resizer.removeEventListener("pointercancel", onUp);
    };

    resizer.addEventListener("pointermove", onMove);
    resizer.addEventListener("pointerup", onUp);
    resizer.addEventListener("pointercancel", onUp);
  });
}

function formatLegendValue(value) {
  if (!Number.isFinite(value)) return "—";
  if (currentIndicateur === "nb_logements_sociaux") {
    return Math.round(value).toLocaleString("fr-FR");
  }
  return Number(value).toLocaleString("fr-FR", { maximumFractionDigits: 1 });
}

async function refreshMap(prefetchedGeojson = null) {
  try {
    const geojson = prefetchedGeojson || await fetchGeoJSON(currentAnnee, currentIndicateur, currentLevel);
    setSelectionGeoJSONCache(currentLevel, geojson);
    currentScale =
      updateMapData(geojson, currentIndicateur, INDICATEUR_LABELS[currentIndicateur], currentLevel) ??
      currentScale;
    updateLegend();
    if (currentAreaSelection?.level === currentLevel) {
      syncSelectionFromArea(currentAreaSelection);
      openSidebar(currentAreaSelection, currentAnnee, getIndicatorScale, currentAreaSelection.level);
    }
  } catch (e) {
    console.error("Erreur fetchGeoJSON", e);
  }
}

function setActiveLevelButton(level) {
  document.querySelectorAll(".level-btn").forEach((btn) => {
    const isActive = btn.dataset.level === level;
    btn.classList.toggle("active", isActive);
    btn.setAttribute("aria-pressed", isActive ? "true" : "false");
  });
}

async function setCurrentLevel(level, { force = false } = {}) {
  if (!force && level === currentLevel) return;
  currentLevel = level;
  currentAreaSelection = null;
  setActiveLevelButton(level);
  clearCompareHighlights();
  clearAreaSelection();
  closeSidebar();
  await refreshMap();
}

async function loadSelectionCaches() {
  const [arr, quartier, iris] = await Promise.all([
    fetchGeoJSON(currentAnnee, currentIndicateur, "arrondissement"),
    fetchGeoJSON(currentAnnee, currentIndicateur, "quartier"),
    fetchGeoJSON(currentAnnee, currentIndicateur, "iris"),
  ]);
  geoJSONCache.arrondissement = arr;
  geoJSONCache.quartier = quartier;
  geoJSONCache.iris = iris;
  setSelectionGeoJSONCache("arrondissement", arr);
  setSelectionGeoJSONCache("quartier", quartier);
  setSelectionGeoJSONCache("iris", iris);
  return { arrondissement: arr, quartier, iris };
}

async function reloadMapContext() {
  const geojsonByLevel = await loadSelectionCaches();
  await refreshMap(geojsonByLevel[currentLevel] || null);
}

async function hardRefreshCurrentLevel({ preserveSelection = true } = {}) {
  const selectionSnapshot = preserveSelection && currentAreaSelection
    ? { ...currentAreaSelection }
    : null;

  clearCompareHighlights();
  clearAreaSelection();

  if (!selectionSnapshot) {
    currentAreaSelection = null;
    closeSidebar();
  } else {
    currentAreaSelection = selectionSnapshot;
  }

  await reloadMapContext();
}

function updateLegend() {
  document.getElementById("legend-title").textContent =
    INDICATEUR_LABELS[currentIndicateur] || currentIndicateur;
  const legendNote = document.getElementById("legend-note");

  const gradient = document.getElementById("legend-gradient");
  gradient.style.background = "linear-gradient(to right, #f43f5e, #f59e0b, #10b981)";
  document.getElementById("legend-min").textContent = formatLegendValue(currentScale.min);
  document.getElementById("legend-max").textContent = formatLegendValue(currentScale.max);
  if (legendNote) legendNote.textContent = "";
}

/* ── Zone search by name ─────────────────────────────────────────── */
function initZoneSearch() {
  const input = document.getElementById("zone-search-input");
  const results = document.getElementById("zone-search-results");
  if (!input || !results) return;

  const getFieldId = (level) => {
    if (level === "arrondissement") return "arrondissement";
    if (level === "iris") return "iris_id";
    return "quartier_id";
  };

  const renderResults = () => {
    const q = input.value.trim().toLowerCase();
    if (q.length < 2) { results.classList.add("hidden"); return; }

    const cache = geoJSONCache[currentLevel];
    if (!cache?.features?.length) { results.classList.add("hidden"); return; }

    const matches = cache.features
      .filter((f) => (f.properties?.nom || "").toLowerCase().includes(q))
      .slice(0, 10);

    if (!matches.length) { results.classList.add("hidden"); return; }

    results.innerHTML = matches.map((f) => {
      const idField = getFieldId(currentLevel);
      const id = f.properties?.[idField];
      const nom = f.properties?.nom || "—";
      const arr = f.properties?.arrondissement;
      const sub = arr ? `${arr}e arr.` : "";
      return `<li role="option" data-id="${id}" data-nom="${nom}">
        <span style="flex:1">${nom}</span>
        ${sub ? `<span style="font-size:10px;color:var(--tx-3)">${sub}</span>` : ""}
      </li>`;
    }).join("");
    results.classList.remove("hidden");

    results.querySelectorAll("li").forEach((li) => {
      li.addEventListener("click", () => {
        const id = li.dataset.id;
        const nom = li.dataset.nom;
        const cache = geoJSONCache[currentLevel];
        const idField = getFieldId(currentLevel);
        const feature = cache?.features?.find(
          (f) => String(f.properties?.[idField]) === String(id)
        );
        if (feature) selectZoneFeature(feature);
        input.value = nom;
        results.classList.add("hidden");
      });
    });
  };

  input.addEventListener("input", renderResults);

  document.addEventListener("click", (e) => {
    if (!input.contains(e.target) && !results.contains(e.target)) {
      results.classList.add("hidden");
    }
  });

  window.addEventListener("lebonplan:level-changed", renderResults);
}

function selectZoneFeature(feature) {
  const props = feature.properties || {};
  const idField = currentLevel === "arrondissement"
    ? "arrondissement"
    : currentLevel === "iris"
      ? "iris_id"
      : "quartier_id";

  const areaSelection = {
    area_id: props[idField],
    nom: props.nom,
    arrondissement: props.arrondissement,
    quartier_code: props.quartier_code,
    iris_code: props.iris_code,
    level: currentLevel,
  };

  currentAreaSelection = areaSelection;
  syncSelectionFromArea(areaSelection);
  openSidebar(areaSelection, currentAnnee, getIndicatorScale, currentLevel);
  flyToZone(feature);
}

/* ── Favorites ───────────────────────────────────────────────────── */
function levelLabel(level) {
  if (level === "arrondissement") return "Arr.";
  if (level === "iris") return "IRIS";
  return "Quartier";
}

function renderFavorites() {
  const section = document.getElementById("favorites-section");
  const list = document.getElementById("favorites-list");
  if (!section || !list) return;

  const favs = getFavorites();
  if (!favs.length) { section.classList.add("hidden"); return; }

  section.classList.remove("hidden");
  list.innerHTML = favs.map((fav) => `
    <div class="favorite-item" data-area-id="${fav.area_id}" data-level="${fav.level}" role="button" tabindex="0"
         aria-label="Aller à ${fav.name}">
      <span class="fav-dot"></span>
      <span class="favorite-item-name">${fav.name}</span>
      <span class="favorite-item-meta">${levelLabel(fav.level)}</span>
      <button class="favorite-remove" data-area-id="${fav.area_id}" data-level="${fav.level}"
              aria-label="Retirer ${fav.name} des favoris" title="Retirer des favoris">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line>
        </svg>
      </button>
    </div>
  `).join("");

  list.querySelectorAll(".favorite-item").forEach((item) => {
    item.addEventListener("click", (e) => {
      if (e.target.closest(".favorite-remove")) return;
      const areaId = item.dataset.areaId;
      const level = item.dataset.level;
      if (level !== currentLevel) setCurrentLevel(level);
      openSidebar({ area_id: areaId, level }, currentAnnee, getIndicatorScale, level);
    });

    item.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") item.click();
    });
  });

  list.querySelectorAll(".favorite-remove").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      removeFavorite(btn.dataset.areaId, btn.dataset.level);
    });
  });
}

function initFavorites() {
  renderFavorites();
  window.addEventListener("lebonplan:favorites-changed", renderFavorites);

  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".btn-favorite");
    if (!btn) return;

    const areaId = btn.dataset.areaId;
    const level = btn.dataset.level;
    const name = btn.dataset.name;

    const nowFav = toggleFavorite({ area_id: areaId, level, name });
    btn.classList.toggle("is-fav", nowFav);
    btn.title = nowFav ? "Retirer des favoris" : "Ajouter aux favoris";
    btn.querySelector("svg").setAttribute("fill", nowFav ? "currentColor" : "none");
  });
}

/* ── App init ─────────────────────────────────────────────────────── */
function init() {
  document.documentElement.dataset.theme = "light";
  initSidebarUI();
  setActiveLevelButton(currentLevel);
  initMap((areaSelection) => {
    const selectionLevel = areaSelection?.level || currentLevel;
    currentAreaSelection = { ...areaSelection, level: selectionLevel };
    openSidebar(areaSelection, currentAnnee, getIndicatorScale, selectionLevel);
  }, { dark: false });
  setMapLevelSyncHandler((nextLevel) => {
    if (!nextLevel || !hasLoadedData) return;
    if ((LEVEL_ORDER[nextLevel] ?? -1) <= (LEVEL_ORDER[currentLevel] ?? -1)) return;
    setCurrentLevel(nextLevel);
  });

  const indicateurSel = document.getElementById("indicateur-select");
  indicateurSel.addEventListener("change", async () => {
    currentIndicateur = indicateurSel.value;
    await hardRefreshCurrentLevel({ preserveSelection: true });
  });

  document.querySelectorAll(".level-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const nextLevel = btn.dataset.level || "quartier";
      setCurrentLevel(nextLevel).then(() => {
        window.dispatchEvent(new CustomEvent("lebonplan:level-changed"));
      });
    });
  });

  const slider = document.getElementById("annee-slider");
  const anneeLabel = document.getElementById("annee-label");
  currentAnnee = +slider.value;
  anneeLabel.textContent = currentAnnee;
  slider.addEventListener("input", () => {
    currentAnnee = +slider.value;
    anneeLabel.textContent = currentAnnee;
    hardRefreshCurrentLevel({ preserveSelection: true });
  });

  initGeocode();
  initCompare(() => currentAnnee, clearCompareHighlights);
  initZoneSearch();
  initFavorites();

  /* Theme toggle — light ↔ dark */
  let isLight = true;
  const themeBtn  = document.getElementById("theme-toggle");
  const iconSun   = document.getElementById("theme-icon-sun");
  const iconMoon  = document.getElementById("theme-icon-moon");
  themeBtn.addEventListener("click", () => {
    isLight = !isLight;
    document.documentElement.dataset.theme = isLight ? "light" : "dark";
    setMapTheme(isLight ? false : true);
    iconSun.classList.toggle("hidden", !isLight);
    iconMoon.classList.toggle("hidden", isLight);
    themeBtn.title = isLight ? "Passer en mode sombre" : "Passer en mode clair";
  });

  document.querySelectorAll(".point-layer-toggle").forEach((cb) => {
    cb.addEventListener("change", () => togglePointLayer(cb.dataset.type, cb.checked));
  });

  initAuth(() => {
    if (hasLoadedData) return;
    hasLoadedData = true;
    let initAttempted = false;
    const waitForMap = setInterval(() => {
      if (initAttempted) return;
      initAttempted = true;
      try {
        Promise.resolve(loadSelectionCaches())
          .then((geojsonByLevel) => refreshMap(geojsonByLevel[currentLevel] || null))
          .then(() => clearInterval(waitForMap))
          .catch((error) => {
            console.error("Erreur loadSelectionCaches", error);
            clearInterval(waitForMap);
          });
      } catch (_) {
        clearInterval(waitForMap);
      }
    }, 500);
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
