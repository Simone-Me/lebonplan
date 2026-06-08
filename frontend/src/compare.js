import { Chart } from "chart.js/auto";
import { fetchCompare } from "./api.js";

let radarChart = null;

const ARRONDISSEMENTS = Array.from({ length: 20 }, (_, i) => i + 1);
const LABELS = ["Qualité de vie", "Transports", "Loisirs", "Services", "Global"];
const KEYS   = ["score_qualite_vie", "score_transports", "score_loisirs", "score_services", "score_global"];

function populateSelects() {
  ["compare-arr1", "compare-arr2"].forEach((id, idx) => {
    const sel = document.getElementById(id);
    sel.innerHTML = ARRONDISSEMENTS.map(
      (a) => `<option value="${a}">${a}${a === 1 ? "er" : "e"}</option>`
    ).join("");
    sel.value = idx === 0 ? 1 : 2;
  });
}

export function initCompare(anneeGetter) {
  const btn    = document.getElementById("compare-btn");
  const panel  = document.getElementById("compare-panel");
  const close  = document.getElementById("compare-close");
  const goBtn  = document.getElementById("compare-go");

  populateSelects();

  btn.addEventListener("click", () => panel.classList.toggle("hidden"));
  close.addEventListener("click", () => panel.classList.add("hidden"));

  goBtn.addEventListener("click", async () => {
    const arr1  = +document.getElementById("compare-arr1").value;
    const arr2  = +document.getElementById("compare-arr2").value;
    const annee = anneeGetter();

    if (arr1 === arr2) {
      alert("Choisissez deux arrondissements différents.");
      return;
    }

    try {
      const data = await fetchCompare(arr1, arr2, annee);
      renderRadar(data, arr1, arr2);
      renderTable(data, arr1, arr2);
    } catch (e) {
      document.getElementById("compare-table").innerHTML =
        `<p class="error">Erreur : ${e.message}</p>`;
    }
  });
}

function renderRadar(data, arr1, arr2) {
  const scores1 = KEYS.map((k) => data.arrondissement_1[k] ?? 0);
  const scores2 = KEYS.map((k) => data.arrondissement_2[k] ?? 0);

  if (radarChart) radarChart.destroy();

  radarChart = new Chart(document.getElementById("compare-radar"), {
    type: "radar",
    data: {
      labels: LABELS,
      datasets: [
        {
          label: `${arr1}${arr1 === 1 ? "er" : "e"}`,
          data: scores1,
          borderColor: "#0066ff",
          backgroundColor: "rgba(0,102,255,0.2)",
        },
        {
          label: `${arr2}${arr2 === 1 ? "er" : "e"}`,
          data: scores2,
          borderColor: "#ff6600",
          backgroundColor: "rgba(255,102,0,0.2)",
        },
      ],
    },
    options: {
      scales: { r: { min: 0, max: 100, ticks: { stepSize: 25 } } },
    },
  });
}

function renderTable(data, arr1, arr2) {
  const fmt = (v) => (v != null ? Number(v).toFixed(1) : "—");
  const rows = KEYS.map((k, i) => {
    const v1 = data.arrondissement_1[k];
    const v2 = data.arrondissement_2[k];
    const winner = v1 > v2 ? "arr1" : v2 > v1 ? "arr2" : "";
    return `<tr>
      <td>${LABELS[i]}</td>
      <td class="${winner === "arr1" ? "win" : ""}">${fmt(v1)}</td>
      <td class="${winner === "arr2" ? "win" : ""}">${fmt(v2)}</td>
    </tr>`;
  });

  document.getElementById("compare-table").innerHTML = `
    <table class="compare-table">
      <thead><tr>
        <th>Indicateur</th>
        <th>${arr1}${arr1 === 1 ? "er" : "e"}</th>
        <th>${arr2}${arr2 === 1 ? "er" : "e"}</th>
      </tr></thead>
      <tbody>${rows.join("")}</tbody>
    </table>`;
}
