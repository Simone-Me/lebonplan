import maplibregl from "maplibre-gl";
import { fetchPointsGeoJSON } from "./api.js";

let map = null;
let popup = null;
let isDark = false;
let currentGeoJSON = { type: "FeatureCollection", features: [] };
let currentMarker = null;
let currentAreaLevel = "quartier";
let mapLevelSyncHandler = null;
let currentScale = {
  min: 0,
  max: 100,
  steps: [
    [0, "#ef4444"],
    [25, "#f97316"],
    [50, "#f59e0b"],
    [75, "#84cc16"],
    [100, "#22c55e"],
  ],
};
const currentScalesByIndicator = {};
const REVERSED_SCALE_INDICATORS = new Set(["prix_m2_median"]);
const activePointLayers = new Set();
const pointLayerEventsBound = new Set();

const STYLES = {
  light: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
  dark:  "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
};

function scoreToColor(steps) {
  return ["interpolate", ["linear"], ["get", "__score"]].concat(
    steps.flatMap(([v, c]) => [v, c])
  );
}

function computeScale(values) {
  const finiteValues = values.filter((value) => Number.isFinite(value));
  if (!finiteValues.length) {
    return {
      min: 0,
      max: 100,
      steps: [
        [0, "#ef4444"],
        [25, "#f97316"],
        [50, "#f59e0b"],
        [75, "#84cc16"],
        [100, "#22c55e"],
      ],
    };
  }

  const min = Math.min(...finiteValues);
  const max = Math.max(...finiteValues);
  if (min === max) {
    return {
      min,
      max,
      steps: [
        [min, "#f59e0b"],
        [min + 1e-6, "#f59e0b"],
      ],
    };
  }

  const range = max - min;
  return {
    min,
    max,
    steps: [
      [min, "#ef4444"],
      [min + range * 0.25, "#f97316"],
      [min + range * 0.5, "#f59e0b"],
      [min + range * 0.75, "#84cc16"],
      [max, "#22c55e"],
    ],
  };
}

function reverseScale(scale) {
  const colors = scale.steps.map(([, color]) => color).reverse();
  return {
    min: scale.min,
    max: scale.max,
    steps: scale.steps.map(([value], index) => [value, colors[index]]),
  };
}

function toNumericOrNaN(value) {
  return value == null ? NaN : Number(value);
}

function applyCurrentScale() {
  if (!map?.getLayer("quartiers-fill")) return;
  map.setPaintProperty("quartiers-fill", "fill-color", scoreToColor(currentScale.steps));
}

function getAreaLevelForZoom(zoom) {
  if (zoom < 12) return "arrondissement";
  if (zoom < 14.5) return "quartier";
  return "iris";
}

function getAreaIdField(level = currentAreaLevel) {
  if (level === "arrondissement") return "arrondissement";
  if (level === "iris") return "iris_id";
  return "quartier_id";
}

function getAreaIdValue(props, level = currentAreaLevel) {
  if (level === "arrondissement") return Number(props.arrondissement);
  if (level === "iris") return props.iris_id;
  return props.quartier_id;
}

function getSelectionFilter(value, level = currentAreaLevel) {
  const field = getAreaIdField(level);
  if (level === "arrondissement") {
    return ["==", field, Number.isFinite(Number(value)) ? Number(value) : -1];
  }
  return ["==", field, value || ""];
}

function getLineOpacity(isActive) {
  return isActive ? 0.95 : 0;
}

