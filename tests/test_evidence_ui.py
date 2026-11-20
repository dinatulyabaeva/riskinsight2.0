"""Browser acceptance for charts and dialog controls, with explicit HTTP fixtures.

Only the frontend is under test here. The backend/MCP integration is tested
separately: no provider-call or numerical-accuracy result is inferred from this
network double. Run with RUN_BROWSER_TESTS=1 (uses locally installed Chromium).
"""
import json
import os
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
UPLOAD = "upload_" + "1" * 32
CONVERSATION = "a" * 32


def response(chart_kind="line"):
    return {
        "mode": "llm", "answer": "DPD 90+ изменился с 2% до 4%. [T2]",
        "duration_ms": 100, "request_id": "test-request", "warnings": [],
        "conversation_id": CONVERSATION, "conversation_reset": False,
        "provenance": {"llm_called": True, "planner": "llm", "narrative_source": "mcp_renderer"},
        "scope": {"dataset_id": "builtin", "as_of": "2026-03-31", "filters": {}},
        "trace": [{"ref": "T2", "ok": True, "tool": "get_npl_trend", "arguments": {},
                   "result": {"points": [{"as_of": "2026-01-31", "npl90_ratio": .02},
                                         {"as_of": "2026-02-28", "npl90_ratio": None},
                                         {"as_of": "2026-03-31", "npl90_ratio": .04}]}, "duration_ms": 4}],
        "protocol": {"transport": "stdio"},
        "visualizations": [{"kind": chart_kind, "title": "Динамика DPD 90+", "unit": "ratio",
                            "x_label": "Дата", "y_label": "DPD 90+, %", "source_ref": "T2",
                            "dataset_id": "builtin", "start_date": "2026-01-31", "end_date": "2026-03-31",
                            "series": [{"name": "DPD 90+", "points": [
                                {"label": "2026-01-31", "value": .02},
                                {"label": "2026-02-28", "value": None},
                                {"label": "2026-03-31", "value": .04}]}]}],
    }


class UIHarness:
    def __init__(self, handler=None):
        self.handler = handler or (lambda body, index: (200, response()))
        self.calls = []
        self.uploaded = False

    def route(self, route):
        request = route.request
        url = urlparse(request.url)
        path = url.path
        if path == "/":
            return route.fulfill(status=200, content_type="text/html", body=(ROOT / "static/index.html").read_text(encoding="utf-8"))
        if path == "/presentation":
            return route.fulfill(status=200, content_type="text/html", body=(ROOT / "docs/presentation.html").read_text(encoding="utf-8"))
        if path.startswith("/static/"):
            file = ROOT / path.lstrip("/")
            return route.fulfill(status=200, content_type="text/css" if path.endswith(".css") else "application/javascript", body=file.read_text(encoding="utf-8"))
        status = 200
        if path == "/api/health":
            data = {"auth_required": False, "storage": "ephemeral"}
        elif path == "/api/datasets":
            data = {"datasets": [{"dataset_id": "builtin", "name": "Встроенный"}]}
            if self.uploaded:
                data["datasets"].append({"dataset_id": UPLOAD, "name": "Тестовый импорт"})
        elif path == "/api/context":
            data = {"dates": ["2026-01-31", "2026-02-28", "2026-03-31"], "latest_date": "2026-03-31",
                    "filters": {"product": ["Потребительский", "Ипотека"], "region": ["Приволжский", "Центральный"], "risk_band": ["A", "B"]},
                    "synthetic": True, "mode": "llm", "llm_configured": True, "model": "network-test-double",
                    "scenarios": {"overview": "Обзор портфеля", "trend": "Покажи тренд DPD 90+", "segments": "Сравни регионы", "drivers": "Что изменилось", "stress": "Стресс PD ×1,5 LGD +5 п.п.", "quality": "Проверь качество"}}
        elif path == "/api/imports":
            data = {"imports": []}
        elif path in {"/api/datasets/import", "/api/datasets/example"}:
            self.uploaded = True
            data = {"status": "imported", "dataset_id": UPLOAD, "row_count": 2, "valid_rows": 2, "invalid_rows": 0, "error_count": 0, "warnings": []}
        elif path == "/api/dashboard":
            dataset = parse_qs(url.query).get("dataset_id", ["builtin"])[0]
            data = {"summary": {"metrics": {"balance_rub": 4000, "npl90_ratio": .75, "loan_count": 2,
                                            "weighted_rate": None, "el_rub": 290, "unavailable_metrics": {}},
                                "npl90_calculation": {"numerator_rub": 3000, "denominator_rub": 4000, "formula": "SUM / SUM", "as_of": "2026-03-31", "dataset_id": dataset}},
                    "trend": {"npl90_delta_pp": 2, "start_date": "2026-01-31", "end_date": "2026-03-31", "points": [
                        {"as_of": "2026-01-31", "npl90_ratio": .02}, {"as_of": "2026-03-31", "npl90_ratio": .04}]},
                    "delinquency": {"buckets": [{"bucket": "0", "balance_share": .25}, {"bucket": "90+", "balance_share": .75}]},
                    "segments": {"segments": [{"segment": "Ипотека", "balance_rub": 4000, "npl90_ratio": .75, "portfolio_share": 1}]}}
        elif path == "/api/chat":
            body = json.loads(request.post_data)
            self.calls.append(body)
            status, data = self.handler(body, len(self.calls))
        else:
            return route.fulfill(status=404, body="Not found")
        return route.fulfill(status=status, content_type="application/json", body=json.dumps(data, ensure_ascii=False))


