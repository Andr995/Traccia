/* Offline browser regressions. Run `npm ci` then `npm test`. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { JSDOM } = require("jsdom");

const html = fs.readFileSync("templates/index.html", "utf8");
const script = fs.readFileSync("static/app.js", "utf8");
const flush = () => new Promise((resolve) => setImmediate(resolve));
const report = (id, country = "IT") => ({
  company: `Fixture ${id}`, report_id: id, verdict: "Non verificato", country_code: country,
  country_name: country, documents: [], evidence: [], source_status: [], confidence: 10,
  alternatives: { target: `Fixture ${id}`, country_code: country, country_name: country, items: [], applicable: false, pending: true },
});
const suggestions = (id, country, brand = "Candidate") => ({
  target: `Fixture ${id}`, country_code: country, country_name: country, applicable: true,
  items: [{ brand, product: "Fixture", product_url: "https://fixture.example", screening_sources: [] }],
});

function setup(history = []) {
  const dom = new JSDOM(html, { url: "https://traccia.example", runScripts: "outside-only" });
  const { window } = dom;
  const requests = [];
  window.fetch = (url, options = {}) => new Promise((resolve) => requests.push({
    url, options,
    reply: (data, status = 200) => resolve({ ok: status < 400, status, text: async () => typeof data === "string" ? data : JSON.stringify(data) }),
  }));
  window.localStorage.setItem("traccia.reports.v2", JSON.stringify(history));
  window.localStorage.setItem("traccia.country", "IT");
  window.eval(script);
  return { window, document: window.document, requests, close: () => window.close() };
}

async function run() {
  let checks = 0;
  {
    const ui = setup({ corrupted: true });
    assert.match(ui.document.querySelector("#history-list").textContent, /report salvati/);
    ui.close(); checks++;
  }
  {
    const ui = setup([report("A"), report("B")]);
    ui.document.querySelector('[data-report-id="A"]').click();
    await flush();
    assert.equal(ui.requests.length, 1);
    assert.match(ui.document.querySelector("#alternatives-panel").textContent, /in corso/);
    ui.document.querySelector('[data-report-id="B"]').click();
    await flush();
    assert.equal(ui.requests.length, 2);
    ui.requests[1].reply(suggestions("B", "IT", "Peer B"));
    await flush();
    // Simulate a response arriving even after AbortController cancellation.
    ui.requests[0].reply(suggestions("A", "IT", "Wrong A"));
    await flush();
    const panel = ui.document.querySelector("#alternatives-panel").textContent;
    assert.match(panel, /Peer B/);
    assert.doesNotMatch(panel, /Wrong A/);
    const stored = JSON.parse(ui.window.localStorage.getItem("traccia.reports.v2"));
    assert.equal(stored.find((item) => item.report_id === "B").alternatives.items[0].brand, "Peer B");
    ui.close(); checks++;
  }
  {
    const fixture = report("A");
    fixture.alternatives = { ...suggestions("A", "IT", "Curated Italy"), pending: false };
    const ui = setup([fixture]);
    ui.document.querySelector('[data-report-id="A"]').click();
    ui.document.querySelector(".alternatives-retry").click();
    assert.match(ui.document.querySelector("#alternatives-panel").textContent, /Curated Italy/);
    ui.requests[0].reply("<html>Bad Gateway</html>", 502);
    await flush();
    assert.match(ui.document.querySelector("#alternatives-panel").textContent, /Curated Italy/);
    assert.match(ui.document.querySelector("#alternatives-panel").textContent, /Riprova ricerca/);
    assert.equal(ui.document.querySelector("#report").classList.contains("hidden"), false);
    ui.close(); checks++;
  }
  {
    const ui = setup([report("A")]);
    ui.document.querySelector('[data-report-id="A"]').click();
    await flush();
    const select = ui.document.querySelector("#country-select");
    select.value = "FR";
    select.dispatchEvent(new ui.window.Event("change"));
    await flush();
    ui.requests[1].reply(suggestions("A", "FR", "French Peer"));
    await flush();
    ui.requests[0].reply(suggestions("A", "IT", "Stale Italian Peer"));
    await flush();
    assert.match(ui.document.querySelector("#alternatives-panel").textContent, /French Peer/);
    assert.doesNotMatch(ui.document.querySelector("#alternatives-panel").textContent, /Stale Italian/);
    ui.close(); checks++;
  }
  {
    const ui = setup();
    ui.document.querySelector("#company-query").value = "Fixture";
    ui.document.querySelector("#search-button").click();
    ui.requests[0].reply({ results: [{ name: "Fixture A", cik: "0000000010" }] });
    await flush();
    ui.document.querySelector(".company-option").click();
    assert.equal(ui.document.querySelector("#analyze-button").disabled, false);
    ui.document.querySelector("#company-query").value = "Changed";
    ui.document.querySelector("#company-query").dispatchEvent(new ui.window.Event("input"));
    assert.equal(ui.document.querySelector("#analyze-button").disabled, true);
    ui.close(); checks++;
  }
  {
    const ui = setup();
    ui.document.querySelector("#company-query").value = "Fixture";
    ui.document.querySelector("#search-button").click();
    ui.requests[0].reply({ results: [{ name: "Fixture A", cik: "0000000010" }] });
    await flush();
    ui.document.querySelector(".company-option").click();
    ui.document.querySelector("#analyze-button").click();
    ui.requests[1].reply(report("A"));
    await flush();
    assert.equal(ui.document.querySelector("#report").classList.contains("hidden"), false);
    assert.equal(ui.requests[2].url, "/api/alternatives");
    ui.requests[2].reply(suggestions("A", "IT"));
    await flush();
    assert.match(ui.document.querySelector("#alternatives-panel").textContent, /Candidate/);
    ui.close(); checks++;
  }
  {
    const fixture = report("A");
    fixture.alternatives = suggestions("A", "IT");
    fixture.alternatives.items[0].product_url = "javascript:alert(1)";
    fixture.initial_signal = { references: ["https://[broken", "javascript:alert(1)"], source_url: "javascript:alert(1)" };
    const ui = setup([fixture]);
    ui.document.querySelector('[data-report-id="A"]').click();
    assert.equal(ui.document.querySelectorAll('a[href^="javascript:"]').length, 0);
    assert.match(ui.document.querySelector("#report").textContent, /Fixture A/);
    ui.close(); checks++;
  }
  console.log(`${checks} browser regressions passed`);
}

run().catch((error) => { console.error(error); process.exitCode = 1; });
