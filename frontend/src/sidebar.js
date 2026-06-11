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

export async function openSidebar(quartierSelection, annee) {
  const quartierId = typeof quartierSelection === "object" ? quartierSelection.quartier_id : quartierSelection;
  const quartierMeta = typeof quartierSelection === "object" ? quartierSelection : { quartier_id: quartierSelection };
  const content = document.getElementById("sidebar-content");

  content.innerHTML = `<p class="loading">Chargement…</p>`;

  try {
    const [kpis, timeline] = await Promise.all([
      fetchKPIs(quartierId, annee),
      fetchTimeline(quartierId),
    ]);

    const nom = kpis.nom || timeline.nom || quartierMeta.nom || "Quartier administratif";
    const code = kpis.quartier_code || timeline.quartier_code || quartierMeta.quartier_code;
    const arr = kpis.arrondissement || timeline.arrondissement || quartierMeta.arrondissement;
    const metaLigne = [
      `Données ${annee}`,
      code ? `Code ${code}` : null,
      arr ? `${arr}e arrondissement` : null,
    ].filter(Boolean).join(" · ");

    content.innerHTML = `
      <h2>${nom}</h2>
      <p class="meta">${metaLigne}</p>

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
        <div class="kpi-row"><span>Capacité Vélib totale</span><b>${fmt(kpis.capacite_velib_totale)}</b></div>
        <div class="kpi-row"><span>Lignes distinctes</span><b>${fmt(kpis.nb_lignes_transport)}</b></div>
        <div class="kpi-row"><span>Lignes par gare</span><b>${fmt(kpis.lignes_par_gare_moyen)}</b></div>
        <div class="kpi-row"><span>Modes lourds présents</span><b>${fmt(kpis.nb_modes_lourds)}</b></div>
        <div class="kpi-row"><span>Arrêts de bus</span><b>${fmt(kpis.nb_arrets_bus)}</b></div>
        <div class="kpi-row"><span>Arrêts accessibles</span><b>${fmt(kpis.pct_arrets_accessibles, " %")}</b></div>
        <div class="kpi-row"><span>Flux total</span><b>${fmt(kpis.flux_multimodal)}</b></div>
        <div class="kpi-row"><span>Flux vélo / trottinette</span><b>${fmt(kpis.flux_velo_trott)}</b></div>
        <div class="kpi-row"><span>Flux bus</span><b>${fmt(kpis.flux_bus)}</b></div>
        <div class="kpi-row"><span>Flux motorisé</span><b>${fmt(kpis.flux_motorise)}</b></div>
        <div class="kpi-row"><span>Part vélo / trottinette</span><b>${fmt(kpis.pct_flux_velo_trott, " %")}</b></div>
        <div class="kpi-row"><span>Part motorisée</span><b>${fmt(kpis.pct_flux_motorise, " %")}</b></div>
        <div class="kpi-row"><span>Part voies cyclables</span><b>${fmt(kpis.pct_flux_voie_cyclable, " %")}</b></div>
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
    const chartNode = document.getElementById("timeline-chart");
    if (years.length) {
      timelineChart = new Chart(chartNode, {
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
    } else if (chartNode) {
      chartNode.outerHTML = `<p class="meta">Aucune série historique disponible pour ce quartier.</p>`;
    }

  } catch (e) {
    content.innerHTML = `<p class="error">Erreur : ${e.message}</p>`;
  }
}

export function closeSidebar() {
  document.getElementById("sidebar-content").innerHTML = "";
  if (timelineChart) { timelineChart.destroy(); timelineChart = null; }
}
