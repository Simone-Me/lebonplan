import { Chart } from "chart.js/auto";
import { fetchKPIs, fetchTimeline, fetchStreamingStatus } from "./api.js";

let timelineChart    = null;
let _streamingTimer  = null;

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

function yearBadge(dataAnnee, sliderAnnee) {
  if (!dataAnnee || !sliderAnnee || dataAnnee === sliderAnnee) return "";
  return `<span class="year-badge" title="Dernière donnée disponible : ${dataAnnee}">${dataAnnee}</span>`;
}

function detailSection(title, rows, dataAnnee, sliderAnnee) {
  const id = title.replace(/\s+/g, "-").toLowerCase();
  return `
    <div class="detail-section">
      <button type="button" class="detail-toggle" data-target="${id}">
        <span class="section-title-wrap">${title}${yearBadge(dataAnnee, sliderAnnee)}</span>
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

function startStreamingCountdown(lastUpdate, intervalSeconds) {
  if (_streamingTimer) clearInterval(_streamingTimer);

  const tick = () => {
    const el = document.getElementById("streaming-timer-value");
    if (!el) { clearInterval(_streamingTimer); _streamingTimer = null; return; }

    if (!lastUpdate) { el.textContent = "en attente"; return; }

    const nextMs = new Date(lastUpdate).getTime() + intervalSeconds * 1000;
    const remaining = nextMs - Date.now();

    if (remaining <= 0) {
      el.textContent = "imminent";
    } else {
      const m = Math.floor(remaining / 60000);
      const s = Math.floor((remaining % 60000) / 1000);
      el.textContent = `${m}m ${String(s).padStart(2, "0")}s`;
    }
  };

  tick();
  _streamingTimer = setInterval(tick, 1000);
}

export async function openSidebar(areaSelection, annee, getIndicatorScale, level = "quartier") {
  const areaId = typeof areaSelection === "object"
    ? areaSelection.area_id
      ?? areaSelection.iris_id
      ?? areaSelection.quartier_id
      ?? areaSelection.arrondissement
    : areaSelection;
  const areaMeta = typeof areaSelection === "object"
    ? areaSelection
    : { area_id: areaSelection, level };

  const content = document.getElementById("sidebar-content");
  content.innerHTML = `<p class="loading">Chargement…</p>`;

  try {
    const [kpis, timeline, streamingStatus] = await Promise.all([
      fetchKPIs(areaId, annee, level),
      fetchTimeline(areaId, level),
      fetchStreamingStatus().catch(() => null),
    ]);

    const nom = kpis.nom || kpis.iris_nom || timeline.nom || areaMeta.nom || "Zone";
    const arr = kpis.arrondissement || timeline.arrondissement || areaMeta.arrondissement;
    const quartierCode = kpis.quartier_code || timeline.quartier_code || areaMeta.quartier_code;
    const irisCode = kpis.iris_code || timeline.iris_code || areaMeta.iris_code;
    const irisType = kpis.iris_type || timeline.iris_type || areaMeta.iris_type;
    const levelLabel = level === "arrondissement"
      ? "Arrondissement"
      : level === "iris"
        ? "IRIS"
        : "Quartier administratif";
    const metaLigne = [
      levelLabel,
      `Données ${annee}`,
      arr ? `${arr}e arrondissement` : null,
      quartierCode ? `Q#${quartierCode}` : null,
      irisCode ? `IRIS ${irisCode}` : null,
      irisType ? `Type ${irisType}` : null,
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
        ${streamingStatus ? `
        <div class="streaming-status" title="Vélib · Sanisettes · Chantiers · Anomalies · Voies — rafraîchis toutes les 5 min via Kafka">
          <span class="streaming-dot"></span>
          <span>Données temps réel — prochain rafraîchissement dans <b id="streaming-timer-value">…</b></span>
        </div>` : ""}
        ${detailSection("Immobilier", [
          ["Prix m² médian",         fmt(kpis.prix_m2_median, " €")],
          ["Surface médiane",        fmt(kpis.surface_mediane, " m²")],
          ["Logements sociaux",      fmt(kpis.nb_logements_sociaux)],
          ["Part logements sociaux", fmt(kpis.pct_logements_sociaux, " %")],
          ["Revenu médian / UC",     kpis.revenu_median_uc != null
            ? `${Number(kpis.revenu_median_uc).toLocaleString("fr-FR")} €/an`
            : "—"],
          ["Effort achat (50 m²)",   kpis.taux_effort_achat != null
            ? `${Number(kpis.taux_effort_achat).toFixed(1)} ans de revenu`
            : "—"],
        ], kpis.annee_immo, annee)}
        <div class="detail-section">
          <button type="button" class="detail-toggle" data-target="repartition-logements">
            Répartition par surface
            <svg class="chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="6 9 12 15 18 9"></polyline>
            </svg>
          </button>
          <div class="detail-body" id="repartition-logements">
            <canvas id="donut-surfaces" style="max-height:200px;margin:8px 0"></canvas>
            <div class="kpi-row"><span>Studios / T1 (≤25 m²)</span><b>${fmt(kpis.nb_t1)}</b></div>
            <div class="kpi-row"><span>T2 (26–45 m²)</span><b>${fmt(kpis.nb_t2)}</b></div>
            <div class="kpi-row"><span>T3 (46–65 m²)</span><b>${fmt(kpis.nb_t3)}</b></div>
            <div class="kpi-row"><span>T4+ (≥66 m²)</span><b>${fmt(kpis.nb_t4plus)}</b></div>
            <div class="kpi-row"><span>Appartements</span><b>${fmt(kpis.nb_appartements)}</b></div>
            <div class="kpi-row"><span>Maisons</span><b>${fmt(kpis.nb_maisons)}</b></div>
          </div>
        </div>
        ${detailSection("Qualité de vie", [
          ["Espaces verts",       fmt(kpis.nb_espaces_verts)],
          ["Arbres",              fmt(kpis.nb_arbres)],
          ["Sanisettes",          fmt(kpis.nb_sanisettes)],
          ["Chantiers actifs",    fmt(kpis.nb_chantiers_actifs)],
          ["Anomalies signalées", fmt(kpis.nb_anomalies)],
          ["Couverture fibre",    fmt(kpis.pct_fibre, " %")],
        ], kpis.annee_qv, annee)}
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
        ], kpis.annee_transport, annee)}
        ${detailSection("Loisirs", [
          ["Événements", fmt(kpis.nb_evenements)],
          ["Cinémas",    fmt(kpis.nb_cinemas)],
          ["Terrasses",  fmt(kpis.nb_terrasses)],
          ["Musées",     fmt(kpis.nb_musees)],
        ], kpis.annee_loisirs, annee)}
        ${detailSection("Services publics", [
          ["Écoles élémentaires",  fmt(kpis.nb_ecoles)],
          ["Collèges",             fmt(kpis.nb_colleges)],
          ["Bibliothèques",        fmt(kpis.nb_bibliotheques)],
          ["Bureaux de poste",     fmt(kpis.nb_bureaux_poste)],
          ["Enseignement sup.",    fmt(kpis.nb_ensup)],
        ], kpis.annee_services, annee)}
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
    renderDonutSurfaces(kpis);

    if (streamingStatus?.last_update) {
      startStreamingCountdown(streamingStatus.last_update, streamingStatus.refresh_interval_seconds ?? 300);
    }

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

function renderDonutSurfaces(kpis) {
  const canvas = document.getElementById("donut-surfaces");
  if (!canvas) return;
  const t1 = kpis.nb_t1 || 0;
  const t2 = kpis.nb_t2 || 0;
  const t3 = kpis.nb_t3 || 0;
  const t4 = kpis.nb_t4plus || 0;
  if (t1 + t2 + t3 + t4 === 0) { canvas.style.display = "none"; return; }
  new Chart(canvas, {
    type: "doughnut",
    data: {
      labels: ["Studios/T1 ≤25m²", "T2 26–45m²", "T3 46–65m²", "T4+ ≥66m²"],
      datasets: [{
        data: [t1, t2, t3, t4],
        backgroundColor: ["#3b82f6", "#22c55e", "#f59e0b", "#ef4444"],
        borderWidth: 1,
      }],
    },
    options: {
      plugins: { legend: { position: "bottom", labels: { font: { size: 10 }, color: "#94a3b8" } } },
      cutout: "60%",
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
          <p>Cliquez sur une zone<br/>pour explorer ses données</p>
      </div>`;
  if (timelineChart) { timelineChart.destroy(); timelineChart = null; }
}