function setupLayers() {
  if (map.getSource("quartiers")) return;

  map.addSource("quartiers", {
    type: "geojson",
    data: currentGeoJSON,
  });

  // Arrondissement highlight (sous le fill principal)
  map.addLayer({
    id: "arrondissement-highlight",
    type: "fill",
    source: "quartiers",
    filter: ["==", "arrondissement", -1],
    paint: {
      "fill-color": "#ffffff",
      "fill-opacity": 0.18,
    },
  });

  map.addLayer({
    id: "quartiers-fill",
    type: "fill",
    source: "quartiers",
    paint: {
      "fill-color": scoreToColor(currentScale.steps),
      "fill-opacity": 0.75,
    },
  });

  map.addLayer({
    id: "quartiers-border",
    type: "line",
    source: "quartiers",
    paint: {
      "line-color": "#ffffff",
      "line-width": 0.8,
      "line-opacity": 0.6,
    },
  });

  // Bordure arrondissement (ligne épaisse autour des quartiers du même arrondissement)
  map.addLayer({
    id: "arrondissement-border",
    type: "line",
    source: "quartiers",
    filter: ["==", "arrondissement", -1],
    paint: {
      "line-color": "#ffffff",
      "line-width": 2.5,
      "line-opacity": 0.9,
    },
  });

  map.addLayer({
    id: "compare-arr1-border",
    type: "line",
    source: "quartiers",
    filter: ["==", "arrondissement", -1],
    paint: {
      "line-color": "#111827",
      "line-width": 4,
      "line-opacity": 0,
    },
  });

  map.addLayer({
    id: "compare-arr2-border",
    type: "line",
    source: "quartiers",
    filter: ["==", "arrondissement", -1],
    paint: {
      "line-color": "#dc2626",
      "line-width": 4,
      "line-opacity": 0,
    },
  });

  map.addLayer({
    id: "compare-quartier1-border",
    type: "line",
    source: "quartiers",
    filter: ["==", "quartier_id", ""],
    paint: {
      "line-color": "#111827",
      "line-width": 5,
      "line-opacity": 0,
    },
  });

  map.addLayer({
    id: "compare-quartier2-border",
    type: "line",
    source: "quartiers",
    filter: ["==", "quartier_id", ""],
    paint: {
      "line-color": "#dc2626",
      "line-width": 5,
      "line-opacity": 0,
    },
  });

  map.addLayer({
    id: "quartiers-selected",
    type: "line",
    source: "quartiers",
    filter: ["==", "quartier_id", ""],
    paint: {
      "line-color": "#ffffff",
      "line-width": 3.2,
      "line-opacity": 1,
    },
  });
}

function attachEvents(onQuartierClick) {
  map.on("mousemove", "quartiers-fill", (e) => {
    if (!e.features.length) return;
    map.getCanvas().style.cursor = "pointer";
    const props = e.features[0].properties;
    const score = props.__score != null ? Number(props.__score).toFixed(1) : "—";
    const suffixe = props.arrondissement
      ? `<br/><span style="color:#94a3b8;font-size:11px">${props.arrondissement}e arrondissement</span>`
      : "";
    popup
      .setLngLat(e.lngLat)
      .setHTML(
        `<strong>${props.nom || "Quartier administratif"}</strong>${suffixe}<br/>
         <span style="color:#94a3b8;font-size:11px">${props.__indicateur_label || "Score"}</span>
         <strong style="float:right;color:#f1f5f9">${score}</strong>`
      )
      .addTo(map);
  });

  map.on("mouseleave", "quartiers-fill", () => {
    map.getCanvas().style.cursor = "";
    popup.remove();
  });

  map.on("click", "quartiers-fill", (e) => {
    const props = e.features[0].properties;
    const areaId = getAreaIdValue(props);
    if (areaId == null || areaId === "") return;

    onQuartierClick({
      level: currentAreaLevel,
      area_id: areaId,
      iris_id: props.iris_id,
      iris_code: props.iris_code,
      iris_type: props.iris_type,
      quartier_id:    props.quartier_id,
      quartier_code:  props.quartier_code,
      nom:            props.nom,
      arrondissement: props.arrondissement,
    });

    map.setFilter("quartiers-selected", getSelectionFilter(areaId));

    if (props.arrondissement != null) {
      const arr = Number(props.arrondissement);
      map.setFilter("arrondissement-highlight", ["==", "arrondissement", arr]);
      map.setFilter("arrondissement-border",    ["==", "arrondissement", arr]);
    }
  });
}

