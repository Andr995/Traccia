(() => {
  const $ = (selector) => document.querySelector(selector);
  const state = { selected: null, lastQuery: "", result: null, busy: false };
  const keyRequired = document.body.dataset.accessKeyRequired === "true";
  const historyKey = "traccia.reports.v2";
  let alternativesRequest = 0;
  let alternativesController = null;

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));

  function safeUrl(value) {
    try {
      const url = new URL(String(value || ""));
      if (["http:", "https:"].includes(url.protocol) && !url.username) return url.href;
    } catch {}
    return "";
  }
  const linkHref = (value) => escapeHtml(safeUrl(value) || "#");

  function cancelAlternatives() {
    alternativesRequest += 1;
    alternativesController?.abort();
    alternativesController = null;
  }

  async function fetchApi(url, options = {}, timeout = 90000, controller = new AbortController()) {
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const response = await fetch(url, { ...options, signal: controller.signal });
      return await readApiResponse(response);
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Tempo di attesa terminato. Il servizio può essere in avvio o la fonte non risponde: riprova tra poco.");
      if (error instanceof TypeError) throw new Error("Connessione al servizio interrotta. Controlla la connessione e riprova.");
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

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
    $("#company-query").disabled = value;
    $("#country-select").disabled = value;
    $("#clear-selection").disabled = value;
  }

  async function readApiResponse(response) {
    const body = await response.text();
    let payload = null;
    try { payload = body ? JSON.parse(body) : null; } catch {}
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error(`Il servizio non ha restituito dati leggibili (HTTP ${response.status}). Può essere in riavvio: attendi e riprova.`);
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
    if (state.busy) return;
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
    if (state.busy) return;
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
      const payload = await fetchApi(`/api/companies?q=${encodeURIComponent(query)}`, { headers: apiHeaders() });
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
        <a class="source-link" href="${linkHref(item.source_url || item.filing_url)}" target="_blank" rel="noopener noreferrer">Apri fonte originale <span aria-hidden="true">↗</span></a>
      </div>
    </details>`).join("");
  }

  function renderInitialSignal(signal) {
    const refs = (signal.references || []).map(safeUrl).filter(Boolean);
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
      <div class="signal-citation"><a href="${linkHref(signal.source_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(signal.source)} · apri fonte originale ↗</a>
        <span>Ultimo aggiornamento: ${update}</span><span>Acquisita: ${escapeHtml(retrieved)}</span></div>
      ${refs.length ? `<details class="signal-references"><summary>Riferimenti riportati dalla lista (${refs.length}) · non verificati</summary><ul>${refs.map((url) => `<li><a href="${linkHref(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(new URL(url).hostname)} ↗</a></li>`).join("")}</ul></details>` : ""}
    </section>`;
  }

  function renderReliability(result) {
    const reliability = result.reliability || {};
    const confirmations = result.primary_confirmations || [];
    const count = Number(reliability.independent_primary_count || 0);
    const sources = confirmations.map((source) => `<li><span><strong>${escapeHtml(source.domain)}</strong> · ${escapeHtml(source.source_class)}</span><a href="${linkHref(source.source_url)}" target="_blank" rel="noopener noreferrer">Apri citazione ↗</a></li>`).join("");
    return `<section class="reliability-row ${count ? "has-confirmation" : "no-confirmation"}" aria-label="Conferme da fonti primarie indipendenti">
      <div class="reliability-count"><p class="eyebrow">CONFERME PRIMARIE · AUTORI DISTINTI</p><strong>${count}</strong><span>${escapeHtml(reliability.label || "Nessuna conferma primaria")}</span></div>
      <div class="reliability-detail"><p>${count ? "Il conteggio raggruppa filing e pagine ufficiali per autore: la stessa azienda conta una volta." : "La lista di segnalazione e gli articoli non valgono come conferme primarie: il verdetto resta non verificato."}</p>
        ${count ? `<details class="confirmation-list"><summary>Esamina le conferme (${count})</summary><ul>${sources}</ul></details>` : ""}
      </div><span class="reliability-note">${escapeHtml(reliability.note || "Conteggio, non probabilità")}</span>
    </section>`;
  }

  function renderAlternatives(result) {
    const data = result.alternatives || {};
    const entries = (data.items || []).map((item) => {
      const screeningSources = (item.screening_sources || []).map((source) => `<li><span>${escapeHtml(source.source)} · ${escapeHtml(source.status)}<br>Ultimo aggiornamento: ${escapeHtml(source.updated_at || "non dichiarato")}${source.retrieved_at ? `<br>Consultata: ${escapeHtml(new Date(source.retrieved_at).toLocaleString("it-IT"))}` : ""}</span>${safeUrl(source.source_url) ? `<a href="${linkHref(source.source_url)}" target="_blank" rel="noopener noreferrer">Apri ↗</a>` : ""}</li>`).join("");
      return `<article class="alternative-item"><div class="alternative-topline"><div><p class="eyebrow">${escapeHtml(item.automatic ? "CONFRONTO AUTOMATICO · " : "SELEZIONE CURATA · ")}${escapeHtml(item.origin)}</p><h4>${escapeHtml(item.brand)} <span>· ${escapeHtml(item.product)}</span></h4></div><span class="alternative-status">${item.screening_complete ? "Liste consultate" : "Screening incompleto"}</span></div>
        <p class="alternative-description">${escapeHtml(item.description)}</p><div class="alternative-links"><a href="${linkHref(item.product_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.source_label)} ↗</a></div>
        ${item.category_url ? `<div class="alternative-links"><a href="${linkHref(item.category_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.category_label || "Verifica settore e paese su Wikidata")} ↗</a><span class="alternative-caveat">BM25 ordina le descrizioni simili; non è una probabilità né un’approvazione.</span></div>` : ""}
        <p class="alternative-caveat">${escapeHtml(item.screening_status)}</p>${item.ownership_note ? `<p class="alternative-description">${escapeHtml(item.ownership_note)}</p>` : ""}${screeningSources ? `<details class="alternative-sources"><summary>Fonti e date del controllo</summary><ul>${screeningSources}</ul></details>` : ""}
      </article>`;
    }).join("");
    return `<section id="alternatives-panel" class="alternatives-section" aria-busy="${Boolean(data.pending)}"><div class="alternatives-heading"><div><p class="eyebrow">CANDIDATI PER PAESE · ${escapeHtml(data.country_name || result.country_name || "")}</p><h3>Alternative a ${escapeHtml(data.target || result.company)}</h3></div><span class="source-count">${(data.items || []).length} suggerimenti</span></div>
      ${entries || (!data.pending ? `<p class="alternatives-empty">Nessun candidato recuperato per questo marchio e paese. L’assenza di suggerimenti non significa che non esistano alternative.</p>` : "")}
      ${data.pending ? `<p class="alternatives-progress" role="status">Ricerca automatica delle alternative in corso… Il report è già consultabile.</p>` : `<button type="button" class="text-button alternatives-retry">${data.error || data.retryable ? "Riprova ricerca alternative" : "Aggiorna alternative"}</button>`}
      ${data.error ? `<p class="alternative-caveat" role="status">${escapeHtml(data.error)}</p>` : ""}
      ${data.automatic_profile_url ? `<p class="alternative-links"><a href="${linkHref(data.automatic_profile_url)}" target="_blank" rel="noopener noreferrer">Scheda usata per il confronto: ${escapeHtml(data.automatic_profile_name)} ↗</a>${data.automatic_retrieved_at ? ` · <span class="source-count">Consultata: ${escapeHtml(new Date(data.automatic_retrieved_at).toLocaleString("it-IT"))}</span>` : ""}</p>` : ""}
      ${data.excluded_count ? `<p class="alternative-caveat">${data.excluded_count} candidato/i escluso/i perché presente/i in una lista di segnalazione.</p>` : ""}<p class="alternatives-disclaimer">Ricerca automatica: ${escapeHtml(data.automatic_status || "nessun dato automatico disponibile")} · Le aziende sono confrontate tramite settore e paese su Wikidata, poi ordinate con BM25 sui nomi e sulle descrizioni. ${escapeHtml(data.catalog_note || "Le corrispondenze sono candidati da controllare, non aziende certificate senza rapporti.")}</p>
    </section>`;
  }

  function renderReport(result) {
    cancelAlternatives();
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
      ${renderAlternatives(result)}
      ${renderReliability(result)}
      ${result.initial_signal ? renderInitialSignal(result.initial_signal) : ""}
      <section class="confidence-row" aria-label="Indicatore di pertinenza delle evidenze"><div class="confidence-copy"><span class="eyebrow">QUALITÀ DEL RECUPERO</span><strong>${escapeHtml(result.confidence)}<small>/100</small></strong><span class="confidence-label">${confidenceLabel(result.confidence)}</span></div><progress class="confidence-meter" max="100" value="${Math.max(0, Math.min(100, result.confidence))}" aria-label="Punteggio di pertinenza"></progress><p class="confidence-note">${escapeHtml(result.confidence_label)} · Euristica da calibrare, non probabilità di legame.</p></section>
      <section class="reasoning"><p class="eyebrow">LETTURA DEL SISTEMA</p><p>${escapeHtml(result.explanation)}</p><div class="method-tags"><span>${escapeHtml(faissState)}</span><span>Entità NER negli estratti: ${entityCount}</span><span>${result.retrieval?.official_profile ? "Profilo ufficiale consultato" : "Profilo ufficiale non configurato"}</span><span>${result.retrieval?.gdelt_context ? "GDELT consultato" : "GDELT non disponibile"}</span></div></section>
      <section class="sources-section"><div class="sources-heading"><div><p class="eyebrow">EVIDENZE E CITAZIONI</p><h3>${evidence.length} passaggi da esaminare</h3></div><span class="source-count">${docs.length} fonti consultate</span></div><div class="evidence-list">${renderEvidence(evidence)}</div>
        <details class="consulted-documents"><summary><span>Documenti e fonti consultati (${docs.length})</span><span class="chevron" aria-hidden="true">⌄</span></summary><ul>${docs.map((doc) => `<li><span><b>${escapeHtml(doc.source_class || doc.form)}</b>${doc.date ? ` · ${doc.date_kind === "rilevazione GDELT" ? "rilevata " : ""}${escapeHtml(doc.date)}` : ""}${doc.description ? ` · ${escapeHtml(doc.description)}` : ""}${doc.domain ? ` · ${escapeHtml(doc.domain)}` : ""}</span><a href="${linkHref(doc.source_url || doc.filing_url)}" target="_blank" rel="noopener noreferrer" aria-label="Apri fonte ${escapeHtml(doc.description || doc.form)}">Apri ↗</a></li>`).join("") || "<li>Nessuna fonte recuperabile.</li>"}</ul></details>
      </section>
      <details class="methodology"><summary><span>Perimetro e metodo</span><span class="chevron" aria-hidden="true">⌄</span></summary><div><p>${escapeHtml(result.scope)}</p><p>Le menzioni sono estratte dai documenti recuperati. La prossimità a termini di relazione genera un candidato da verificare, non una conclusione automatica. GDELT fornisce un estratto e un collegamento: apri l’articolo originale per controllarne contesto, data e affidabilità.</p>${sourceStates ? `<ul class="source-status-list">${sourceStates}</ul>` : ""}<p>Creato il ${escapeHtml(new Date(result.generated_at).toLocaleString("it-IT"))} · ID ${escapeHtml(result.report_id)}</p></div></details>`;
    showState("report");
    $("#export-report").addEventListener("click", () => exportReport(result));
    $("#print-report").addEventListener("click", () => window.print());
    saveHistory(result);
    if (!result.alternatives || result.alternatives.pending || result.country_code !== $("#country-select").value) {
      queueMicrotask(refreshAlternatives);
    }
  }

  async function refreshAlternatives() {
    if (!state.result || state.busy || $("#report").classList.contains("hidden")) return;
    const panel = $("#alternatives-panel");
    if (!panel) return;
    cancelAlternatives();
    const requestId = alternativesRequest;
    const report = state.result;
    const controller = new AbortController();
    alternativesController = controller;
    const countryCode = $("#country-select").value;
    const previous = report.alternatives?.country_code === countryCode ? report.alternatives : {};
    const view = { ...previous, country_code: countryCode, country_name: countrySelectLabel(countryCode), target: report.company };
    function updatePanel(data) {
      const replacement = document.createElement("div");
      replacement.innerHTML = renderAlternatives({ ...report, alternatives: data });
      $("#alternatives-panel")?.replaceWith(replacement.firstElementChild);
    }
    updatePanel({ ...view, pending: true, error: "" });
    try {
      const payload = await fetchApi("/api/alternatives", {
        method: "POST",
        headers: apiHeaders(true),
        body: JSON.stringify({ company: report.company, entity_id: report.entity_id || "", country_code: countryCode }),
      }, 90000, controller);
      if (requestId !== alternativesRequest || state.result !== report || $("#country-select").value !== countryCode) return;
      if (!Array.isArray(payload.items) || payload.country_code !== countryCode || payload.target !== report.company) {
        throw new Error("Risultato delle alternative non valido.");
      }
      report.alternatives = payload;
      report.country_code = countryCode;
      report.country_name = payload.country_name;
      updatePanel(payload);
      saveHistory(report);
    } catch (error) {
      if (requestId !== alternativesRequest || state.result !== report) return;
      report.alternatives = { ...view, pending: false, error: error.message };
      report.country_code = countryCode;
      report.country_name = view.country_name;
      updatePanel(report.alternatives);
      saveHistory(report);
    } finally {
      if (requestId === alternativesRequest) alternativesController = null;
    }
  }

  function countrySelectLabel(code) {
    const option = Array.from($("#country-select").options).find((item) => item.value === code);
    return option?.textContent || "paese selezionato";
  }

  async function runAnalysis() {
    if (!state.selected || state.busy) return;
    setBusy(true);
    cancelAlternatives();
    $("#loading-title").textContent = "Recupero delle fonti";
    $("#loading-message").textContent = "Lettura dei filing e delle fonti pubbliche configurate; ricerca di passaggi pertinenti.";
    showState("loading-state");
    try {
      const payload = await fetchApi("/api/analyze", {
        method: "POST",
        headers: apiHeaders(true),
        body: JSON.stringify({ company: state.selected.name, cik: state.selected.cik || "", entity_id: state.selected.entity_id || "", country_code: $("#country-select").value }),
      }, 150000);
      renderReport(payload);
    } catch (error) {
      $("#error-message").textContent = error.message;
      showState("error-state");
    } finally {
      setBusy(false);
    }
  }

  function readHistory() {
    try {
      const parsed = JSON.parse(localStorage.getItem(historyKey) || "[]");
      return Array.isArray(parsed) ? parsed.filter((item) => item && typeof item.company === "string" && typeof item.verdict === "string" && typeof item.report_id === "string").slice(0, 8) : [];
    }
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
      if (state.busy) return;
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
  $("#company-query").addEventListener("input", () => {
    state.selected = null;
    $("#selected-company").classList.add("hidden");
    $("#company-results").replaceChildren();
    $("#analyze-button").disabled = true;
    $("#search-status").textContent = "Cerca e seleziona l’azienda corrispondente al nuovo nome.";
  });
  $("#report").addEventListener("click", (event) => {
    if (event.target.closest(".alternatives-retry")) refreshAlternatives();
  });
  $("#analyze-button").addEventListener("click", runAnalysis);
  $("#retry-search").addEventListener("click", () => { if (state.selected) runAnalysis(); else searchCompanies(); });
  $("#clear-selection").addEventListener("click", () => {
    state.selected = null;
    $("#selected-company").classList.add("hidden");
    $("#analyze-button").disabled = true;
    $("#search-status").textContent = "Selezione rimossa.";
  });
  $("#clear-history").addEventListener("click", () => { try { localStorage.removeItem(historyKey); } catch {} renderHistory(); });
  const countrySelect = $("#country-select");
  const supportedCountryCodes = new Set(Array.from(countrySelect.options, (option) => option.value));
  let preferredCountry = "IT";
  try {
    const savedCountry = localStorage.getItem("traccia.country");
    const localeCountry = navigator.language.split("-").pop().toUpperCase();
    if (savedCountry && supportedCountryCodes.has(savedCountry)) preferredCountry = savedCountry;
    else if (supportedCountryCodes.has(localeCountry)) preferredCountry = localeCountry;
  } catch {}
  countrySelect.value = preferredCountry;
  countrySelect.addEventListener("change", () => {
    try { localStorage.setItem("traccia.country", countrySelect.value); } catch {}
    refreshAlternatives();
  });
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
