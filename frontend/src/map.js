import maplibregl from "maplibre-gl";

let map = null;
let popup = null;
let isDark = false;
let currentGeoJSON = { type: "FeatureCollection", features: [] };

const STYLES = {
  light: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
  dark:  "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
};

const COLOR_STEPS = [
  [0,   "#ef4444"],
  [25,  "#f97316"],
  [50,  "#f59e0b"],
  [75,  "#84cc16"],
  [100, "#22c55e"],
];

function scoreToColor(steps) {
  return ["interpolate", ["linear"], ["get", "__score"]].concat(
    steps.flatMap(([v, c]) => [v, c])
  );
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
      "fill-color": scoreToColor(COLOR_STEPS),
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
    id: "quartiers-selected",
    type: "line",
    source: "quartiers",
    filter: ["==", "quartier_id", ""],
    paint: {
      "line-color": "#3b82f6",
      "line-width": 3,
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
    if (!props.quartier_id) return;

    onQuartierClick({
      quartier_id:    props.quartier_id,
      quartier_code:  props.quartier_code,
      nom:            props.nom,
      arrondissement: props.arrondissement,
    });

    map.setFilter("quartiers-selected", ["==", "quartier_id", props.quartier_id]);

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
      onReady?.();
    } else {
      setTimeout(waitForStyle, 80);
    }
  };

  map.setStyle(dark ? STYLES.dark : STYLES.light);
  setTimeout(waitForStyle, 120);
}

export function updateMapData(geojson, indicateur, indicateurLabel) {
  const features = geojson.features.map((f) => ({
    ...f,
    properties: {
      ...f.properties,
      __score: f.properties[indicateur] ?? null,
      __indicateur_label: indicateurLabel,
    },
  }));

  currentGeoJSON = { type: "FeatureCollection", features };

  if (!map || !map.isStyleLoaded()) return;
  const src = map.getSource("quartiers");
  if (src) src.setData(currentGeoJSON);
}

export function flyToLngLat(lng, lat, zoom = 14) {
  map?.flyTo({ center: [lng, lat], zoom });
}

export function placeMarker(lng, lat, label) {
  if (!map) return;
  new maplibregl.Marker({ color: "#3b82f6" })
    .setLngLat([lng, lat])
    .setPopup(new maplibregl.Popup().setText(label))
    .addTo(map)
    .togglePopup();
}
