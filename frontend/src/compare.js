import { Chart } from "chart.js/auto";
import { fetchCompare, fetchGeoJSON, fetchKPIs } from "./api.js";
import { setCompareHighlights } from "./map.js";

let radarChart = null;
let quartierOptionsCache = { year: null, byArr: new Map() };
let irisOptionsCache = { year: null, byQuartier: new Map() };

const ARRONDISSEMENTS = Array.from({ length: 20 }, (_, i) => i + 1);
const LABELS = ["Qualité de vie", "Transports", "Loisirs", "Services publics", "Global"];
const KEYS = ["score_qualite_vie", "score_transports", "score_loisirs", "score_services", "score_global"];

function formatArrLabel(arr) {
  return `${arr}${arr === 1 ? "er" : "e"} arrondissement`;
}

function fmt(v) {
  return v != null ? Number(v).toFixed(1) : "—";
}

function populateArrondissementSelects() {
  ["compare-arr1", "compare-arr2"].forEach((id, idx) => {
    const sel = document.getElementById(id);
    sel.innerHTML = ARRONDISSEMENTS.map(
      (arr) => `<option value="${arr}">${formatArrLabel(arr)}</option>`
    ).join("");
    sel.value = idx === 0 ? 1 : 2;
  });
}

async function ensureQuartierOptions(annee) {
  if (quartierOptionsCache.year === annee && quartierOptionsCache.byArr.size) {
    return quartierOptionsCache.byArr;
  }

  const geojson = await fetchGeoJSON(annee, "score_global");
  const byArr = new Map();

  for (const feature of geojson.features ?? []) {
    const arr = Number(feature.properties?.arrondissement);
    if (!Number.isInteger(arr)) continue;
    if (!byArr.has(arr)) byArr.set(arr, []);
    byArr.get(arr).push({
      quartier_id: feature.properties?.quartier_id,
      quartier_code: feature.properties?.quartier_code,
      nom: feature.properties?.nom,
      arrondissement: arr,
    });
  }

  for (const [arr, items] of byArr.entries()) {
    items.sort((a, b) => (a.nom || "").localeCompare(b.nom || "", "fr"));
  }

  quartierOptionsCache = { year: annee, byArr };
  return byArr;
}

function populateQuartierSelect(selectId, arr, byArr) {
  const sel = document.getElementById(selectId);
  const items = byArr.get(arr) ?? [];
  sel.innerHTML = [
    `<option value="">Tout l'arrondissement</option>`,
    ...items.map((item) => `<option value="${item.quartier_id}">${item.nom}</option>`),
  ].join("");
  sel.disabled = items.length === 0;
}

async function ensureIrisOptions(annee) {
  if (irisOptionsCache.year === annee && irisOptionsCache.byQuartier.size) {
    return irisOptionsCache.byQuartier;
  }

  const geojson = await fetchGeoJSON(annee, "score_global", "iris");
  const byQuartier = new Map();

  for (const feature of geojson.features ?? []) {
    const qid = feature.properties?.quartier_id;
    if (!qid) continue;
    if (!byQuartier.has(qid)) byQuartier.set(qid, []);
    byQuartier.get(qid).push({
      iris_id: feature.properties?.iris_id,
      iris_code: feature.properties?.iris_code,
      nom: feature.properties?.nom,
      quartier_id: qid,
    });
  }

  for (const [, items] of byQuartier.entries()) {
    items.sort((a, b) => (a.nom || "").localeCompare(b.nom || "", "fr", { numeric: true }));
  }

  irisOptionsCache = { year: annee, byQuartier };
  return byQuartier;
}

function populateIrisSelect(selectId, quartierId, byQuartier) {
  const sel = document.getElementById(selectId);
  if (!sel) return;
  const items = quartierId ? (byQuartier.get(quartierId) ?? []) : [];
  const hasItems = items.length > 0;
  sel.innerHTML = [
    `<option value="">Tout le quartier</option>`,
    ...items.map((item) => `<option value="${item.iris_id}">${item.nom}</option>`),
  ].join("");
  sel.disabled = !hasItems;
  sel.classList.toggle("hidden", !quartierId);
}

function getSelectionState() {
  const arr1 = +document.getElementById("compare-arr1").value;
  const arr2 = +document.getElementById("compare-arr2").value;
  const quartier1 = document.getElementById("compare-quartier1").value || null;
  const quartier2 = document.getElementById("compare-quartier2").value || null;
  const iris1 = document.getElementById("compare-iris1")?.value || null;
  const iris2 = document.getElementById("compare-iris2")?.value || null;

  return { arr1, arr2, quartier1, quartier2, iris1, iris2 };
}

