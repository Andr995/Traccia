"""Offline regressions: python -m unittest discover -s tests -v.

All names, IDs and passages below are synthetic fixtures, not research claims.
Only Flask is required; no model download or live public API call is made.
"""

import json
import unittest
from unittest.mock import patch

import app as web
import alternative_recommendations as recommendations
import automatic_alternatives as automatic
import signal_sources as signals
from company_identity import company_key, normalize_name, public_link


def claim(qid):
    return {"mainsnak": {"datavalue": {"value": {"id": qid}}}}


def entity(qid, properties):
    return {"entities": {qid: {
        "claims": {key: [claim(value)] for key, value in properties.items()},
        "labels": {"en": {"value": "Fixture Manufacturer"}},
        "descriptions": {"en": {"value": "Chocolate manufacturer"}},
    }}}


def binding(**fields):
    return {key: {"value": value} for key, value in fields.items()}


EMPTY_SOURCE = {"ok": True, "records": [], "updated_at": "2026-01-01", "retrieved_at": "2026-01-02T00:00:00Z"}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        automatic._cache.clear()
        signals._cache.clear()

    def test_names_preserve_languages_and_legal_suffixes(self):
        self.assertEqual(company_key("The Fixture S.p.A."), "fixture")
        self.assertEqual(company_key("Fixture S.R.L."), "fixture")
        self.assertEqual(normalize_name("Caffè"), "caffe")
        self.assertEqual(normalize_name("任天堂"), "任天堂")

    def test_links_reject_script_and_malformed_urls(self):
        for url in ("javascript:alert(1)", "https://", "https://[broken", "https://user@host.example"):
            self.assertEqual(public_link(url), "")
        self.assertEqual(public_link("https://fixture.example/path"), "https://fixture.example/path")

    def test_resolver_filters_surnames_and_uses_aliases(self):
        payload = {"search": [
            {"id": "Q1", "label": "Fixture", "description": "family name"},
            {"id": "Q2", "label": "Fixture SpA", "description": "Italian manufacturer"},
        ]}
        with patch.object(automatic, "_fetch_json", return_value=payload):
            self.assertEqual(automatic._resolve_entity("Fixture"), "Q2")
        payload = {"search": [{"id": "Q3", "label": "Fixture Holdings", "description": "company", "match": {"text": "Sample"}}]}
        with patch.object(automatic, "_fetch_json", return_value=payload):
            self.assertEqual(automatic._resolve_entity("Sample"), "Q3")

    def test_ambiguous_companies_require_selection(self):
        payload = {"search": [
            {"id": "Q1", "label": "Fixture Inc", "description": "company"},
            {"id": "Q2", "label": "Fixture Ltd", "description": "company"},
        ]}
        with patch.object(automatic, "_fetch_json", return_value=payload):
            self.assertEqual(automatic._resolve_entity("Fixture"), "")

    def test_named_product_does_not_resolve_to_whole_conglomerate(self):
        payload = {"search": [
            {"id": "Q1", "label": "The Fixture Company", "description": "beverage corporation", "match": {"text": "Fixture"}},
            {"id": "Q2", "label": "Fixture", "description": "carbonated soft drink"},
            {"id": "Q3", "label": "Fixture", "description": "painting"},
        ]}
        with patch.object(automatic, "_fetch_json", return_value=payload):
            self.assertEqual(automatic._resolve_entity("Fixture"), "Q2")

    def test_cola_scope_also_applies_to_legal_company_alias(self):
        with patch.object(signals, "_cached_source", return_value=EMPTY_SOURCE), patch.object(recommendations, "get_automatic_alternatives", return_value={}) as discover:
            recommendations.get_alternatives("COCA COLA CO", "IT")
        self.assertEqual(discover.call_args.kwargs["category_ids"], ("Q134041",))

    def test_brand_follows_documented_manufacturer_and_merges_owners(self):
        rows = [binding(company="http://www.wikidata.org/entity/Q30", companyLabel="Peer",
                        website="https://peer.example", industryLabel="Chocolate",
                        descriptionEn="Chocolate producer", parent="http://www.wikidata.org/entity/Q40",
                        parentLabel="Owner One"),
                binding(company="http://www.wikidata.org/entity/Q30", companyLabel="Peer",
                        website="https://peer.example", industryLabel="Chocolate",
                        parent="http://www.wikidata.org/entity/Q41", parentLabel="Owner Two")]
        with patch.object(automatic, "_fetch_json", side_effect=[
            entity("Q10", {"P176": "Q20", "P279": "Q6"}),
            entity("Q20", {"P452": "Q5", "P1056": "Q6", "P749": "Q7"}),
            {"results": {"bindings": rows}},
        ]) as fetch:
            result = automatic._discover("Fixture Brand", "IT", "Q10")
        self.assertEqual(result["entity_id"], "Q10")
        self.assertTrue(result["profile_url"].endswith("Q20"))
        self.assertTrue(result["product_comparison"])
        self.assertEqual(result["items"][0]["parents"], ["Owner One", "Owner Two"])
        from urllib.parse import unquote
        query = unquote(fetch.call_args.args[0])
        self.assertIn("wdt:P1056", query)
        self.assertIn("wd:Q7", query)

    def test_historical_or_malformed_claims_are_ignored(self):
        historical = claim("Q1")
        historical["qualifiers"] = {"P582": [{}]}
        data = {"claims": {"P749": [historical, {"mainsnak": None}, claim("Q2")]}}
        self.assertEqual(automatic._claim_ids(data, "P749"), ["Q2"])

    def test_malformed_sparql_is_retryable_and_not_cached_for_hours(self):
        with patch.object(automatic, "_fetch_json", side_effect=[
            entity("Q10", {"P452": "Q5"}), {"results": []},
        ]):
            result = automatic._discover("Fixture", "IT", "Q10")
        self.assertTrue(result["retryable"])
        stored, data = automatic._cache[("Q10", "IT", ())]
        automatic._cache[("Q10", "IT", ())] = (stored - 61, data)
        with patch.object(automatic, "_fetch_json", side_effect=[
            entity("Q10", {"P452": "Q5"}), {"results": {"bindings": []}},
        ]) as fetch:
            refreshed = automatic._discover("Fixture", "IT", "Q10")
        self.assertEqual(fetch.call_count, 2)
        self.assertFalse(refreshed.get("retryable", False))

    def test_parent_signal_excludes_candidate(self):
        candidate = {"name": "Unlisted Brand", "parents": ["Listed Parent"]}
        def source(key, loader):
            return {**EMPTY_SOURCE, "records": [{"name": "Listed Parent"}]}
        with patch.object(automatic, "_discover", return_value={"items": [candidate]}), patch.object(signals, "_cached_source", side_effect=source):
            result = automatic.get_automatic_alternatives("Fixture", "IT")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["excluded_count"], 1)

    def test_curated_results_survive_discovery_outage(self):
        with patch.object(signals, "_cached_source", return_value=EMPTY_SOURCE), patch.object(automatic, "_fetch_json", side_effect=TimeoutError):
            result = recommendations.get_alternatives("Coca-Cola", "IT")
        self.assertEqual([item["brand"] for item in result["items"]], ["Tomarchio", "Molecola"])
        self.assertTrue(result["retryable"])
        self.assertEqual(result["items"][0]["screening_sources"][0]["updated_at"], "2026-01-01")

    def test_curated_stage_does_not_wait_for_automatic_discovery(self):
        with patch.object(signals, "_cached_source", return_value=EMPTY_SOURCE), patch.object(recommendations, "get_automatic_alternatives") as discover:
            result = recommendations.get_alternatives("Ferrero", "IT", include_automatic=False)
        self.assertTrue(result["pending"])
        self.assertEqual(len(result["items"]), 2)
        discover.assert_not_called()


