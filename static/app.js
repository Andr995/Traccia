(() => {
  const $ = (selector) => document.querySelector(selector);
  const state = { selected: null, lastQuery: "", result: null, busy: false };
  const keyRequired = document.body.dataset.accessKeyRequired === "true";
  const historyKey = "traccia.reports.v2";

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));

  function apiHeaders(json = false) {
    const headers = {};
    if (json) headers["Content-Type"] = "application/json";
    if (keyRequired) headers["X-Access-Key"] = $("#access-key").value;
    return headers;
  }

  function showState(name) {
    ["#empty-state", "#loading-state", "#error-state", "#report"].forEach((selector) => {
      $(selector).classList.toggle("hidden", selector !== `#${name}`);
    });
  }

  function setBusy(value) {
    state.busy = value;
    $("#search-button").disabled = value;
    $("#analyze-button").disabled = value || !state.selected;
    $("#search-button").classList.toggle("is-loading", value);
  }

  async function readApiResponse(response) {
    const body = await response.text();
    let payload = null;
    try { payload = body ? JSON.parse(body) : null; } catch {}
    if (!payload || typeof payload !== "object") {
      throw new Error(`Il servizio ha restituito una risposta vuota o non leggibile (HTTP ${response.status}). Controlla i log Render.`);
    }
    if (!response.ok) throw new Error(payload.error || `Richiesta non riuscita (HTTP ${response.status}).`);
    return payload;
  }

  function renderCompanies(results, manualName = "") {
    const container = $("#company-results");
    container.replaceChildren();
    if (!results.length && !manualName) return;
    if (results.length) {
      const heading = document.createElement("p");
      heading.className = "result-count";
      heading.textContent = `${results.length} candidati · verifica nome e descrizione`;
      container.append(heading);
    }
    results.forEach((company) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "company-option";
      const identifier = company.cik
        ? `${company.ticker || "SEC"} · CIK ${company.cik}`
        : `Wikidata ${company.entity_id}${company.description ? ` · ${company.description}` : ""}`;
      button.innerHTML = `<span class="option-monogram" aria-hidden="true">${escapeHtml(company.name.slice(0, 1))}</span>
        <span class="option-info"><strong>${escapeHtml(company.name)}</strong><small>${escapeHtml(identifier)}</small></span>
        <span class="option-arrow" aria-hidden="true">↗</span>`;
      button.addEventListener("click", () => selectCompany(company));
      container.append(button);
    });
    if (manualName) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "company-option manual-option";
      button.innerHTML = `<span class="option-monogram" aria-hidden="true">⌕</span>
        <span class="option-info"><strong>Analizza il nome inserito</strong><small>${escapeHtml(manualName)} · identità non verificata in SEC/Wikidata</small></span>
        <span class="option-arrow" aria-hidden="true">↗</span>`;
      button.addEventListener("click", () => selectCompany({
        name: manualName, ticker: "", cik: "", entity_id: "", entity_source: "Nome inserito",
      }));
      container.append(button);
    }
  }

  function selectCompany(company) {
    state.selected = company;
    $("#selected-name").textContent = company.name;
    $("#selected-meta").textContent = company.cik
      ? `${company.ticker || "SEC"} · CIK ${company.cik}`
      : company.entity_id
        ? `Wikidata ${company.entity_id} · identità da confermare`
        : "Nome inserito · identità non verificata";
    $("#selected-company").classList.remove("hidden");
    $("#company-results").replaceChildren();
    $("#analyze-button").disabled = state.busy;
    $("#search-status").textContent = "Identità selezionata. Avvia l’analisi quando vuoi.";
  }

  async function searchCompanies() {
    const query = $("#company-query").value.trim();
    state.lastQuery = query;
    if (query.length < 2) {
      $("#search-status").textContent = "Inserisci almeno due caratteri.";
      return;
    }
    setBusy(true);
    state.selected = null;
    $("#selected-company").classList.add("hidden");
    $("#analyze-button").disabled = true;
    $("#search-status").textContent = "Ricerca nel catalogo SEC e in Wikidata…";
    $("#company-results").replaceChildren();
    try {
      const response = await fetch(`/api/companies?q=${encodeURIComponent(query)}`, { headers: apiHeaders() });
      const payload = await readApiResponse(response);
      renderCompanies(payload.results || [], payload.manual_name || "");
      $("#search-status").textContent = payload.results?.length
        ? "Controlla nome e descrizione; poi avvia la ricerca sulle fonti."
        : payload.manual_name
          ? "Nessun candidato nei registri: puoi continuare con il nome inserito."
          : "Nessuna corrispondenza trovata.";
    } catch (error) {
      $("#search-status").textContent = error.message;
    } finally {
      setBusy(false);
    }
  }

  function confidenceLabel(score) {
    if (score >= 75) return "Pertinenza alta";
    if (score >= 55) return "Pertinenza moderata";
    return "Pertinenza limitata";
  }

  function verdictClass(verdict) {
    if (verdict.startsWith("Relazione documentata")) return "green";
    if (verdict.startsWith("Non verificato")) return "amber";
    if (verdict.startsWith("Attività in Israele dichiarate")) return "amber";
    if (verdict.startsWith("Possibile relazione")) return "amber";
    if (verdict.startsWith("Menzione trovata")) return "amber";
    return "neutral";
  }

  function renderEvidence(items) {
    if (!items.length) return `<div class="no-evidence"><span aria-hidden="true">⌕</span><div><strong>Nessun passaggio pertinente nelle fonti recuperate</strong><p>Il risultato riguarda solo le fonti e il periodo indicati; non dimostra l’assenza di rapporti.</p></div></div>`;
    return items.map((item, index) => `<details class="evidence-item" ${index === 0 ? "open" : ""}>
      <summary><span class="source-type">${escapeHtml(item.source_class || item.form)}</span><span class="evidence-title">${escapeHtml(item.description || "Passaggio nella fonte")}</span><time>${escapeHtml(item.date ? (item.date_kind === "rilevazione GDELT" ? `Rilevata ${item.date}` : item.date) : "Data n.d.")}</time><span class="chevron" aria-hidden="true">⌄</span></summary>
      <div class="evidence-body"><blockquote>${escapeHtml(item.text)}</blockquote>
        <div class="evidence-meta"><span>${item.candidate ? "Termine di relazione vicino alla menzione" : "Menzione senza relazione stabilita"}</span>${item.entities?.length ? `<span>Entità NER: ${escapeHtml(item.entities.join(", "))}</span>` : ""}</div>
        <a class="source-link" href="${escapeHtml(item.source_url || item.filing_url)}" target="_blank" rel="noopener noreferrer">Apri fonte originale <span aria-hidden="true">↗</span></a>
      </div>
    </details>`).join("");
  }

  function renderInitialSignal(signal) {
    const refs = (signal.references || []).filter((url) => /^https:\/\//i.test(url));
    const update = signal.source_updated_at
      ? escapeHtml(signal.source_updated_at)
      : "Non dichiarata dalla fonte";
    const retrieved = signal.retrieved_at
      ? new Date(signal.retrieved_at).toLocaleString("it-IT")
      : "Data di acquisizione non disponibile";
    return `<section class="initial-signal ${signal.matched ? "is-matched" : "is-empty"}">
      <div class="signal-heading"><div><p class="eyebrow">PISTA INIZIALE · NON È UNA CONFERMA</p><h3>${signal.matched ? `Segnalazione per ${escapeHtml(signal.name)}` : signal.searched ? "Ricerca nelle liste completata" : "Fonti non raggiungibili"}</h3></div>
        <span class="signal-state">${signal.matched ? "Da verificare" : signal.searched ? "Nessuna corrispondenza" : "Dato non disponibile"}</span></div>
      <p class="signal-text">${escapeHtml(signal.description || "Nessun dettaglio disponibile.")}</p>
      ${signal.impact ? `<p class="signal-meta">Priorità riportata dalla lista: <strong>${escapeHtml(signal.impact)}</strong>${signal.category ? ` · ${escapeHtml(signal.category)}` : ""}</p>` : ""}
      <div class="signal-citation"><a href="${escapeHtml(signal.source_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(signal.source)} · apri fonte originale ↗</a>
        <span>Ultimo aggiornamento: ${update}</span><span>Acquisita: ${escapeHtml(retrieved)}</span></div>
      ${refs.length ? `<details class="signal-references"><summary>Riferimenti riportati dalla lista (${refs.length}) · non verificati</summary><ul>${refs.map((url) => `<li><a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(new URL(url).hostname)} ↗</a></li>`).join("")}</ul></details>` : ""}
    </section>`;
  }

  function renderReliability(result) {
    const reliability = result.reliability || {};
    const confirmations = result.primary_confirmations || [];
    const count = Number(reliability.independent_primary_count || 0);
    const sources = confirmations.map((source) => `<li><span><strong>${escapeHtml(source.domain)}</strong> · ${escapeHtml(source.source_class)}</span><a href="${escapeHtml(source.source_url)}" target="_blank" rel="noopener noreferrer">Apri citazione ↗</a></li>`).join("");
    return `<section class="reliability-row ${count ? "has-confirmation" : "no-confirmation"}" aria-label="Conferme da fonti primarie indipendenti">
      <div class="reliability-count"><p class="eyebrow">CONFERME PRIMARIE · DOMINI DISTINTI</p><strong>${count}</strong><span>${escapeHtml(reliability.label || "Nessuna conferma primaria")}</span></div>
      <div class="reliability-detail"><p>${count ? "Il conteggio usa domini distinti con passaggi pertinenti in fonti aziendali ufficiali o filing regolamentari." : "La lista di segnalazione e gli articoli non valgono come conferme primarie: il verdetto resta non verificato."}</p>
        ${count ? `<details class="confirmation-list"><summary>Esamina le conferme (${count})</summary><ul>${sources}</ul></details>` : ""}
      </div><span class="reliability-note">${escapeHtml(reliability.note || "Conteggio, non probabilità")}</span>
    </section>`;
  }

  function renderReport(result) {
    state.result = result;
    const tone = verdictClass(result.verdict);
    const docs = result.documents || [];
    const evidence = result.evidence || [];
    const faissState = result.retrieval?.faiss ? "FAISS vettoriale + BM25" : "BM25 · ricerca lessicale";
    const entityCount = new Set(evidence.flatMap((item) => item.entities || [])).size;
    const sourceStates = (result.source_status || []).map((source) => `<li><strong>${escapeHtml(source.source)}</strong><span>${escapeHtml(source.status)}</span></li>`).join("");
    $("#report").innerHTML = `<div class="report-topline"><span class="eyebrow">DOSSIER ${escapeHtml(result.report_id)}</span><div class="report-actions"><button id="export-report" class="text-button" type="button">Esporta JSON ↓</button><button id="print-report" class="text-button" type="button">Stampa</button></div></div>
      <div class="company-title"><div><h2>${escapeHtml(result.company)}</h2><p>${result.cik ? `${escapeHtml(result.ticker || "Emittente SEC")} · CIK ${escapeHtml(result.cik)}` : `${escapeHtml(result.identity_source || "Nome inserito")}${result.entity_id ? ` · ${escapeHtml(result.entity_id)}` : " · identità da verificare"}`}</p></div><span class="scope-chip">${docs.length} fonti</span></div>
      <section class="verdict-block ${tone}"><div class="verdict-symbol" aria-hidden="true">${tone === "amber" ? "!" : tone === "green" ? "✓" : "·"}</div><div class="verdict-content"><p class="eyebrow">VERDETTO</p><h1>${escapeHtml(result.verdict)}</h1><p>${escapeHtml(result.explanation)}</p></div></section>
      ${renderReliability(result)}
      ${result.initial_signal ? renderInitialSignal(result.initial_signal) : ""}
      <section class="confidence-row" aria-label="Indicatore di pertinenza delle evidenze"><div class="confidence-copy"><span class="eyebrow">QUALITÀ DEL RECUPERO</span><strong>${escapeHtml(result.confidence)}<small>/100</small></strong><span class="confidence-label">${confidenceLabel(result.confidence)}</span></div><progress class="confidence-meter" max="100" value="${Math.max(0, Math.min(100, result.confidence))}" aria-label="Punteggio di pertinenza"></progress><p class="confidence-note">${escapeHtml(result.confidence_label)} · Euristica da calibrare, non probabilità di legame.</p></section>
      <section class="reasoning"><p class="eyebrow">LETTURA DEL SISTEMA</p><p>${escapeHtml(result.explanation)}</p><div class="method-tags"><span>${escapeHtml(faissState)}</span><span>Entità NER negli estratti: ${entityCount}</span><span>${result.retrieval?.official_profile ? "Profilo ufficiale consultato" : "Profilo ufficiale non configurato"}</span><span>${result.retrieval?.gdelt_context ? "GDELT consultato" : "GDELT non disponibile"}</span></div></section>
      <section class="sources-section"><div class="sources-heading"><div><p class="eyebrow">EVIDENZE E CITAZIONI</p><h3>${evidence.length} passaggi da esaminare</h3></div><span class="source-count">${docs.length} fonti consultate</span></div><div class="evidence-list">${renderEvidence(evidence)}</div>
        <details class="consulted-documents"><summary><span>Documenti e fonti consultati (${docs.length})</span><span class="chevron" aria-hidden="true">⌄</span></summary><ul>${docs.map((doc) => `<li><span><b>${escapeHtml(doc.source_class || doc.form)}</b>${doc.date ? ` · ${doc.date_kind === "rilevazione GDELT" ? "rilevata " : ""}${escapeHtml(doc.date)}` : ""}${doc.description ? ` · ${escapeHtml(doc.description)}` : ""}${doc.domain ? ` · ${escapeHtml(doc.domain)}` : ""}</span><a href="${escapeHtml(doc.source_url || doc.filing_url)}" target="_blank" rel="noopener noreferrer" aria-label="Apri fonte ${escapeHtml(doc.description || doc.form)}">Apri ↗</a></li>`).join("") || "<li>Nessuna fonte recuperabile.</li>"}</ul></details>
      </section>
      <details class="methodology"><summary><span>Perimetro e metodo</span><span class="chevron" aria-hidden="true">⌄</span></summary><div><p>${escapeHtml(result.scope)}</p><p>Le menzioni sono estratte dai documenti recuperati. La prossimità a termini di relazione genera un candidato da verificare, non una conclusione automatica. GDELT fornisce un estratto e un collegamento: apri l’articolo originale per controllarne contesto, data e affidabilità.</p>${sourceStates ? `<ul class="source-status-list">${sourceStates}</ul>` : ""}<p>Creato il ${escapeHtml(new Date(result.generated_at).toLocaleString("it-IT"))} · ID ${escapeHtml(result.report_id)}</p></div></details>`;
    showState("report");
    $("#export-report").addEventListener("click", () => exportReport(result));
    $("#print-report").addEventListener("click", () => window.print());
    saveHistory(result);
  }

  async function runAnalysis() {
    if (!state.selected || state.busy) return;
    setBusy(true);
    $("#loading-title").textContent = "Recupero delle fonti";
    $("#loading-message").textContent = "Lettura dei filing e delle fonti pubbliche configurate; ricerca di passaggi pertinenti.";
    showState("loading-state");
    try {
      const response = await fetch("/api/analyze", {
        method: "POST",
        headers: apiHeaders(true),
        body: JSON.stringify({ company: state.selected.name, cik: state.selected.cik || "", entity_id: state.selected.entity_id || "" }),
      });
      const payload = await readApiResponse(response);
      renderReport(payload);
    } catch (error) {
      $("#error-message").textContent = error.message;
      showState("error-state");
    } finally {
      setBusy(false);
    }
  }

  function readHistory() {
    try { return JSON.parse(localStorage.getItem(historyKey) || "[]"); }
    catch { return []; }
  }

  function saveHistory(result) {
    const items = readHistory().filter((item) => item.report_id !== result.report_id);
    items.unshift(result);
    try { localStorage.setItem(historyKey, JSON.stringify(items.slice(0, 8))); }
    catch { /* The report still works when browser storage is disabled or full. */ }
    renderHistory();
  }

  function renderHistory() {
    const list = $("#history-list");
    const items = readHistory();
    if (!items.length) {
      list.innerHTML = `<p class="muted">I report salvati qui restano su questo dispositivo.</p>`;
      return;
    }
    list.innerHTML = items.map((item) => `<button class="history-item" type="button" data-report-id="${escapeHtml(item.report_id)}"><span class="history-dot ${verdictClass(item.verdict)}"></span><span><strong>${escapeHtml(item.company)}</strong><small>${escapeHtml(item.verdict)}</small></span><span class="history-arrow" aria-hidden="true">↗</span></button>`).join("");
    list.querySelectorAll("[data-report-id]").forEach((button) => button.addEventListener("click", () => {
      const item = readHistory().find((record) => record.report_id === button.dataset.reportId);
      if (item) renderReport(item);
    }));
  }

  function exportReport(result) {
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `traccia-${result.cik}-${result.report_id}.json`;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  $("#search-button").addEventListener("click", searchCompanies);
  $("#company-query").addEventListener("keydown", (event) => { if (event.key === "Enter") searchCompanies(); });
  $("#analyze-button").addEventListener("click", runAnalysis);
  $("#retry-search").addEventListener("click", () => { if (state.selected) runAnalysis(); else searchCompanies(); });
  $("#clear-selection").addEventListener("click", () => {
    state.selected = null;
    $("#selected-company").classList.add("hidden");
    $("#analyze-button").disabled = true;
    $("#search-status").textContent = "Selezione rimossa.";
  });
  $("#clear-history").addEventListener("click", () => { try { localStorage.removeItem(historyKey); } catch {} renderHistory(); });
  $("#theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("traccia.theme", next); } catch {}
  });
  if (keyRequired) $("#access-key-row").classList.remove("hidden");
  let theme = null;
  try { theme = localStorage.getItem("traccia.theme"); } catch {}
  if (theme) document.documentElement.dataset.theme = theme;
  renderHistory();
})();
