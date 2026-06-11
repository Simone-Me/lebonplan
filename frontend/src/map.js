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

export function initMap(onQuartierClick) {
  map = new maplibregl.Map({
    container: "map",
    style: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
    center: [2.347, 48.859],
    zoom: 11.5,
  });

  window.addEventListener("resize", () => map.resize());

  // ResizeObserver : redimensionne la carte dès que le conteneur change de taille
  const ro = new ResizeObserver(() => map.resize());
  ro.observe(document.getElementById("map-container"));

  map.addControl(new maplibregl.NavigationControl(), "top-left");

  popup = new maplibregl.Popup({
    closeButton: false,
    closeOnClick: false,
    className: "map-popup",
  });

  window._map = map;

  map.on("load", () => {
    map.resize();
    map.addSource("quartiers", {
      type: "geojson",
      data: { type: "FeatureCollection", features: [] },
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
        "line-width": 1.1,
      },
    });

    map.addLayer({
      id: "quartiers-selected",
      type: "line",
      source: "quartiers",
      filter: ["==", "quartier_id", ""],
      paint: {
        "line-color": "#0066ff",
        "line-width": 3,
      },
    });
  });

  // Hover tooltip
  map.on("mousemove", "quartiers-fill", (e) => {
    if (!e.features.length) return;
    map.getCanvas().style.cursor = "pointer";
    const props = e.features[0].properties;
    const score = props.__score != null ? Number(props.__score).toFixed(1) : "—";
    const suffixe = props.arrondissement ? `<br/>${props.arrondissement}e arrondissement` : "";
    popup
      .setLngLat(e.lngLat)
      .setHTML(
        `<strong>${props.nom || "Quartier administratif"}</strong>${suffixe}<br/>
         ${props.__indicateur_label || "Score"} : <b>${score}</b>`
      )
      .addTo(map);
  });

  map.on("mouseleave", "quartiers-fill", () => {
    map.getCanvas().style.cursor = "";
    popup.remove();
  });

  // Click → sidebar
  map.on("click", "quartiers-fill", (e) => {
    const props = e.features[0].properties;
    if (props.quartier_id) {
      onQuartierClick({
        quartier_id: props.quartier_id,
        quartier_code: props.quartier_code,
        nom: props.nom,
        arrondissement: props.arrondissement,
      });
      map.setFilter("quartiers-selected", ["==", "quartier_id", props.quartier_id]);
    }
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

  map.getSource("quartiers").setData({ type: "FeatureCollection", features });
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
