import { Chart } from "chart.js/auto";
import { fetchKPIs, fetchTimeline } from "./api.js";

let timelineChart = null;

function fmt(v, unit = "") {
  return v != null ? `${Number(v).toLocaleString("fr-FR")}${unit}` : "—";
}

function normalizeForDisplay(value, scale) {
  if (value == null) return 0;
  const min = scale?.min ?? 0;
  const max = scale?.max ?? 100;
  if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) return 50;
  const pct = ((Number(value) - min) / (max - min)) * 100;
  return Math.max(0, Math.min(100, pct));
}

function scoreColor(pct) {
  if (pct >= 75) return "#22c55e";
  if (pct >= 50) return "#84cc16";
  if (pct >= 25) return "#f59e0b";
  if (pct > 0) return "#f97316";
  return "#ef4444";
}

function scoreCard(label, value, scale, fullWidth = false) {
  const pct = normalizeForDisplay(value, scale);
  const color = scoreColor(pct);
  const displayValue = value != null ? Math.round(value) : "—";
  return `
    <div class="score-card${fullWidth ? " full-width" : ""}">
      <div class="score-card-label">${label}</div>
      <div class="score-card-value" style="color:${color}">${displayValue}</div>
      <div class="score-track">
        <div class="score-fill" style="width:${pct}%;background:${color}"></div>
      </div>
    </div>`;
}

function detailSection(title, rows) {
  const id = title.replace(/\s+/g, "-").toLowerCase();
  return `
    <div class="detail-section">
      <button type="button" class="detail-toggle" data-target="${id}">
        ${title}
        <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="6 9 12 15 18 9"></polyline>
        </svg>
      </button>
      <div class="detail-body" id="${id}">
        ${rows.map(([label, val]) => `
          <div class="kpi-row"><span>${label}</span><b>${val}</b></div>`).join("")}
      </div>
    </div>`;
}

export async function openSidebar(quartierSelection, annee, getIndicatorScale) {
  const quartierId = typeof quartierSelection === "object"
    ? quartierSelection.quartier_id
    : quartierSelection;
  const quartierMeta = typeof quartierSelection === "object"
    ? quartierSelection
    : { quartier_id: quartierSelection };

  const content = document.getElementById("sidebar-content");
  content.innerHTML = `<p class="loading">Chargement…</p>`;

  try {
    const [kpis, timeline] = await Promise.all([
      fetchKPIs(quartierId, annee),
      fetchTimeline(quartierId),
    ]);

    const nom = kpis.nom || timeline.nom || quartierMeta.nom || "Quartier administratif";
    const arr = kpis.arrondissement || timeline.arrondissement || quartierMeta.arrondissement;
    const code = kpis.quartier_code || timeline.quartier_code || quartierMeta.quartier_code;
    const metaLigne = [
      `Données ${annee}`,
      arr ? `${arr}e arrondissement` : null,
      code ? `#${code}` : null,
    ].filter(Boolean).join(" · ");
    const scales = {
      score_global: getIndicatorScale?.("score_global"),
      score_qualite_vie: getIndicatorScale?.("score_qualite_vie"),
      score_transports: getIndicatorScale?.("score_transports"),
      score_loisirs: getIndicatorScale?.("score_loisirs"),
      score_services: getIndicatorScale?.("score_services"),
    };

    content.innerHTML = `
      <div class="q-header">
        <div class="q-name">${nom}</div>
        <div class="q-meta">${metaLigne}</div>
      </div>

      <div class="tabs">
        <button type="button" class="tab active" data-tab="scores">Scores</button>
        <button type="button" class="tab" data-tab="details">Détails</button>
        <button type="button" class="tab" data-tab="tendance">Tendance</button>
      </div>

      <!-- Tab: Scores -->
      <div class="tab-panel active" id="tab-scores">
        <div class="scores-grid">
          ${scoreCard("Score global",      kpis.score_global,      scales.score_global, true)}
          ${scoreCard("Qualité de vie",    kpis.score_qualite_vie, scales.score_qualite_vie)}
          ${scoreCard("Transports",        kpis.score_transports,  scales.score_transports)}
          ${scoreCard("Loisirs",           kpis.score_loisirs,     scales.score_loisirs)}
          ${scoreCard("Services publics",  kpis.score_services,    scales.score_services)}
        </div>
      </div>

      <!-- Tab: Détails -->
      <div class="tab-panel" id="tab-details">
        ${detailSection("Immobilier", [
          ["Prix m² médian",       fmt(kpis.prix_m2_median, " €")],
          ["Logements sociaux",    fmt(kpis.nb_logements_sociaux)],
          ["Part logements sociaux", fmt(kpis.pct_logements_sociaux, " %")],
        ])}
        ${detailSection("Qualité de vie", [
          ["Espaces verts",       fmt(kpis.nb_espaces_verts)],
          ["Arbres",              fmt(kpis.nb_arbres)],
          ["Sanisettes",          fmt(kpis.nb_sanisettes)],
          ["Chantiers actifs",    fmt(kpis.nb_chantiers_actifs)],
          ["Anomalies signalées", fmt(kpis.nb_anomalies)],
          ["Couverture fibre",    fmt(kpis.pct_fibre, " %")],
        ])}
        ${detailSection("Transports", [
          ["Gares",                  fmt(kpis.nb_gares)],
          ["Stations Vélib",         fmt(kpis.nb_stations_velib)],
          ["Capacité Vélib totale",  fmt(kpis.capacite_velib_totale)],
          ["Lignes distinctes",      fmt(kpis.nb_lignes_transport)],
          ["Lignes par gare",        fmt(kpis.lignes_par_gare_moyen)],
          ["Modes lourds présents",  fmt(kpis.nb_modes_lourds)],
          ["Arrêts de bus",          fmt(kpis.nb_arrets_bus)],
          ["Arrêts accessibles",     fmt(kpis.pct_arrets_accessibles, " %")],
          ["Flux total",             fmt(kpis.flux_multimodal)],
          ["Flux vélo/trottinette",  fmt(kpis.flux_velo_trott)],
          ["Flux bus",               fmt(kpis.flux_bus)],
          ["Flux motorisé",          fmt(kpis.flux_motorise)],
          ["Part vélo/trottinette",  fmt(kpis.pct_flux_velo_trott, " %")],
          ["Part motorisée",         fmt(kpis.pct_flux_motorise, " %")],
          ["Part voies cyclables",   fmt(kpis.pct_flux_voie_cyclable, " %")],
        ])}
        ${detailSection("Loisirs", [
          ["Événements", fmt(kpis.nb_evenements)],
          ["Cinémas",    fmt(kpis.nb_cinemas)],
          ["Terrasses",  fmt(kpis.nb_terrasses)],
          ["Musées",     fmt(kpis.nb_musees)],
        ])}
        ${detailSection("Services publics", [
          ["Écoles élémentaires",  fmt(kpis.nb_ecoles)],
          ["Collèges",             fmt(kpis.nb_colleges)],
          ["Bibliothèques",        fmt(kpis.nb_bibliotheques)],
          ["Bureaux de poste",     fmt(kpis.nb_bureaux_poste)],
          ["Enseignement sup.",    fmt(kpis.nb_ensup)],
        ])}
      </div>

      <!-- Tab: Tendance -->
      <div class="tab-panel" id="tab-tendance">
        <div class="timeline-wrap">
          <canvas id="timeline-chart"></canvas>
          <p class="loading" id="timeline-empty" style="display:none">
            Aucune série historique disponible.
          </p>
        </div>
      </div>
    `;

    bindTabs(content);
    bindAccordions(content);
    renderTimeline(timeline);

  } catch (e) {
    content.innerHTML = `<p class="error">Erreur : ${e.message}</p>`;
  }
}

