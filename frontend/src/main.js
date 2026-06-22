import "maplibre-gl/dist/maplibre-gl.css";
import { initMap, updateMapData, setMapTheme, getIndicatorScale, clearCompareHighlights } from "./map.js";
import { openSidebar } from "./sidebar.js";
import { initCompare } from "./compare.js";
import { initGeocode } from "./geocode.js";
import { fetchGeoJSON } from "./api.js";

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
    const geojson = await fetchGeoJSON(currentAnnee, currentIndicateur);
    currentScale =
      updateMapData(geojson, currentIndicateur, INDICATEUR_LABELS[currentIndicateur]) ??
      currentScale;
    updateLegend();
  } catch (e) {
    console.error("Erreur fetchGeoJSON", e);
  }
}

function updateLegend() {
  document.getElementById("legend-title").textContent =
    INDICATEUR_LABELS[currentIndicateur] || currentIndicateur;
  const legendNote = document.getElementById("legend-note");

  const gradient = document.getElementById("legend-gradient");
  gradient.style.background = "linear-gradient(to right, #ef4444, #f59e0b, #22c55e)";
  document.getElementById("legend-min").textContent = formatLegendValue(currentScale.min);
  document.getElementById("legend-max").textContent = formatLegendValue(currentScale.max);
  if (legendNote) {
    legendNote.textContent = currentIndicateur === "prix_m2_median"
      ? "Valeur DVF héritée de l'arrondissement avec couleurs inversées : plus cher = rouge."
      : "";
  }
}

function init() {
  initMap((quartier) => openSidebar(quartier, currentAnnee, getIndicatorScale));

  const indicateurSel = document.getElementById("indicateur-select");
  indicateurSel.addEventListener("change", () => {
    currentIndicateur = indicateurSel.value;
    refreshMap();
  });

  const slider = document.getElementById("annee-slider");
  const anneeLabel = document.getElementById("annee-label");
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
    setMapTheme(darkMap);
    iconSun.classList.toggle("hidden", darkMap);
    iconMoon.classList.toggle("hidden", !darkMap);
    themeBtn.title = darkMap ? "Passer en mode clair" : "Passer en mode sombre";
  });

  const waitForMap = setInterval(() => {
    try {
      refreshMap();
      clearInterval(waitForMap);
    } catch (_) {}
  }, 500);
}

// Attendre que le DOM soit prêt et le layout calculé
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