function buildCompareContext(selection, byArr, byQuartier = new Map()) {
  const { arr1, arr2, quartier1, quartier2, iris1, iris2 } = selection;
  const hasQuartier1 = Boolean(quartier1);
  const hasQuartier2 = Boolean(quartier2);
  const hasIris1 = Boolean(iris1);
  const hasIris2 = Boolean(iris2);

  if (hasIris1 !== hasIris2) {
    throw new Error("Choisissez soit deux IRIS, soit deux zones de même niveau.");
  }

  if (!hasIris1 && hasQuartier1 !== hasQuartier2) {
    throw new Error("Choisissez soit deux arrondissements, soit deux quartiers administratifs.");
  }

  if (!hasQuartier1 && !hasIris1 && arr1 === arr2) {
    throw new Error("Choisissez deux arrondissements différents.");
  }

  if (hasQuartier1 && !hasIris1 && quartier1 === quartier2) {
    throw new Error("Choisissez deux quartiers administratifs différents.");
  }

  if (hasIris1 && iris1 === iris2) {
    throw new Error("Choisissez deux IRIS différents.");
  }

  const meta1 = (byArr.get(arr1) ?? []).find((item) => item.quartier_id === quartier1) ?? null;
  const meta2 = (byArr.get(arr2) ?? []).find((item) => item.quartier_id === quartier2) ?? null;
  const irisMeta1 = hasIris1 ? (byQuartier.get(quartier1) ?? []).find((item) => item.iris_id === iris1) ?? null : null;
  const irisMeta2 = hasIris2 ? (byQuartier.get(quartier2) ?? []).find((item) => item.iris_id === iris2) ?? null : null;

  if (hasIris1) {
    return {
      mode: "iris",
      left: {
        type: "iris",
        id: iris1,
        label: irisMeta1?.nom || "IRIS",
        sublabel: meta1?.nom ? `${meta1.nom} · ${formatArrLabel(arr1)}` : formatArrLabel(arr1),
        arrondissement: arr1,
        quartierId: quartier1,
        irisId: iris1,
        irisCode: irisMeta1?.iris_code || null,
      },
      right: {
        type: "iris",
        id: iris2,
        label: irisMeta2?.nom || "IRIS",
        sublabel: meta2?.nom ? `${meta2.nom} · ${formatArrLabel(arr2)}` : formatArrLabel(arr2),
        arrondissement: arr2,
        quartierId: quartier2,
        irisId: iris2,
        irisCode: irisMeta2?.iris_code || null,
      },
    };
  }

  const mode = hasQuartier1 ? "quartier" : "arrondissement";
  return {
    mode,
    left: hasQuartier1
      ? {
          type: "quartier",
          id: quartier1,
          label: meta1?.nom || "Quartier administratif",
          sublabel: formatArrLabel(arr1),
          arrondissement: arr1,
          quartierId: quartier1,
          quartierCode: meta1?.quartier_code || null,
        }
      : {
          type: "arrondissement",
          id: arr1,
          label: formatArrLabel(arr1),
          sublabel: "Vue globale",
          arrondissement: arr1,
          quartierId: null,
          quartierCode: null,
        },
    right: hasQuartier2
      ? {
          type: "quartier",
          id: quartier2,
          label: meta2?.nom || "Quartier administratif",
          sublabel: formatArrLabel(arr2),
          arrondissement: arr2,
          quartierId: quartier2,
          quartierCode: meta2?.quartier_code || null,
        }
      : {
          type: "arrondissement",
          id: arr2,
          label: formatArrLabel(arr2),
          sublabel: "Vue globale",
          arrondissement: arr2,
          quartierId: null,
          quartierCode: null,
        },
  };
}

function updateQuartierSelects(byArr) {
  const { arr1, arr2 } = getSelectionState();
  populateQuartierSelect("compare-quartier1", arr1, byArr);
  populateQuartierSelect("compare-quartier2", arr2, byArr);
}

async function updateIrisSelects(byQuartier) {
  const { quartier1, quartier2 } = getSelectionState();
  populateIrisSelect("compare-iris1", quartier1, byQuartier);
  populateIrisSelect("compare-iris2", quartier2, byQuartier);
}

function syncQuartierSelects(byArr, previousSelection) {
  const quartier1Sel = document.getElementById("compare-quartier1");
  const quartier2Sel = document.getElementById("compare-quartier2");
  updateQuartierSelects(byArr);
  if (previousSelection?.quartier1) quartier1Sel.value = previousSelection.quartier1;
  if (previousSelection?.quartier2) quartier2Sel.value = previousSelection.quartier2;
}