export function initMap(onQuartierClick) {
  map = new maplibregl.Map({
    container: "map",
    style: STYLES.light,
    center: [2.347, 48.859],
    zoom: 11.5,
  });

  window.addEventListener("resize", () => map.resize());

  const ro = new ResizeObserver(() => map.resize());
  ro.observe(document.getElementById("map-area"));

  map.addControl(new maplibregl.NavigationControl(), "top-right");

  popup = new maplibregl.Popup({
    closeButton: false,
    closeOnClick: false,
    className: "map-popup",
  });

  window._map = map;

  map.on("load", () => {
    map.resize();
    setupLayers();
    attachEvents(onQuartierClick);
  });

  map.on("zoomend", () => {
    mapLevelSyncHandler?.(getAreaLevelForZoom(map.getZoom()));
  });

  return map;
}

export function setMapTheme(dark, onReady) {
  if (!map) return;
  isDark = dark;

  const waitForStyle = () => {
    if (map.isStyleLoaded()) {
      setupLayers();
      const src = map.getSource("quartiers");
      if (src) src.setData(currentGeoJSON);
      applyCurrentScale();
      restoreActivePointLayers();
      onReady?.();
    } else {
      setTimeout(waitForStyle, 80);
    }
  };

  map.setStyle(dark ? STYLES.dark : STYLES.light);
  setTimeout(waitForStyle, 120);
}

export function updateMapData(geojson, indicateur, indicateurLabel, areaLevel = "quartier") {
  currentAreaLevel = areaLevel;
  const features = geojson.features.map((f) => ({
    ...f,
    properties: {
      ...f.properties,
      __score: f.properties[indicateur] ?? null,
      __indicateur_label: indicateurLabel,
    },
  }));

  const baseScale = computeScale(
    features.map((feature) => {
      return toNumericOrNaN(feature.properties.__score);
    })
  );
  currentScale = REVERSED_SCALE_INDICATORS.has(indicateur)
    ? reverseScale(baseScale)
    : baseScale;
  Object.assign(currentScalesByIndicator, {
    score_global: computeScale(features.map((feature) => toNumericOrNaN(feature.properties.score_global))),
    score_qualite_vie: computeScale(features.map((feature) => toNumericOrNaN(feature.properties.score_qualite_vie))),
    score_transports: computeScale(features.map((feature) => toNumericOrNaN(feature.properties.score_transports))),
    score_loisirs: computeScale(features.map((feature) => toNumericOrNaN(feature.properties.score_loisirs))),
    score_services: computeScale(features.map((feature) => toNumericOrNaN(feature.properties.score_services))),
    prix_m2_median: computeScale(features.map((feature) => toNumericOrNaN(feature.properties.prix_m2_median))),
    nb_logements_sociaux: computeScale(features.map((feature) => toNumericOrNaN(feature.properties.nb_logements_sociaux))),
  });
  currentGeoJSON = { type: "FeatureCollection", features };

  if (!map || !map.isStyleLoaded()) {
    return { min: currentScale.min, max: currentScale.max };
  }
  const src = map.getSource("quartiers");
  if (src) src.setData(currentGeoJSON);
  applyCurrentScale();
  clearAreaSelection();

  return { min: currentScale.min, max: currentScale.max };
}

export function flyToLngLat(lng, lat, zoom = 14) {
  map?.flyTo({ center: [lng, lat], zoom });
}

export function placeMarker(lng, lat, label) {
  if (!map) return;
  clearMarker();
  const popup = new maplibregl.Popup({ closeButton: true, closeOnClick: false }).setText(label);
  currentMarker = new maplibregl.Marker({ color: "#3b82f6" })
    .setLngLat([lng, lat])
    .setPopup(popup)
    .addTo(map);
  popup.on("close", () => {
    if (currentMarker) {
      currentMarker.remove();
      currentMarker = null;
    }
    window.dispatchEvent(new CustomEvent("geocode-marker-cleared"));
  });
  currentMarker.togglePopup();
}

export function clearMarker() {
  if (!currentMarker) return;
  currentMarker.remove();
  currentMarker = null;
  window.dispatchEvent(new CustomEvent("geocode-marker-cleared"));
}

export function getIndicatorScale(indicator) {
  return currentScalesByIndicator[indicator] ?? { min: 0, max: 100 };
}