class SourceTests(unittest.TestCase):
    def setUp(self):
        signals._cache.clear()

    def test_html_void_elements_do_not_swallow_company_cards(self):
        parser = signals.BoycottHTMLParser()
        parser.feed('<h3>Food</h3><h4>Fixture <img src="logo"> <span>high</span></h4><p>First<br>claim <a href="https://source.example">source</a></p><h4>Second low</h4><p>Other claim</p>')
        parser.close()
        self.assertEqual([item["name"] for item in parser.records], ["Fixture", "Second"])
        self.assertIn("First claim", parser.records[0]["description"])
        self.assertEqual(parser.records[0]["references"], ["https://source.example"])

    def test_failed_source_recovers_after_a_minute(self):
        with patch.object(signals.time, "monotonic", return_value=100):
            signals._cached_source("boycott_html", lambda: {"ok": False})
        with patch.object(signals.time, "monotonic", return_value=161):
            recovered = signals._cached_source("boycott_html", lambda: EMPTY_SOURCE)
        self.assertTrue(recovered["ok"])

    def test_incomplete_screening_is_not_clearance(self):
        with patch.object(signals, "_cached_source", return_value={"ok": False, "records": []}):
            result = signals.screen_names(["Fixture"])
        self.assertFalse(result["complete"])
        self.assertFalse(result["matched"])

    def test_company_token_boundaries(self):
        self.assertFalse(signals._record_matches("Intel", {"name": "Intellicorp"}))
        self.assertTrue(signals._record_matches("Fixture S.p.A.", {"name": "Fixture"}))


