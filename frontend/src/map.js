import maplibregl from "maplibre-gl";

let map = null;
let popup = null;

// Palette de couleurs : vert → jaune → rouge (score 0-100)
const COLOR_STEPS = [
  [0,   "#d73027"],
  [25,  "#f46d43"],
  [50,  "#fdae61"],
  [75,  "#a6d96a"],
  [100, "#1a9641"],
];

function scoreToColor(steps) {
  return ["interpolate", ["linear"], ["get", "__score"]].concat(
    steps.flatMap(([v, c]) => [v, c])
  );
}

export function initMap(onArrClick) {
  map = new maplibregl.Map({
    container: "map",
    style: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
    center: [2.347, 48.859],
    zoom: 11.5,
  });

  map.addControl(new maplibregl.NavigationControl(), "top-left");

  popup = new maplibregl.Popup({
    closeButton: false,
    closeOnClick: false,
    className: "map-popup",
  });

  map.on("load", () => {
    map.addSource("arrondissements", {
      type: "geojson",
      data: { type: "FeatureCollection", features: [] },
    });

    map.addLayer({
      id: "arrondissements-fill",
      type: "fill",
      source: "arrondissements",
      paint: {
        "fill-color": scoreToColor(COLOR_STEPS),
        "fill-opacity": 0.75,
      },
    });

    map.addLayer({
      id: "arrondissements-border",
      type: "line",
      source: "arrondissements",
      paint: {
        "line-color": "#ffffff",
        "line-width": 1.5,
      },
    });

    map.addLayer({
      id: "arrondissements-selected",
      type: "line",
      source: "arrondissements",
      filter: ["==", "arrondissement", -1],
      paint: {
        "line-color": "#0066ff",
        "line-width": 3,
      },
    });
  });

  // Hover tooltip
  map.on("mousemove", "arrondissements-fill", (e) => {
    if (!e.features.length) return;
    map.getCanvas().style.cursor = "pointer";
    const props = e.features[0].properties;
    const score = props.__score != null ? Number(props.__score).toFixed(1) : "—";
    popup
      .setLngLat(e.lngLat)
      .setHTML(
        `<strong>${props.nom || props.arrondissement + "e arrondissement"}</strong><br/>
         ${props.__indicateur_label || "Score"} : <b>${score}</b>`
      )
      .addTo(map);
  });

  map.on("mouseleave", "arrondissements-fill", () => {
    map.getCanvas().style.cursor = "";
    popup.remove();
  });

  // Click → sidebar
  map.on("click", "arrondissements-fill", (e) => {
    const arr = e.features[0].properties.arrondissement;
    if (arr) onArrClick(arr);
    map.setFilter("arrondissements-selected", ["==", "arrondissement", arr]);
  });

  return map;
}

export function updateMapData(geojson, indicateur, indicateurLabel) {
  if (!map || !map.isStyleLoaded()) return;

  // Injecte un champ __score normalisé pour la couleur
  const features = geojson.features.map((f) => ({
    ...f,
    properties: {
      ...f.properties,
      __score: f.properties[indicateur] ?? null,
      __indicateur_label: indicateurLabel,
    },
  }));

  map.getSource("arrondissements").setData({ type: "FeatureCollection", features });
}

export function flyToLngLat(lng, lat, zoom = 14) {
  map?.flyTo({ center: [lng, lat], zoom });
}

export function placeMarker(lng, lat, label) {
  if (!map) return;
  new maplibregl.Marker({ color: "#0066ff" })
    .setLngLat([lng, lat])
    .setPopup(new maplibregl.Popup().setText(label))
    .addTo(map)
    .togglePopup();
}