export function setCompareHighlights({
  arr1 = null,
  arr2 = null,
  quartier1 = null,
  quartier2 = null,
} = {}) {
  if (!map) return;

  const hasArr1 = Number.isInteger(arr1) && arr1 > 0;
  const hasArr2 = Number.isInteger(arr2) && arr2 > 0;
  const hasQuartier1 = typeof quartier1 === "string" && quartier1.length > 0;
  const hasQuartier2 = typeof quartier2 === "string" && quartier2.length > 0;

  map.setFilter("compare-arr1-border", ["==", "arrondissement", hasArr1 ? arr1 : -1]);
  map.setFilter("compare-arr2-border", ["==", "arrondissement", hasArr2 ? arr2 : -1]);
  map.setFilter("compare-quartier1-border", ["==", "quartier_id", hasQuartier1 ? quartier1 : ""]);
  map.setFilter("compare-quartier2-border", ["==", "quartier_id", hasQuartier2 ? quartier2 : ""]);
  map.setPaintProperty("compare-arr1-border", "line-opacity", getLineOpacity(hasArr1));
  map.setPaintProperty("compare-arr2-border", "line-opacity", getLineOpacity(hasArr2));
  map.setPaintProperty("compare-quartier1-border", "line-opacity", getLineOpacity(hasQuartier1));
  map.setPaintProperty("compare-quartier2-border", "line-opacity", getLineOpacity(hasQuartier2));
}

export function clearCompareHighlights() {
  setCompareHighlights();
}

export function clearAreaSelection() {
  if (!map) return;
  map.setFilter("quartiers-selected", getSelectionFilter(null));
  map.setFilter("arrondissement-highlight", ["==", "arrondissement", -1]);
  map.setFilter("arrondissement-border", ["==", "arrondissement", -1]);
}

export function setMapLevelSyncHandler(handler) {
  mapLevelSyncHandler = handler;
}

// ─── Couche de points Silver ──────────────────────────────────────────────────

const POINT_COLORS = {
  gares:         "#f59e0b",
  velib:         "#3b82f6",
  espaces_verts: "#22c55e",
  musees:        "#a855f7",
  cinemas:       "#ec4899",
  bibliotheques: "#14b8a6",
};

const _pointPopup = new maplibregl.Popup({ closeButton: false, closeOnClick: false });

async function renderPointLayer(type) {
  if (!map || !map.isStyleLoaded()) return;
  const layerId = `points-${type}`;
  const sourceId = `points-src-${type}`;

  try {
    const geojson = await fetchPointsGeoJSON(type);

    if (map.getSource(sourceId)) {
      map.getSource(sourceId).setData(geojson);
    } else {
      map.addSource(sourceId, { type: "geojson", data: geojson });
    }

    if (!map.getLayer(layerId)) {
      map.addLayer({
        id: layerId,
        type: "circle",
        source: sourceId,
        paint: {
          "circle-radius": 6,
          "circle-color": POINT_COLORS[type] ?? "#6b7280",
          "circle-stroke-width": 2,
          "circle-stroke-color": "#ffffff",
          "circle-opacity": 0.96,
        },
      });
    }

    if (!pointLayerEventsBound.has(layerId)) {
      map.on("mouseenter", layerId, (e) => {
        map.getCanvas().style.cursor = "pointer";
        const props = e.features[0].properties;
        _pointPopup
          .setLngLat(e.lngLat)
          .setHTML(`<strong>${props.nom || type}</strong><br/><span style="color:#94a3b8;font-size:11px">${type}</span>`)
          .addTo(map);
      });
      map.on("mouseleave", layerId, () => {
        map.getCanvas().style.cursor = "";
        _pointPopup.remove();
      });
      pointLayerEventsBound.add(layerId);
    }
  } catch (_) { /* silently ignore if API unavailable */ }
}

function removePointLayer(type) {
  if (!map) return;
  const layerId = `points-${type}`;
  const sourceId = `points-src-${type}`;
  if (map.getLayer(layerId)) map.removeLayer(layerId);
  if (map.getSource(sourceId)) map.removeSource(sourceId);
}

function restoreActivePointLayers() {
  activePointLayers.forEach((type) => {
    renderPointLayer(type);
  });
}

export async function togglePointLayer(type, enabled) {
  if (!map) return;
  if (!enabled) {
    activePointLayers.delete(type);
    removePointLayer(type);
    return;
  }

  activePointLayers.add(type);
  await renderPointLayer(type);
}
