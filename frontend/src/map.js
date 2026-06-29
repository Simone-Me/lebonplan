import maplibregl from "maplibre-gl";
import { fetchPointsGeoJSON } from "./api.js";
import bikeSvg from "lucide-static/icons/bike.svg?raw";
import filmSvg from "lucide-static/icons/film.svg?raw";
import landmarkSvg from "lucide-static/icons/landmark.svg?raw";
import librarySvg from "lucide-static/icons/library.svg?raw";
import treesSvg from "lucide-static/icons/trees.svg?raw";
import trainFrontTunnelSvg from "lucide-static/icons/train-front-tunnel.svg?raw";

let map = null;
let popup = null;
let isDark = false;
let currentGeoJSON = { type: "FeatureCollection", features: [] };
let currentMarker = null;
let currentAreaLevel = "quartier";
let currentSelection = null;
let mapLevelSyncHandler = null;
const selectionGeoJSONCache = {
  arrondissement: null,
  quartier: null,
  iris: null,
};
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
const REVERSED_SCALE_INDICATORS = new Set(["prix_m2_median", "nb_logements_sociaux"]);
const activePointLayers = new Set();
const pointLayerEventsBound = new Set();

const STYLES = {
  light: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
  dark:  "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
};
const SELECTION_COLORS = {
  arrondissement: "#ffffff",
  quartier: "#111827",
  iris: "#130bf5",
};
const SELECTION_FILL_COLORS = {
  arrondissement: "rgba(255,255,255,0.18)",
  quartier: "rgba(15,23,42,0.14)",
  iris: "rgba(245,158,11,0.18)",
};

function emptyFeatureCollection() {
  return { type: "FeatureCollection", features: [] };
}

function oneFeatureCollection(feature) {
  if (!feature) return emptyFeatureCollection();
  return { type: "FeatureCollection", features: [feature] };
}

function setSelectionSourceData(sourceId, feature) {
  const src = map?.getSource(sourceId);
  if (!src) return;
  src.setData(oneFeatureCollection(feature));
}

function getCachedAreaFeature(level, id) {
  const cache = selectionGeoJSONCache[level];
  if (!cache?.features?.length || id == null || id === "") return null;

  const field = level === "arrondissement"
    ? "arrondissement"
    : level === "iris"
      ? "iris_id"
      : "quartier_id";

  return cache.features.find((feature) => {
    const props = feature?.properties || {};
    if (field === "arrondissement") {
      return Number(props[field]) === Number(id);
    }
    return String(props[field] ?? "") === String(id);
  }) || null;
}

function getParentFeatures() {
  if (!currentSelection) {
    return { arrondissement: null, quartier: null, iris: null };
  }

  const selectionLevel = currentSelection.level;
  const arrFeature = selectionLevel === "arrondissement"
    ? currentSelection.feature
    : currentSelection.arrondissement != null
      ? getCachedAreaFeature("arrondissement", currentSelection.arrondissement)
      : null;
  const quartierFeature = currentSelection.quartierId
    ? selectionLevel === "quartier"
      ? currentSelection.feature
      : getCachedAreaFeature("quartier", currentSelection.quartierId)
    : null;
  const irisFeature = selectionLevel === "iris" ? currentSelection.feature : null;

  return {
    arrondissement: arrFeature,
    quartier: quartierFeature,
    iris: irisFeature,
  };
}

function scoreToColor(steps) {
  return ["interpolate", ["linear"], ["get", "__score"]].concat(
    steps.flatMap(([v, c]) => [v, c])
  );
}

function quantile(sortedValues, q) {
  if (!sortedValues.length) return NaN;
  if (sortedValues.length === 1) return sortedValues[0];
  const index = (sortedValues.length - 1) * q;
  const lower = Math.floor(index);
  const upper = Math.ceil(index);
  if (lower === upper) return sortedValues[lower];
  const weight = index - lower;
  return sortedValues[lower] * (1 - weight) + sortedValues[upper] * weight;
}

function dedupeStepValues(steps) {
  const deduped = [];
  for (const [value, color] of steps) {
    const previous = deduped[deduped.length - 1];
    if (previous && previous[0] === value) {
      previous[1] = color;
      continue;
    }
    deduped.push([value, color]);
  }
  return deduped;
}

