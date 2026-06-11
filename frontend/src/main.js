import "maplibre-gl/dist/maplibre-gl.css";
import { initMap, updateMapData } from "./map.js";
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
  pct_logements_sociaux:  "% Logements sociaux",
};

let currentAnnee = 2026;
let currentIndicateur = "score_global";

async function refreshMap() {
  try {
    const geojson = await fetchGeoJSON(currentAnnee, currentIndicateur);
    updateMapData(geojson, currentIndicateur, INDICATEUR_LABELS[currentIndicateur]);
    updateLegend();
  } catch (e) {
    console.error("Erreur fetchGeoJSON", e);
  }
}

function updateLegend() {
  document.getElementById("legend-title").textContent =
    INDICATEUR_LABELS[currentIndicateur] || currentIndicateur;

  const gradient = document.getElementById("legend-gradient");
  if (["prix_m2_median", "pct_logements_sociaux"].includes(currentIndicateur)) {
    gradient.style.background = "linear-gradient(to right, #ffffcc, #0066ff)";
    document.querySelector(".legend-min").textContent = "min";
    document.querySelector(".legend-max").textContent = "max";
  } else {
    gradient.style.background = "linear-gradient(to right, #d73027, #fdae61, #1a9641)";
    document.querySelector(".legend-min").textContent = "0";
    document.querySelector(".legend-max").textContent = "100";
  }
}

function init() {
  initMap((quartier) => openSidebar(quartier, currentAnnee));

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
  initCompare(() => currentAnnee);

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