async function loadComparisonOptions(annee) {
  const previousSelection = getSelectionState();
  const byArr = await ensureQuartierOptions(annee);
  syncQuartierSelects(byArr, previousSelection);

  const byQuartier = await ensureIrisOptions(annee);
  await updateIrisSelects(byQuartier);
  if (previousSelection?.iris1) {
    const el = document.getElementById("compare-iris1");
    if (el) { el.value = previousSelection.iris1; el.classList.remove("hidden"); }
  }
  if (previousSelection?.iris2) {
    const el = document.getElementById("compare-iris2");
    if (el) { el.value = previousSelection.iris2; el.classList.remove("hidden"); }
  }

  return { byArr, byQuartier };
}

async function fetchComparisonData(context, annee) {
  if (context.mode === "arrondissement") {
    const data = await fetchCompare(context.left.id, context.right.id, annee);
    return {
      left: data.arrondissement_1,
      right: data.arrondissement_2,
    };
  }

  if (context.mode === "iris") {
    const [left, right] = await Promise.all([
      fetchKPIs(context.left.id, annee, "iris"),
      fetchKPIs(context.right.id, annee, "iris"),
    ]);
    return { left, right };
  }

  const [left, right] = await Promise.all([
    fetchKPIs(context.left.id, annee, "quartier"),
    fetchKPIs(context.right.id, annee, "quartier"),
  ]);
  return { left, right };
}