@unittest.skipUnless(os.getenv("RUN_BROWSER_TESTS") == "1", "Set RUN_BROWSER_TESTS=1")
class EvidenceUITests(unittest.TestCase):
    def setUp(self):
        from playwright.sync_api import sync_playwright, expect
        self.expect = expect
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True, executable_path=os.getenv("CHROMIUM_EXECUTABLE") or None)
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 1000})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def start(self, harness):
        self.page.route("http://127.0.0.1:8765/**", harness.route)
        self.page.goto("http://127.0.0.1:8765/")
        self.expect(self.page.locator("#send-button")).to_be_enabled()

    def ask(self, question):
        self.page.locator("#question").fill(question)
        self.page.locator("#send-button").click()

    def test_chart_has_units_null_gap_and_source_and_is_text_safe(self):
        value = response()
        value["answer"] += " <img src=x onerror=alert(1)>"
        self.start(UIHarness(lambda body, index: (200, value)))
        self.ask("Покажи динамику риска")
        chart = self.page.locator(".evidence-chart")
        self.expect(chart).to_have_count(1)
        self.assertEqual(chart.locator("svg path").get_attribute("d").count("M"), 2)
        self.assertNotIn("L", chart.locator("svg path").get_attribute("d"))
        self.assertEqual(chart.locator("circle").count(), 2)
        chart.locator(".chart-values summary").click()
        self.assertIn("4,00 %", chart.locator("table").inner_text())
        self.assertIn("Нет данных", chart.locator("table").inner_text())
        self.assertEqual(chart.locator('td[data-value="0.04"]').count(), 1)
        self.assertEqual(self.page.locator(".answer img").count(), 0)
        self.assertIn("<img", self.page.locator(".answer").inner_text())
        chart.locator(".source-ref").click()
        self.assertTrue(self.page.locator(".trace").evaluate("e=>e.open"))
        self.assertIn("Модель выбрала инструменты", self.page.locator(".provenance-note").inner_text())
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > innerWidth"))

    def test_retry_keeps_turn_id_and_followup_memory_resets_with_scope(self):
        harness = UIHarness(lambda body, index: (502, {"detail": "Тестовая ошибка провайдера", "request_id": "error-42"}) if index == 1 else (200, response()))
        self.start(harness)
        self.ask("Сравни регионы")
        self.expect(self.page.locator(".analysis-error")).to_contain_text("error-42")
        self.page.locator(".retry-analysis").click()
        self.expect(self.page.locator(".result-block")).to_have_count(1)
        self.assertEqual(self.page.locator(".user-message").count(), 1)
        self.assertEqual(harness.calls[0]["client_request_id"], harness.calls[1]["client_request_id"])
        self.assertEqual(len(harness.calls[1]["client_request_id"]), 32)
        self.expect(self.page.locator("#send-button")).to_be_enabled()
        self.ask("А теперь покажи тренд")
        self.expect(self.page.locator(".result-block")).to_have_count(2)
        self.assertEqual(harness.calls[-1]["conversation_id"], CONVERSATION)
        self.expect(self.page.locator("#product-filter")).to_be_enabled()
        self.page.select_option("#product-filter", "Ипотека")
        self.expect(self.page.locator("#send-button")).to_be_enabled()
        self.expect(self.page.locator(".result-block")).to_have_count(0)
        self.ask("Покажи динамику")
        self.expect(self.page.locator(".result-block")).to_have_count(1)
        self.assertIsNone(harness.calls[-1]["conversation_id"])
        self.assertEqual(harness.calls[-1]["filters"]["product"], "Ипотека")
        self.page.locator("#new-analysis").click()
        self.expect(self.page.locator(".result-block")).to_have_count(0)
        self.expect(self.page.locator("#memory-status")).to_have_text("Новый диалог")

    def test_upload_selects_new_dataset_and_new_question_uses_it(self):
        harness = UIHarness()
        self.start(harness)
        self.page.locator("#xlsx-file").set_input_files({"name": "portfolio.xlsx", "mimeType": "application/octet-stream", "buffer": b"mock-import-bytes"})
        self.page.locator("#upload-button").click()
        self.expect(self.page.locator("#dataset-filter")).to_have_value(UPLOAD)
        self.expect(self.page.locator("#send-button")).to_be_enabled()
        self.ask("Каков EL загруженного портфеля?")
        self.expect(self.page.locator(".result-block")).to_have_count(1)
        self.assertEqual(harness.calls[-1]["dataset_id"], UPLOAD)
        self.assertIsNone(harness.calls[-1]["conversation_id"])
        self.expect(self.page.locator("#storage-note")).to_be_visible()

    def test_unverified_chart_is_not_rendered_and_demo_is_labeled(self):
        value = response("bar")
        value["visualizations"][0]["source_ref"] = "T999"
        value["mode"] = "demo"
        value["provenance"] = {"llm_called": False, "planner": "demo", "narrative_source": "mcp_renderer"}
        self.start(UIHarness(lambda body, index: (200, value)))
        self.ask("Покажи график")
        self.expect(self.page.locator(".result-block")).to_have_count(1)
        self.assertEqual(self.page.locator(".evidence-chart").count(), 0)
        self.assertIn("DEMO", self.page.locator(".answer-head").inner_text())

    def test_presentation_navigation_and_scenario_chart_use_fixture(self):
        self.start(UIHarness())
        self.page.goto("http://127.0.0.1:8765/presentation")
        self.expect(self.page.locator("#counter")).to_have_text("01 / 10")
        self.page.keyboard.press("ArrowRight")
        self.expect(self.page.locator("#counter")).to_have_text("02 / 10")
        self.page.locator('#dots button[aria-label="Слайд 4"]').click()
        self.page.select_option("#demo-scenario", "deterioration")
        self.page.select_option("#demo-month", "2026-04-30")
        self.expect(self.page.locator("#d-npl")).to_have_text("5,57%")
        self.assertEqual(self.page.locator("#demo-chart circle").count(), 12)
        self.page.select_option("#demo-scenario", "recovery")
        self.page.select_option("#demo-month", "2026-09-30")
        self.expect(self.page.locator("#d-npl")).to_have_text("3,85%")
        self.page.locator('#dots button[aria-label="Слайд 5"]').click()
        self.page.locator('[data-step="2"]').click()
        self.expect(self.page.locator("#flow-description")).to_contain_text("MCP-клиент")
        self.page.set_viewport_size({"width": 390, "height": 844})
        self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > innerWidth"))


if __name__ == "__main__":
    unittest.main()