class ApiTests(unittest.TestCase):
    def setUp(self):
        web._request_counts.clear()
        web.app.config["TESTING"] = True
        self.client = web.app.test_client()
        self.auth = patch.object(web, "APP_ACCESS_KEY", "")
        self.auth.start()
        self.addCleanup(self.auth.stop)

    def test_api_validates_payloads_as_json_errors(self):
        cases = [[], "bad", None, {"company": None}, {"company": "Fixture", "country_code": "ITALY"}, {"company": "Fixture", "entity_id": "invalid"}, {"company": "Fixture", "cik": "12345678901"}]
        for path in ("/api/analyze", "/api/alternatives"):
            for body in cases:
                with self.subTest(path=path, body=body):
                    response = self.client.post(path, data=json.dumps(body), content_type="application/json")
                    self.assertEqual(response.status_code, 400)
                    self.assertIsInstance(response.json["error"], str)

    def test_http_errors_always_have_json_for_api(self):
        response = self.client.get("/api/missing")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.json)
        response = self.client.post("/api/alternatives", data="x" * 40000, content_type="application/json")
        self.assertEqual(response.status_code, 413)
        self.assertIn("error", response.json)

    def test_authentication_and_no_store(self):
        with patch.object(web, "APP_ACCESS_KEY", "fixture-key"):
            denied = self.client.post("/api/alternatives", json={"company": "Fixture"})
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.headers["Cache-Control"], "no-store")

    def test_search_and_alternatives_do_not_consume_analysis_quota(self):
        with patch.object(web, "find_companies", return_value=[]), patch.object(web, "find_wikidata_entities", return_value=[]), patch.object(web, "get_alternatives", return_value={}), patch.object(web, "analyze_company", return_value={"verdict": "Non verificato"}):
            for _ in range(8):
                self.assertEqual(self.client.get("/api/companies?q=Fixture").status_code, 200)
                self.assertEqual(self.client.post("/api/alternatives", json={"company": "Fixture"}).status_code, 200)
            for _ in range(6):
                self.assertEqual(self.client.post("/api/analyze", json={"company": "Fixture"}).status_code, 200)
            self.assertEqual(self.client.post("/api/analyze", json={"company": "Fixture"}).status_code, 429)
            self.client.get("/api/companies?q=Fixture")
            self.assertEqual(self.client.post("/api/analyze", json={"company": "Fixture"}).status_code, 429)

    def test_busy_request_does_not_consume_quota(self):
        web._analysis_lock.acquire()
        try:
            response = self.client.post("/api/analyze", json={"company": "Fixture"})
        finally:
            web._analysis_lock.release()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(web._request_counts, {})

    def test_missing_sec_and_news_still_yields_nonverified_report(self):
        with patch.object(web, "_recent_filings", side_effect=RuntimeError("SEC not configured")), patch.object(web, "_curated_company_evidence", return_value=([], [], "non disponibile")), patch.object(web, "_gdelt_public_sources", return_value=([], [], "non disponibile")), patch.object(signals, "_cached_source", return_value=EMPTY_SOURCE):
            report = web.analyze_company("Fixture", cik="0000000010")
        self.assertEqual(report["verdict"], "Non verificato")
        self.assertTrue(report["alternatives"]["pending"])

    def test_sparse_sec_arrays_do_not_crash(self):
        payload = {"filings": {"recent": {"form": ["10-K", "10-K"], "filingDate": []}}}
        with patch.object(web, "_fetch", return_value=json.dumps(payload).encode()):
            _, filings = web._recent_filings("0000000010")
        self.assertEqual(len(filings), 2)
        self.assertEqual(filings[0]["document"], "")


class VerificationTests(unittest.TestCase):
    def test_negations_and_third_parties_are_not_confirmations(self):
        for passage in ("We have no offices in Israel.", "We do not operate in Israel.", "Our competitors operate in Israel.", "A competitor operates in Israel."):
            self.assertFalse(web._primary_tie_assertion(passage, "Fixture"), passage)
        self.assertTrue(web._primary_tie_assertion("Fixture operates a factory in Israel.", "Fixture"))

    def test_sec_and_company_disclosures_are_one_author(self):
        evidence = [{"candidate": True, "text": "Fixture operates a factory in Israel.", "source_class": kind, "source_url": url} for kind, url in (
            ("Filing regolamentare SEC", "https://www.sec.gov/fixture"),
            ("Fonte aziendale ufficiale", "https://fixture.example/israel"),
        )]
        self.assertEqual(len(web._independent_primary_confirmations(evidence, "Fixture")), 1)


if __name__ == "__main__":
    unittest.main()