function renderRadar(data, context) {
  const scores1 = KEYS.map((key) => data.left[key] ?? 0);
  const scores2 = KEYS.map((key) => data.right[key] ?? 0);

  if (radarChart) radarChart.destroy();

  radarChart = new Chart(document.getElementById("compare-radar"), {
    type: "radar",
    data: {
      labels: LABELS,
      datasets: [
        {
          label: context.left.label,
          data: scores1,
          borderColor: "#3b82f6",
          backgroundColor: "rgba(59,130,246,0.18)",
          pointBackgroundColor: "#3b82f6",
          borderWidth: 2,
        },
        {
          label: context.right.label,
          data: scores2,
          borderColor: "#ec4899",
          backgroundColor: "rgba(236,72,153,0.14)",
          pointBackgroundColor: "#ec4899",
          borderWidth: 2,
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      plugins: {
        legend: {
          labels: {
            color: "#f1f5f9",
            font: { size: 13, weight: "600" },
            padding: 18,
          },
        },
      },
      scales: {
        r: {
          min: 0,
          max: 100,
          ticks: {
            stepSize: 25,
            color: "#94a3b8",
            backdropColor: "transparent",
            font: { size: 11 },
          },
          angleLines: { color: "rgba(148,163,184,0.18)" },
          grid: { color: "rgba(148,163,184,0.18)" },
          pointLabels: {
            color: (ctx) => ctx.index === LABELS.length - 1 ? "#f8fafc" : "#cbd5e1",
            font: (ctx) => ctx.index === LABELS.length - 1
              ? { size: 15, weight: "700" }
              : { size: 12, weight: "600" },
          },
        },
      },
    },
  });
}

function renderSummary(data, context) {
  const score1 = data.left.score_global;
  const score2 = data.right.score_global;
  const winner1 = (score1 ?? -Infinity) >= (score2 ?? -Infinity);
  const winner2 = (score2 ?? -Infinity) >= (score1 ?? -Infinity);

  document.getElementById("compare-summary").innerHTML = `
    <div class="compare-score${winner1 ? " is-best arr1" : " arr1"}">
      <span class="compare-score-label">${context.left.label}</span>
      <strong>${fmt(score1)}</strong>
      <small>${context.left.sublabel}</small>
    </div>
    <div class="compare-score${winner2 ? " is-best arr2" : " arr2"}">
      <span class="compare-score-label">${context.right.label}</span>
      <strong>${fmt(score2)}</strong>
      <small>${context.right.sublabel}</small>
    </div>
  `;
  document.getElementById("compare-explain").innerHTML = `
    <strong>Global</strong> correspond ici a la <strong>moyenne</strong> des 4 indicateurs
    (qualite de vie, transports, loisirs, services publics), pas a leur somme.
  `;
}

function renderTable(data, context) {
  const rows = KEYS.map((key, idx) => {
    const v1 = data.left[key];
    const v2 = data.right[key];
    const winner = v1 > v2 ? "arr1" : v2 > v1 ? "arr2" : "";
    const isGlobal = key === "score_global";
    return `<tr>
      <td>${isGlobal ? "Score global (moyenne)" : LABELS[idx]}</td>
      <td class="${winner === "arr1" ? "win" : ""}">${fmt(v1)}</td>
      <td class="${winner === "arr2" ? "win" : ""}">${fmt(v2)}</td>
    </tr>`;
  });
  const groupedRows = [
    ...rows.slice(0, 4),
    rows[4].replace("<tr>", `<tr class="global-row">`),
  ];

  document.getElementById("compare-table").innerHTML = `
    <table class="compare-table">
      <thead><tr>
        <th>Indicateur</th>
        <th>${context.left.label}</th>
        <th>${context.right.label}</th>
      </tr></thead>
      <tbody>${groupedRows.join("")}</tbody>
    </table>`;
}

function renderSelectionDetails(context) {
  const content = document.getElementById("compare-quartiers-content");
  const leftSub = context.left.sublabel
    + (context.left.quartierCode ? ` · #${context.left.quartierCode}` : "")
    + (context.left.irisCode ? ` · IRIS ${context.left.irisCode}` : "");
  const rightSub = context.right.sublabel
    + (context.right.quartierCode ? ` · #${context.right.quartierCode}` : "")
    + (context.right.irisCode ? ` · IRIS ${context.right.irisCode}` : "");
  content.innerHTML = `
    <div class="compare-quartier-group arr1">
      <h3>${context.left.label}</h3>
      <p>${leftSub}</p>
    </div>
    <div class="compare-quartier-group arr2">
      <h3>${context.right.label}</h3>
      <p>${rightSub}</p>
    </div>
  `;
}

function applyCompareHighlights(context) {
  if (context.mode === "iris") {
    setCompareHighlights({
      iris1: context.left.irisId,
      iris2: context.right.irisId,
    });
    return;
  }

  if (context.mode === "quartier") {
    setCompareHighlights({
      quartier1: context.left.quartierId,
      quartier2: context.right.quartierId,
    });
    return;
  }

  setCompareHighlights({
    arr1: context.left.arrondissement,
    arr2: context.right.arrondissement,
  });
}

export function initCompare(anneeGetter, clearCompareHighlights) {
  const btn = document.getElementById("compare-btn");
  const panel = document.getElementById("compare-panel");
  const close = document.getElementById("compare-close");
  const goBtn = document.getElementById("compare-go");
  const arr1Sel = document.getElementById("compare-arr1");
  const arr2Sel = document.getElementById("compare-arr2");
  const quartier1Sel = document.getElementById("compare-quartier1");
  const quartier2Sel = document.getElementById("compare-quartier2");

  populateArrondissementSelects();

  const refreshOptions = async () => {
    try {
      await loadComparisonOptions(anneeGetter());
    } catch (error) {
      document.getElementById("compare-table").innerHTML =
        `<p class="error">Erreur : ${error.message}</p>`;
    }
  };

  const refreshIrisOnly = async () => {
    try {
      const byQuartier = await ensureIrisOptions(anneeGetter());
      await updateIrisSelects(byQuartier);
    } catch (_) {}
  };

  btn.addEventListener("click", async () => {
    const willHide = !panel.classList.contains("hidden");
    panel.classList.toggle("hidden");
    if (willHide) {
      clearCompareHighlights?.();
      return;
    }
    await refreshOptions();
  });

  close.addEventListener("click", () => {
    panel.classList.add("hidden");
    clearCompareHighlights?.();
  });

  arr1Sel.addEventListener("change", refreshOptions);
  arr2Sel.addEventListener("change", refreshOptions);
  quartier1Sel?.addEventListener("change", refreshIrisOnly);
  quartier2Sel?.addEventListener("change", refreshIrisOnly);

  goBtn.addEventListener("click", async () => {
    try {
      const { byArr, byQuartier } = await loadComparisonOptions(anneeGetter());
      const context = buildCompareContext(getSelectionState(), byArr, byQuartier);
      const data = await fetchComparisonData(context, anneeGetter());
      applyCompareHighlights(context);
      renderSummary(data, context);
      renderRadar(data, context);
      renderTable(data, context);
      renderSelectionDetails(context);
    } catch (error) {
      document.getElementById("compare-summary").innerHTML = "";
      document.getElementById("compare-table").innerHTML =
        `<p class="error">Erreur : ${error.message}</p>`;
    }
  });

  refreshOptions();
}
