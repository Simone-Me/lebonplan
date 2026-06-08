import { Chart } from "chart.js/auto";
import { fetchKPIs, fetchTimeline } from "./api.js";

let timelineChart = null;

const INDICATEURS = [
  { key: "score_global",       label: "Score global" },
  { key: "score_qualite_vie",  label: "Qualité de vie" },
  { key: "score_transports",   label: "Transports" },
  { key: "score_loisirs",      label: "Loisirs" },
  { key: "score_services",     label: "Services publics" },
];

function fmt(v, unit = "") {
  return v != null ? `${Number(v).toLocaleString("fr-FR")}${unit}` : "—";
}

function scoreBar(score) {
  const pct = Math.round(score ?? 0);
  const color = pct >= 66 ? "#1a9641" : pct >= 33 ? "#fdae61" : "#d73027";
  return `<div class="score-bar-wrap">
    <div class="score-bar" style="width:${pct}%;background:${color}"></div>
    <span>${pct}/100</span>
  </div>`;
}

export async function openSidebar(arrondissement, annee) {
  const sidebar = document.getElementById("sidebar");
  const content = document.getElementById("sidebar-content");
  sidebar.classList.remove("hidden");

  content.innerHTML = `<p class="loading">Chargement…</p>`;

  try {
    const [kpis, timeline] = await Promise.all([
      fetchKPIs(arrondissement, annee),
      fetchTimeline(arrondissement),
    ]);

    const nom = `${arrondissement}${arrondissement === 1 ? "er" : "e"} arrondissement`;

    content.innerHTML = `
      <h2>${nom}</h2>
      <p class="meta">Données ${annee}</p>

      <section class="kpi-section">
        <h3>Scores composites</h3>
        ${INDICATEURS.map(({ key, label }) => `
          <div class="kpi-row">
            <span class="kpi-label">${label}</span>
            ${scoreBar(kpis[key])}
          </div>`).join("")}
      </section>

      <section class="kpi-section">
        <h3>Immobilier</h3>
        <div class="kpi-row"><span>Prix m² médian</span><b>${fmt(kpis.prix_m2_median, " €")}</b></div>
        <div class="kpi-row"><span>Logements sociaux</span><b>${fmt(kpis.pct_logements_sociaux, " %")}</b></div>
        <div class="kpi-row"><span>Nb logements sociaux</span><b>${fmt(kpis.nb_logements_sociaux)}</b></div>
      </section>

      <section class="kpi-section">
        <h3>Qualité de vie</h3>
        <div class="kpi-row"><span>Espaces verts</span><b>${fmt(kpis.nb_espaces_verts)}</b></div>
        <div class="kpi-row"><span>Arbres</span><b>${fmt(kpis.nb_arbres)}</b></div>
        <div class="kpi-row"><span>Sanisettes</span><b>${fmt(kpis.nb_sanisettes)}</b></div>
        <div class="kpi-row"><span>Chantiers actifs</span><b>${fmt(kpis.nb_chantiers_actifs)}</b></div>
        <div class="kpi-row"><span>Anomalies signalées</span><b>${fmt(kpis.nb_anomalies)}</b></div>
        <div class="kpi-row"><span>Couverture fibre</span><b>${fmt(kpis.pct_fibre, " %")}</b></div>
      </section>

      <section class="kpi-section">
        <h3>Transports</h3>
        <div class="kpi-row"><span>Gares</span><b>${fmt(kpis.nb_gares)}</b></div>
        <div class="kpi-row"><span>Stations Vélib</span><b>${fmt(kpis.nb_stations_velib)}</b></div>
        <div class="kpi-row"><span>Flux multimodal</span><b>${fmt(kpis.flux_multimodal)}</b></div>
      </section>

      <section class="kpi-section">
        <h3>Loisirs</h3>
        <div class="kpi-row"><span>Événements</span><b>${fmt(kpis.nb_evenements)}</b></div>
        <div class="kpi-row"><span>Cinémas</span><b>${fmt(kpis.nb_cinemas)}</b></div>
        <div class="kpi-row"><span>Terrasses</span><b>${fmt(kpis.nb_terrasses)}</b></div>
        <div class="kpi-row"><span>Musées</span><b>${fmt(kpis.nb_musees)}</b></div>
      </section>

      <section class="kpi-section">
        <h3>Services publics</h3>
        <div class="kpi-row"><span>Écoles élémentaires</span><b>${fmt(kpis.nb_ecoles)}</b></div>
        <div class="kpi-row"><span>Collèges</span><b>${fmt(kpis.nb_colleges)}</b></div>
        <div class="kpi-row"><span>Bibliothèques</span><b>${fmt(kpis.nb_bibliotheques)}</b></div>
        <div class="kpi-row"><span>Bureaux de poste</span><b>${fmt(kpis.nb_bureaux_poste)}</b></div>
        <div class="kpi-row"><span>Enseignement supérieur</span><b>${fmt(kpis.nb_ensup)}</b></div>
      </section>

      <section class="kpi-section">
        <h3>Évolution du score global</h3>
        <canvas id="timeline-chart"></canvas>
      </section>
    `;

    // Timeline chart
    if (timelineChart) timelineChart.destroy();
    const years  = timeline.points.map((p) => p.annee);
    const scores = timeline.points.map((p) => p.score_global);

    timelineChart = new Chart(document.getElementById("timeline-chart"), {
      type: "line",
      data: {
        labels: years,
        datasets: [{
          label: "Score global",
          data: scores,
          borderColor: "#0066ff",
          backgroundColor: "rgba(0,102,255,0.1)",
          tension: 0.3,
          fill: true,
        }],
      },
      options: {
        plugins: { legend: { display: false } },
        scales: {
          y: { min: 0, max: 100, grid: { color: "#e5e7eb" } },
          x: { grid: { display: false } },
        },
      },
    });

  } catch (e) {
    content.innerHTML = `<p class="error">Erreur : ${e.message}</p>`;
  }
}

export function closeSidebar() {
  document.getElementById("sidebar").classList.add("hidden");
  if (timelineChart) { timelineChart.destroy(); timelineChart = null; }
}