function computeScale(values, strategy = "linear") {
  const finiteValues = values.filter((value) => Number.isFinite(value));
  if (!finiteValues.length) {
    return {
      min: 0,
      max: 100,
      steps: [
        [0, "#ef4444"],
        [40, "#f97316"],
        [70, "#f59e0b"],
        [85, "#84cc16"],
        [100, "#22c55e"],
      ],
    };
  }

  const sortedValues = [...finiteValues].sort((a, b) => a - b);
  const min = sortedValues[0];
  const max = sortedValues[sortedValues.length - 1];
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

  if (strategy === "quantile") {
    return {
      min,
      max,
      steps: dedupeStepValues([
        [min, "#ef4444"],
        [quantile(sortedValues, 0.25), "#f97316"],
        [quantile(sortedValues, 0.5), "#f59e0b"],
        [quantile(sortedValues, 0.75), "#84cc16"],
        [max, "#22c55e"],
      ]),
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

function computeDivergingScale(deltas) {
  const finite = deltas.filter(Number.isFinite);
  if (!finite.length) return { min: -1, max: 1, steps: [[-1, "#94a3b8"], [0, "#94a3b8"], [1, "#94a3b8"]] };
  const absMax = Math.max(...finite.map(Math.abs));
  if (absMax === 0) return { min: -1, max: 1, steps: [[-1, "#94a3b8"], [0, "#94a3b8"], [1, "#94a3b8"]] };
  return {
    min: -absMax,
    max: absMax,
    steps: dedupeStepValues([
      [-absMax,        "#10b981"],
      [-absMax * 0.4,  "#34d399"],
      [0,              "#cbd5e1"],
      [absMax * 0.4,   "#fb923c"],
      [absMax,         "#f43f5e"],
    ]),
  };
}

function getScaleStrategy(indicator) {
  if (!indicator) return "linear";
  if (indicator.startsWith("score_")) return "quantile";
  return "linear";
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

function getEmptyFilter(level = currentAreaLevel) {
  if (level === "arrondissement") return ["==", "arrondissement", -1];
  if (level === "iris") return ["==", "iris_id", ""];
  return ["==", "quartier_id", ""];
}

function applySelectionLayers() {
  if (!map) return;

  const selectionLevel = currentSelection?.level || null;
  const parents = getParentFeatures();
  const hasArrondissement = Boolean(parents.arrondissement);
  const hasQuartier = Boolean(parents.quartier);
  const hasIris = Boolean(parents.iris);

  setSelectionSourceData("selection-arr-source", parents.arrondissement);
  setSelectionSourceData("selection-quartier-source", parents.quartier);
  setSelectionSourceData("selection-iris-source", parents.iris);

  map.setPaintProperty("selection-arr-border", "line-opacity", hasArrondissement ? 1 : 0);
  map.setPaintProperty("selection-arr-border", "line-color", SELECTION_COLORS.arrondissement);
  map.setPaintProperty("selection-arr-border", "line-width", selectionLevel === "arrondissement" ? 6 : 4.8);

  map.setPaintProperty("selection-quartier-border", "line-opacity", hasQuartier && (selectionLevel === "quartier" || selectionLevel === "iris") ? 1 : 0);
  map.setPaintProperty("selection-quartier-border", "line-color", SELECTION_COLORS.quartier);
  map.setPaintProperty("selection-quartier-border", "line-width", selectionLevel === "quartier" ? 5 : 4.2);

  map.setPaintProperty("selection-iris-border", "line-opacity", selectionLevel === "iris" && hasIris ? 1 : 0);
  map.setPaintProperty("selection-iris-border", "line-color", SELECTION_COLORS.iris);
  map.setPaintProperty("selection-iris-border", "line-width", 4);
}

function updateSelectionFromFeature(feature) {
  const props = feature?.properties || {};
  const areaId = getAreaIdValue(props);
  if (areaId == null || areaId === "") return;

  currentSelection = {
    level: currentAreaLevel,
    areaId,
    feature,
    arrondissement: Number.isFinite(Number(props.arrondissement)) ? Number(props.arrondissement) : null,
    quartierId: typeof props.quartier_id === "string" ? props.quartier_id : null,
    irisId: typeof props.iris_id === "string" ? props.iris_id : null,
  };
  applySelectionLayers();
}

function findFeatureBySelection(selection) {
  if (!selection?.level || !currentGeoJSON?.features?.length) return null;
  const field = getAreaIdField(selection.level);
  const targetId = selection.area_id ?? selection.areaId;
  if (targetId == null || targetId === "") return null;

  return currentGeoJSON.features.find((feature) => {
    const props = feature?.properties || {};
    if (selection.level === "arrondissement") {
      return Number(props[field]) === Number(targetId);
    }
    return String(props[field] ?? "") === String(targetId);
  }) || null;
}

function setupLayers() {
  if (map.getSource("quartiers")) return;

  map.addSource("quartiers", {
    type: "geojson",
    data: currentGeoJSON,
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

  map.addSource("selection-arr-source", {
    type: "geojson",
    data: emptyFeatureCollection(),
  });
  map.addSource("selection-quartier-source", {
    type: "geojson",
    data: emptyFeatureCollection(),
  });
  map.addSource("selection-iris-source", {
    type: "geojson",
    data: emptyFeatureCollection(),
  });

  map.addLayer({
    id: "selection-arr-border",
    type: "line",
    source: "selection-arr-source",
    paint: {
      "line-color": SELECTION_COLORS.arrondissement,
      "line-width": 5.2,
      "line-opacity": 0,
    },
  });

  map.addLayer({
    id: "selection-quartier-border",
    type: "line",
    source: "selection-quartier-source",
    paint: {
      "line-color": SELECTION_COLORS.quartier,
      "line-width": 4.6,
      "line-opacity": 0,
    },
  });

  map.addLayer({
    id: "selection-iris-border",
    type: "line",
    source: "selection-iris-source",
    paint: {
      "line-color": SELECTION_COLORS.iris,
      "line-width": 3.6,
      "line-opacity": 0,
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
}

function attachEvents(onQuartierClick) {
  map.on("mousemove", "quartiers-fill", (e) => {
    if (!e.features.length) return;
    map.getCanvas().style.cursor = "pointer";
    const props = e.features[0].properties;

    const niveauLabel = currentAreaLevel === "arrondissement"
      ? "Arrondissement"
      : currentAreaLevel === "iris"
        ? "IRIS"
        : "Quartier";
    const arrStr = props.arrondissement ? ` · ${props.arrondissement}e arr.` : "";

    const isDelta   = !!props.__is_delta;
    const scoreRaw  = props.__score != null ? Number(props.__score) : null;
    let scoreText, scoreColor;
    if (isDelta && scoreRaw != null) {
      const sign = scoreRaw >= 0 ? "+" : "";
      scoreText  = `${sign}${Math.round(scoreRaw).toLocaleString("fr-FR")} €/m²`;
      scoreColor = scoreRaw > 50 ? "#f43f5e" : scoreRaw < -50 ? "#10b981" : "var(--tx-3)";
    } else {
      scoreText  = scoreRaw != null ? scoreRaw.toFixed(1) : "—";
      scoreColor = "var(--ac)";
    }

    const deltaDetail = isDelta && props.__delta_v1 != null && props.__delta_v2 != null
      ? `<div style="font-size:10px;color:var(--tx-3);margin-top:2px;text-align:right">
           ${Math.round(Number(props.__delta_v1)).toLocaleString("fr-FR")} → ${Math.round(Number(props.__delta_v2)).toLocaleString("fr-FR")} €/m²
         </div>`
      : "";

    const prixRaw       = props.prix_m2_median;
    const prixFormatted = !isDelta && prixRaw != null && !Number.isNaN(Number(prixRaw))
      ? `${Math.round(Number(prixRaw)).toLocaleString("fr-FR")} €/m²`
      : null;
    const prixRow = prixFormatted
      ? `<div style="display:flex;justify-content:space-between;align-items:center;
              border-top:1px solid rgba(148,163,184,0.2);margin-top:6px;padding-top:6px">
           <span style="font-size:11px;color:var(--tx-3)">Prix m²</span>
           <strong style="font-size:12px;color:var(--tx-1)">${prixFormatted}</strong>
         </div>`
      : "";

    popup
      .setLngLat(e.lngLat)
      .setHTML(
        `<div style="min-width:160px">
           <div style="font-weight:700;font-size:13px;color:var(--tx-1);margin-bottom:2px">
             ${props.nom || niveauLabel}
           </div>
           <div style="font-size:10px;color:var(--tx-3);margin-bottom:7px">
             ${niveauLabel}${arrStr}
           </div>
           <div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px">
             <span style="font-size:11px;color:var(--tx-2)">${props.__indicateur_label || "Score"}</span>
             <strong style="font-size:14px;color:${scoreColor}">${scoreText}</strong>
           </div>
           ${deltaDetail}${prixRow}
         </div>`
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

    updateSelectionFromFeature(e.features[0]);
  });
}

export function initMap(onQuartierClick, { dark = false } = {}) {
  isDark = dark;
  map = new maplibregl.Map({
    container: "map",
    style: dark ? STYLES.dark : STYLES.light,
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
      applySelectionLayers();
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

  const indicatorValues = features.map((feature) => toNumericOrNaN(feature.properties.__score));
  const baseScale = computeScale(
    indicatorValues,
    getScaleStrategy(indicateur)
  );
  currentScale = REVERSED_SCALE_INDICATORS.has(indicateur)
    ? reverseScale(baseScale)
    : baseScale;
  Object.assign(currentScalesByIndicator, {
    score_global: computeScale(features.map((feature) => toNumericOrNaN(feature.properties.score_global)), "quantile"),
    score_qualite_vie: computeScale(features.map((feature) => toNumericOrNaN(feature.properties.score_qualite_vie)), "quantile"),
    score_transports: computeScale(features.map((feature) => toNumericOrNaN(feature.properties.score_transports)), "quantile"),
    score_loisirs: computeScale(features.map((feature) => toNumericOrNaN(feature.properties.score_loisirs)), "quantile"),
    score_services: computeScale(features.map((feature) => toNumericOrNaN(feature.properties.score_services)), "quantile"),
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
  applySelectionLayers();
  restoreActivePointLayers();
  map.triggerRepaint?.();

  return { min: currentScale.min, max: currentScale.max };
}

export function updateMapDataDelta(geojson1, geojson2, indicateur, indicateurLabel, areaLevel = "quartier") {
  currentAreaLevel = areaLevel;

  const idField = areaLevel === "arrondissement" ? "arrondissement"
    : areaLevel === "iris" ? "iris_id"
    : "quartier_id";

  const lookup1 = new Map();
  for (const f of geojson1.features) {
    const id = String(f.properties?.[idField] ?? "");
    if (id) lookup1.set(id, toNumericOrNaN(f.properties?.[indicateur]));
  }

  const features = geojson2.features.map((f) => {
    const id    = String(f.properties?.[idField] ?? "");
    const v2    = toNumericOrNaN(f.properties?.[indicateur]);
    const v1    = lookup1.get(id) ?? NaN;
    const delta = Number.isFinite(v1) && Number.isFinite(v2) ? v2 - v1 : null;
    return {
      ...f,
      properties: {
        ...f.properties,
        __score:           delta,
        __indicateur_label: `Δ ${indicateurLabel}`,
        __delta_v1:        Number.isFinite(v1) ? v1 : null,
        __delta_v2:        Number.isFinite(v2) ? v2 : null,
        __is_delta:        true,
      },
    };
  });

  const deltas  = features.map((f) => toNumericOrNaN(f.properties.__score));
  currentScale  = computeDivergingScale(deltas);
  currentGeoJSON = { type: "FeatureCollection", features };

  if (!map || !map.isStyleLoaded()) return { min: currentScale.min, max: currentScale.max };
  const src = map.getSource("quartiers");
  if (src) src.setData(currentGeoJSON);
  applyCurrentScale();
  applySelectionLayers();
  map.triggerRepaint?.();

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
  currentSelection = null;
  setSelectionSourceData("selection-arr-source", null);
  setSelectionSourceData("selection-quartier-source", null);
  setSelectionSourceData("selection-iris-source", null);
  map.setPaintProperty("selection-arr-border", "line-opacity", 0);
  map.setPaintProperty("selection-quartier-border", "line-opacity", 0);
  map.setPaintProperty("selection-iris-border", "line-opacity", 0);
}

export function setMapLevelSyncHandler(handler) {
  mapLevelSyncHandler = handler;
}

export function setSelectionGeoJSONCache(level, geojson) {
  if (!level || !(level in selectionGeoJSONCache)) return;
  selectionGeoJSONCache[level] = geojson || emptyFeatureCollection();
  if (currentSelection) {
    applySelectionLayers();
  }
}

export function syncSelectionFromArea(selection) {
  if (!selection) {
    clearAreaSelection();
    return false;
  }
  const feature = findFeatureBySelection(selection);
  if (!feature) {
    clearAreaSelection();
    return false;
  }
  updateSelectionFromFeature(feature);
  return true;
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
const POINT_ICONS = {
  velib: bikeSvg,
  cinemas: filmSvg,
  bibliotheques: librarySvg,
  espaces_verts: treesSvg,
  musees: landmarkSvg,
  gares: trainFrontTunnelSvg,
};
const POINT_ICON_SIZE = 25;

const POINT_POPUP_OFFSET = [0, 14];
const _pointHoverPopup = new maplibregl.Popup({
  closeButton: false,
  closeOnClick: false,
  className: "map-popup map-popup-point",
  anchor: "top",
  offset: POINT_POPUP_OFFSET,
});
const _pointPinnedPopup = new maplibregl.Popup({
  closeButton: true,
  closeOnClick: false,
  className: "map-popup map-popup-point",
  anchor: "top",
  offset: POINT_POPUP_OFFSET,
});
let _pinnedPointId = null;

function getPointPopupId(type, feature) {
  const props = feature?.properties || {};
  const geometry = feature?.geometry?.coordinates || [];
  return [
    type,
    props.nom || "",
    props.id || "",
    props.identifiant || "",
    geometry[0] ?? "",
    geometry[1] ?? "",
  ].join("|");
}

function getPointLngLat(feature, fallbackLngLat) {
  const coords = feature?.geometry?.coordinates;
  if (Array.isArray(coords) && coords.length >= 2) {
    return coords;
  }
  return fallbackLngLat;
}

function getPointPopupHTML(type, feature) {
  const props = feature?.properties || {};
  return `<strong>${props.nom || type}</strong><br/><span style="color:#94a3b8;font-size:11px">${type}</span>`;
}

function openPinnedPointPopup(type, feature, fallbackLngLat) {
  if (!map || !feature) return;
  _pinnedPointId = getPointPopupId(type, feature);
  _pointHoverPopup.remove();
  _pointPinnedPopup
    .setLngLat(getPointLngLat(feature, fallbackLngLat))
    .setHTML(getPointPopupHTML(type, feature))
    .addTo(map);
}

_pointPinnedPopup.on("close", () => {
  _pinnedPointId = null;
});

function svgToDataUrl(svg) {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

function buildPointIconSvg(type) {
  const iconSvg = POINT_ICONS[type];
  if (!iconSvg) return null;

  const innerSvg = iconSvg
    .replace(/<svg[^>]*>/i, "")
    .replace(/<\/svg>\s*$/i, "")
    .trim();

  const color = POINT_COLORS[type] ?? "#6b7280";
  return `
    <svg xmlns="http://www.w3.org/2000/svg" width="${POINT_ICON_SIZE}" height="${POINT_ICON_SIZE}" viewBox="0 0 40 40">
      <circle cx="20" cy="20" r="16" fill="${color}" />
      <circle cx="20" cy="20" r="15.25" fill="none" stroke="rgba(255,255,255,0.94)" stroke-width="1.5" />
      <g transform="translate(8 8)" stroke="#ffffff" fill="none" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
        ${innerSvg}
      </g>
    </svg>
  `.trim();
}

function loadImageElement(src) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = reject;
    image.src = src;
  });
}

async function ensurePointIconLoaded(type) {
  if (!map) return null;

  const iconId = `point-icon-${type}`;
  if (map.hasImage(iconId)) return iconId;

  const svg = buildPointIconSvg(type);
  if (!svg) return null;

  const image = await loadImageElement(svgToDataUrl(svg));
  if (!map.hasImage(iconId)) {
    map.addImage(iconId, image);
  }
  return iconId;
}

async function renderPointLayer(type) {
  if (!map || !map.isStyleLoaded()) return;
  const layerId        = `points-${type}`;
  const clusterCircleId = `points-cluster-${type}`;
  const clusterCountId  = `points-cluster-count-${type}`;
  const sourceId       = `points-src-${type}`;
  const color          = POINT_COLORS[type] ?? "#6b7280";

  try {
    const geojson = await fetchPointsGeoJSON(type);

    if (map.getSource(sourceId)) {
      map.getSource(sourceId).setData(geojson);
    } else {
      map.addSource(sourceId, {
        type: "geojson",
        data: geojson,
        cluster: true,
        clusterMaxZoom: 14,
        clusterRadius: 40,
      });
    }

    const iconId = await ensurePointIconLoaded(type);

    /* ── Cluster circle ──────────────────────────────── */
    if (!map.getLayer(clusterCircleId)) {
      map.addLayer({
        id: clusterCircleId,
        type: "circle",
        source: sourceId,
        filter: ["has", "point_count"],
        paint: {
          "circle-color": color,
          "circle-opacity": 0.82,
          "circle-stroke-width": 2.5,
          "circle-stroke-color": "#ffffff",
          "circle-radius": [
            "step", ["get", "point_count"],
            13,    // < 10 points
            10, 18, // ≥ 10
            50, 24, // ≥ 50
          ],
        },
      });
    }

    /* ── Cluster count label ─────────────────────────── */
    if (!map.getLayer(clusterCountId)) {
      map.addLayer({
        id: clusterCountId,
        type: "symbol",
        source: sourceId,
        filter: ["has", "point_count"],
        layout: {
          "text-field": ["to-string", ["get", "point_count"]],
          "text-size": 11,
          "text-font": ["Open Sans Semibold", "Arial Unicode MS Regular"],
        },
        paint: { "text-color": "#ffffff" },
      });
    }

    /* ── Individual unclustered points ───────────────── */
    if (!map.getLayer(layerId)) {
      map.addLayer(iconId ? {
        id: layerId,
        type: "symbol",
        source: sourceId,
        filter: ["!", ["has", "point_count"]],
        layout: {
          "icon-image": iconId,
          "icon-size": 0.82,
          "icon-allow-overlap": false,
        },
      } : {
        id: layerId,
        type: "circle",
        source: sourceId,
        filter: ["!", ["has", "point_count"]],
        paint: {
          "circle-radius": 6,
          "circle-color": color,
          "circle-stroke-width": 2,
          "circle-stroke-color": "#ffffff",
          "circle-opacity": 0.96,
        },
      });
    }

    [clusterCircleId, clusterCountId, layerId].forEach((lid) => {
      if (map.getLayer(lid)) map.moveLayer(lid);
    });

    if (!pointLayerEventsBound.has(layerId)) {
      /* hover / click on individual points */
      map.on("mouseenter", layerId, (e) => {
        map.getCanvas().style.cursor = "pointer";
        const feature = e.features?.[0];
        if (!feature) return;
        if (_pinnedPointId === getPointPopupId(type, feature)) return;
        _pointHoverPopup
          .setLngLat(getPointLngLat(feature, e.lngLat))
          .setHTML(getPointPopupHTML(type, feature))
          .addTo(map);
      });
      map.on("mouseleave", layerId, () => {
        map.getCanvas().style.cursor = "";
        _pointHoverPopup.remove();
      });
      map.on("click", layerId, (e) => {
        const feature = e.features?.[0];
        if (!feature) return;
        openPinnedPointPopup(type, feature, e.lngLat);
      });

      /* click on cluster → zoom to expand */
      map.on("click", clusterCircleId, (e) => {
        const features = map.queryRenderedFeatures(e.point, { layers: [clusterCircleId] });
        const clusterId = features[0]?.properties?.cluster_id;
        if (clusterId == null) return;
        map.getSource(sourceId).getClusterExpansionZoom(clusterId, (err, zoom) => {
          if (err) return;
          map.easeTo({ center: features[0].geometry.coordinates, zoom: zoom + 0.5 });
        });
      });
      map.on("mouseenter", clusterCircleId, () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", clusterCircleId, () => { map.getCanvas().style.cursor = ""; });

      pointLayerEventsBound.add(layerId);
    }
  } catch (_) { /* silently ignore if API unavailable */ }
}

function removePointLayer(type) {
  if (!map) return;
  const layerId         = `points-${type}`;
  const clusterCircleId = `points-cluster-${type}`;
  const clusterCountId  = `points-cluster-count-${type}`;
  const sourceId        = `points-src-${type}`;
  [clusterCountId, clusterCircleId, layerId].forEach((lid) => {
    if (map.getLayer(lid)) map.removeLayer(lid);
  });
  if (map.getSource(sourceId)) map.removeSource(sourceId);
  pointLayerEventsBound.delete(layerId);
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

export function flyToZone(feature) {
  if (!map || !feature?.geometry) return;
  let minLng = Infinity, minLat = Infinity, maxLng = -Infinity, maxLat = -Infinity;
  const scan = (coords) => {
    if (!Array.isArray(coords)) return;
    if (typeof coords[0] === "number") {
      const [lng, lat] = coords;
      if (lng < minLng) minLng = lng;
      if (lat < minLat) minLat = lat;
      if (lng > maxLng) maxLng = lng;
      if (lat > maxLat) maxLat = lat;
    } else {
      coords.forEach(scan);
    }
  };
  scan(feature.geometry.coordinates);
  if (!isFinite(minLng)) return;
  map.fitBounds([[minLng, minLat], [maxLng, maxLat]], {
    padding: { top: 60, bottom: 60, left: 60, right: 60 },
    maxZoom: 15,
    duration: 700,
  });
}
