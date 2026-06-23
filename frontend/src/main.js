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
} from "./map.js";
import { openSidebar, closeSidebar } from "./sidebar.js";
import { initCompare } from "./compare.js";
import { initGeocode } from "./geocode.js";
import { fetchGeoJSON } from "./api.js";
import { initAuth } from "./auth.js";

const INDICATEUR_LABELS = {
  score_global:           "Score global",
  score_qualite_vie:      "Qualité de vie",
  score_transports:       "Transports",
  score_loisirs:          "Loisirs",
  score_services:         "Services publics",
  prix_m2_median:         "Prix m² médian (€)",
  nb_logements_sociaux:   "Logements sociaux",
};

let currentAnnee = 2026;
let currentIndicateur = "score_global";
let currentScale = { min: 0, max: 100 };
let currentLevel = "quartier";
let hasLoadedData = false;

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
  if (currentIndicateur === "prix_m2_median") {
    return `${Math.round(value).toLocaleString("fr-FR")} €`;
  }
  if (currentIndicateur === "nb_logements_sociaux") {
    return Math.round(value).toLocaleString("fr-FR");
  }
  return Number(value).toLocaleString("fr-FR", { maximumFractionDigits: 1 });
}

async function refreshMap() {
  try {
    const geojson = await fetchGeoJSON(currentAnnee, currentIndicateur, currentLevel);
    currentScale =
      updateMapData(geojson, currentIndicateur, INDICATEUR_LABELS[currentIndicateur], currentLevel) ??
      currentScale;
    updateLegend();
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
  setActiveLevelButton(level);
  clearCompareHighlights();
  clearAreaSelection();
  closeSidebar();
  await refreshMap();
}

function updateLegend() {
  document.getElementById("legend-title").textContent =
    INDICATEUR_LABELS[currentIndicateur] || currentIndicateur;
  const legendNote = document.getElementById("legend-note");

  const gradient = document.getElementById("legend-gradient");
  gradient.style.background = currentIndicateur === "prix_m2_median"
    ? "linear-gradient(to right, #22c55e, #f59e0b, #ef4444)"
    : "linear-gradient(to right, #ef4444, #f59e0b, #22c55e)";
  document.getElementById("legend-min").textContent = formatLegendValue(currentScale.min);
  document.getElementById("legend-max").textContent = formatLegendValue(currentScale.max);
  if (legendNote) {
    legendNote.textContent = currentIndicateur === "prix_m2_median"
      ? "Valeur DVF héritée de l'arrondissement avec couleurs inversées : plus cher = rouge."
      : "";
  }
}

function init() {
  document.documentElement.dataset.theme = "light";
  initSidebarUI();
  setActiveLevelButton(currentLevel);
  initMap((areaSelection) => {
    const selectionLevel = areaSelection?.level || currentLevel;
    openSidebar(areaSelection, currentAnnee, getIndicatorScale, selectionLevel);
  });
  setMapLevelSyncHandler((nextLevel) => {
    if (!nextLevel || nextLevel === currentLevel || !hasLoadedData) return;
    setCurrentLevel(nextLevel);
  });

  const indicateurSel = document.getElementById("indicateur-select");
  indicateurSel.addEventListener("change", () => {
    currentIndicateur = indicateurSel.value;
    refreshMap();
  });

  document.querySelectorAll(".level-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const nextLevel = btn.dataset.level || "quartier";
      setCurrentLevel(nextLevel);
    });
  });

  const slider = document.getElementById("annee-slider");
  const anneeLabel = document.getElementById("annee-label");
  currentAnnee = +slider.value;
  anneeLabel.textContent = currentAnnee;
  slider.addEventListener("input", () => {
    currentAnnee = +slider.value;
    anneeLabel.textContent = currentAnnee;
    refreshMap();
  });

  initGeocode();
  initCompare(() => currentAnnee, clearCompareHighlights);

  // Theme toggle
  let darkMap = false;
  const themeBtn  = document.getElementById("theme-toggle");
  const iconSun   = document.getElementById("theme-icon-sun");
  const iconMoon  = document.getElementById("theme-icon-moon");
  themeBtn.addEventListener("click", () => {
    darkMap = !darkMap;
    document.documentElement.dataset.theme = darkMap ? "dark" : "light";
    setMapTheme(darkMap);
    iconSun.classList.toggle("hidden", darkMap);
    iconMoon.classList.toggle("hidden", !darkMap);
    themeBtn.title = darkMap ? "Passer en mode clair" : "Passer en mode sombre";
  });

  // Toggles de couches de points
  document.querySelectorAll(".point-layer-toggle").forEach((cb) => {
    cb.addEventListener("change", () => togglePointLayer(cb.dataset.type, cb.checked));
  });

  initAuth(() => {
    if (hasLoadedData) return;
    hasLoadedData = true;
    const waitForMap = setInterval(() => {
      try {
        setCurrentLevel(currentLevel, { force: true });
        clearInterval(waitForMap);
      } catch (_) {}
    }, 500);
  });
}

// Attendre que le DOM soit prêt et le layout calculé
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
