import { geocodeAddress } from "./api.js";
import { flyToLngLat, placeMarker } from "./map.js";

let debounceTimer = null;

export function initGeocode() {
  const input   = document.getElementById("geocode-input");
  const results = document.getElementById("geocode-results");

  input.addEventListener("input", () => {
    clearTimeout(debounceTimer);
    const q = input.value.trim();
    if (q.length < 3) {
      results.classList.add("hidden");
      results.innerHTML = "";
      return;
    }
    debounceTimer = setTimeout(async () => {
      const features = await geocodeAddress(q);
      if (!features.length) {
        results.classList.add("hidden");
        return;
      }
      results.innerHTML = features
        .map((f, i) => `<li data-i="${i}">${f.properties.label}</li>`)
        .join("");
      results.classList.remove("hidden");

      results.querySelectorAll("li").forEach((li) => {
        li.addEventListener("click", () => {
          const f = features[+li.dataset.i];
          const [lng, lat] = f.geometry.coordinates;
          flyToLngLat(lng, lat);
          placeMarker(lng, lat, f.properties.label);
          input.value = f.properties.label;
          results.classList.add("hidden");
        });
      });
    }, 300);
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest("#geocode-box")) results.classList.add("hidden");
  });
}