function bindTabs(root) {
  const tabs   = root.querySelectorAll(".tab");
  const panels = root.querySelectorAll(".tab-panel");
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.classList.remove("active"));
      panels.forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      root.querySelector(`#tab-${tab.dataset.tab}`).classList.add("active");
    });
  });
}

function bindAccordions(root) {
  root.querySelectorAll(".detail-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const body = root.querySelector(`#${btn.dataset.target}`);
      const open = btn.classList.toggle("open");
      body.classList.toggle("open", open);
    });
  });
}

function renderTimeline(timeline) {
  if (timelineChart) { timelineChart.destroy(); timelineChart = null; }

  const years  = timeline.points?.map((p) => p.annee) ?? [];
  const scores = timeline.points?.map((p) => p.score_global) ?? [];
  const canvas  = document.getElementById("timeline-chart");
  const empty   = document.getElementById("timeline-empty");

  if (!years.length) {
    if (canvas) canvas.style.display = "none";
    if (empty)  empty.style.display = "block";
    return;
  }

  timelineChart = new Chart(canvas, {
    type: "line",
    data: {
      labels: years,
      datasets: [{
        label: "Score global",
        data: scores,
        borderColor: "#3b82f6",
        backgroundColor: "rgba(59,130,246,0.12)",
        tension: 0.35,
        fill: true,
        pointRadius: 3,
        pointBackgroundColor: "#3b82f6",
      }],
    },
    options: {
      plugins: { legend: { display: false } },
      scales: {
        y: {
          min: 0, max: 100,
          grid: { color: "rgba(255,255,255,0.05)" },
          ticks: { color: "#475569", font: { size: 10 } },
        },
        x: {
          grid: { display: false },
          ticks: { color: "#475569", font: { size: 10 } },
        },
      },
    },
  });
}

export function closeSidebar() {
  const content = document.getElementById("sidebar-content");
  content.innerHTML = `
    <div class="sidebar-placeholder">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path>
      </svg>
      <p>Cliquez sur un quartier<br/>pour explorer ses données</p>
    </div>`;
  if (timelineChart) { timelineChart.destroy(); timelineChart = null; }
}
