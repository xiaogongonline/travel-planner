"""Offline regression tests, with synthetic fixtures and automatically cleaned temporary files."""
import copy
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import travel_plan as tp

AS_OF = "2026-09-23"


class ChecklistParser(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.inputs = {}
        self.labels = set()
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("type") == "checkbox":
            self.inputs[attrs["id"]] = attrs
        elif tag == "label":
            self.labels.add(attrs.get("for"))


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.demo = tp.read_json(tp.ROOT / "assets" / "demo-plan.json")
        self.plan = copy.deepcopy(self.demo)
        self.plan["bookings"][0].update(status="confirmed", confirmed_evidence="模拟确认结果")
        for t in self.plan["tasks"]:
            t["status"] = "done"

    def report(self):
        return tp.validate(self.plan, AS_OF)

    def test_consistent_plan_and_decimal_budget(self):
        result = self.report()
        self.assertEqual(result["schema_errors"], [])
        self.assertEqual(result["blockers"], [])
        self.assertEqual(result["budget"]["required_low"], 1070)
        self.assertEqual(result["budget"]["required_high"], 1370)

    def test_pending_booking_downgrades_ready(self):
        self.plan = self.demo
        self.plan["status"] = "ready"
        result = self.report()
        self.assertEqual(result["effective_status"], "conditional")
        self.assertTrue(any("关键安排未落实" in x for x in result["blockers"]))

    def test_hard_budget_conflict(self):
        self.plan["budget"]["limit"] = 1200
        self.assertTrue(any("预算上限" in x for x in self.report()["blockers"]))

    def test_paid_counted_in_total_and_removed_from_remaining(self):
        self.plan["costs"][0]["paid"] = 240
        total = self.report()["budget"]
        self.assertEqual(total["required_low"], 1070)
        self.assertEqual(total["remaining_low"], 728.99)
        self.plan["budget"].update(scope="remaining", limit=1150)
        self.assertEqual(self.report()["blockers"], [])

    def test_date_change_invalidates_source_and_booking(self):
        self.plan["trip"]["end_date"] = "2026-10-04"
        self.plan["sources"][0]["valid_to"] = "2026-10-02"
        self.plan["bookings"][0]["date"] = "2026-10-02"
        issues = self.report()["blockers"]
        self.assertTrue(any("不适用于" in x for x in issues))
        self.assertTrue(any("预约日期不一致" in x for x in issues))

    def test_overlap(self):
        self.plan["days"][0]["items"][1]["start"] = "2026-10-02T10:00:00+08:00"
        self.assertTrue(any("重叠" in x for x in self.report()["blockers"]))

    def test_buffer_underallocation(self):
        self.plan["days"][0]["items"][0]["buffer_minutes"] = 40
        self.assertTrue(any("时间槽不足" in x for x in self.report()["blockers"]))

    def test_window_violation(self):
        self.plan["days"][1]["items"][0]["windows"][0]["start"] = "2026-10-03T11:00:00+08:00"
        self.assertTrue(any("可用时间窗" in x for x in self.report()["blockers"]))

    def test_missing_map_can_remain_explicit_estimate(self):
        self.plan["days"][0]["items"][0]["source_ids"] = []
        self.assertEqual(self.report()["blockers"], [])
        self.assertIn("交通方式待酒店选定后核实", tp.render(self.plan, self.report()))

    def test_critical_unknown(self):
        self.plan["places"][0]["facts"][0].update(kind="unknown", critical=True)
        self.assertTrue(any("关键事实未落实" in x for x in self.report()["blockers"]))

    def test_unsupported_verified_claim(self):
        self.plan["places"][0]["facts"][0].update(kind="verified", source_ids=[])
        self.assertTrue(any("没有来源" in x for x in self.report()["blockers"]))

    def test_invalid_reference_and_sensitive_extra_field(self):
        self.plan["days"][0]["items"][0]["source_ids"] = ["missing"]
        self.assertTrue(self.report()["schema_errors"])
        self.plan["passport_number"] = "not-a-real-number"
        self.assertTrue(any("未知字段" in x for x in self.report()["schema_errors"]))

    def test_bad_datetime_and_nonfinite(self):
        self.plan["days"][0]["items"][0]["start"] = "09:00"
        self.assertTrue(self.report()["schema_errors"])
        self.plan = copy.deepcopy(self.demo)
        self.plan["costs"][0]["quantity"] = float("nan")
        self.assertTrue(self.report()["schema_errors"])

    def test_huge_numbers_rejected_without_arithmetic_crash(self):
        self.plan["costs"][0]["unit_high"] = 1e30
        self.assertTrue(self.report()["schema_errors"])
        self.plan["costs"][0]["unit_high"] = 10 ** 400
        self.assertTrue(self.report()["schema_errors"])

    def test_unknown_source_dates_not_presented_as_unlimited(self):
        self.plan["sources"][0].update(valid_from=None, valid_to=None)
        rendered = tp.render(self.plan, self.report())
        self.assertIn("截止适用日期未确认", rendered)
        self.assertNotIn("截止未限定", rendered)

    def test_cross_timezone_duration(self):
        i = self.plan["days"][0]["items"][0]
        i.update(start="2026-10-02T09:30:00+08:00", end="2026-10-02T03:30:00+01:00")
        self.assertEqual(self.report()["blockers"], [])

    def test_activity_end_exceeds_trip(self):
        self.plan["days"][1]["items"][-1]["end"] = "2026-10-04T01:00:00+08:00"
        self.plan["status"] = "ready"
        result = self.report()
        self.assertEqual(result["effective_status"], "conditional")
        self.assertTrue(any("超出旅行结束日" in x for x in result["blockers"]))

    def test_one_night_quote_checks_consumption_date(self):
        source = self.plan["sources"][0]
        source = dict(source, id="s-hotel", valid_to="2026-10-02")
        self.plan["sources"].append(source)
        self.plan["costs"][1]["source_ids"] = ["s-hotel"]
        self.assertEqual(self.report()["blockers"], [])
        self.plan["costs"][1]["dates"] = ["2026-10-03"]
        self.assertTrue(any("不适用于" in x for x in self.report()["blockers"]))

    def test_due_critical_source_needs_refresh(self):
        self.plan["sources"][0]["recheck_on"] = AS_OF
        self.assertTrue(any("需重新核实" in x for x in self.report()["blockers"]))

    def test_source_issues_accumulate_without_duplicate_warning(self):
        source = self.plan["sources"][0]
        source.update(valid_from="2026-09-01", valid_to="2026-10-01",
                      conflict="开放时间相互矛盾", recheck_on=AS_OF)
        result = self.report()
        self.assertEqual(result["schema_errors"], [])
        for text in ("不适用于", "开放时间相互矛盾", "已到复核日期"):
            self.assertTrue(any(text in issue for issue in result["blockers"]), text)
        self.assertFalse(any("已到复核日期" in issue for issue in result["warnings"]))
        unused = dict(source, id="s-unused")
        self.plan["sources"].append(unused)
        self.assertTrue(any("s-unused" in issue and "已到复核日期" in issue
                            for issue in self.report()["warnings"]))

    def test_decimal_cost_display_matches_single_cost_total(self):
        for unit, quantity, expected in ((2.675, 1, "2.68"), (0.05, 0.9, "0.04")):
            with self.subTest(unit=unit, quantity=quantity):
                cost = dict(self.demo["costs"][0], unit_low=unit, unit_high=unit,
                            quantity=quantity, paid=0, optional=False)
                self.plan["costs"] = [cost]
                result = self.report()
                self.assertEqual(result["budget"]["required_low"], float(expected))
                rendered = tp.render(self.plan, result)
                self.assertIn(f'class="amount">{expected}–{expected} 元', rendered)
                self.assertIn(f'<strong>{expected}–{expected}</strong>', rendered)
                self.assertNotIn("CNY", rendered)

    def test_cli_utf8_pipes_and_existing_file_recovery(self):
        env = dict(os.environ, PYTHONIOENCODING="gbk", PYTHONUTF8="0")
        command = [sys.executable, "-B", str(tp.ROOT / "scripts" / "travel_plan.py")]
        with tempfile.TemporaryDirectory(prefix="travel-中文-") as folder:
            path = Path(folder) / "行程.json"
            created = subprocess.run(command + ["init", str(path)], env=env, capture_output=True)
            self.assertEqual(created.returncode, 0, created.stderr)
            self.assertEqual(json.loads(created.stdout.decode("utf-8"))["created"], str(path.resolve()))
            original = path.read_bytes()
            duplicate = subprocess.run(command + ["init", str(path)], env=env, capture_output=True)
            self.assertEqual(duplicate.returncode, 2)
            error = json.loads(duplicate.stderr.decode("utf-8"))["error"]
            self.assertIn("目标文件已存在", error)
            self.assertIn("--force", error)
            self.assertEqual(path.read_bytes(), original)
            checked = subprocess.run(command + ["check", str(path)], env=env, capture_output=True)
            self.assertEqual(checked.returncode, 1)
            self.assertTrue(json.loads(checked.stdout.decode("utf-8"))["blockers"])
            forced = subprocess.run(command + ["init", str(path), "--force"], env=env, capture_output=True)
            self.assertEqual(forced.returncode, 0, forced.stderr)

    def test_no_destination_is_exploration(self):
        self.plan = tp.blank()
        result = self.report()
        self.assertEqual(result["effective_status"], "exploration")
        self.assertTrue(result["blockers"])
        self.assertEqual(result["schema_errors"], [])

    def test_html_escape_preserves_text_without_execution(self):
        self.plan["title"] = '<img src=x onerror=alert(1)> @@BODY@@'
        self.plan["places"][0]["address"] = '</span><script>alert(1)</script>'
        rendered = tp.render(self.plan, self.report())
        self.assertNotIn("<script>alert(1)", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("@@BODY@@", rendered)
        self.assertTrue(tp.audit(rendered)["passed"])

    def test_unsafe_source_url_rejected(self):
        self.plan["sources"][0]["url"] = "javascript:alert(1)"
        self.assertTrue(self.report()["schema_errors"])

    def test_no_javascript_needed_for_core_content(self):
        rendered = tp.render(self.demo, tp.validate(self.demo, AS_OF))
        static = rendered.split("<script>")[0]
        for expected in ("示例河湾步道", "2026-10-03", "示例展馆入场", "1,370.00", "预约待完成"):
            self.assertIn(expected, static)
        self.assertTrue(tp.audit(rendered)["passed"])

    def test_checklist_initial_states_and_accessible_labels(self):
        self.plan["tasks"][0]["status"] = "pending"
        self.plan["tasks"].append(dict(self.plan["tasks"][0], id="t-done", status="done"))
        original = copy.deepcopy(self.plan)
        parsed = ChecklistParser(tp.render(self.plan, self.report()))
        self.assertEqual(len(parsed.inputs), len(self.plan["tasks"]))
        for task in self.plan["tasks"]:
            ident = "task-check-" + task["id"]
            self.assertIn(ident, parsed.labels)
            self.assertEqual("checked" in parsed.inputs[ident], task["status"] == "done")
            self.assertIn("disabled", parsed.inputs[ident])
        self.assertEqual(self.plan, original)

    def test_checklist_content_namespace_is_stable_and_isolated(self):
        key = tp.checklist_key(self.demo)
        reordered = dict(reversed(list(self.demo.items())))
        reordered["generated_at"] = "2026-09-24T12:00:00+08:00"
        self.assertEqual(tp.checklist_key(reordered), key)
        for field, value in (("revision", "new-version"), ("title", "another-trip")):
            changed = copy.deepcopy(self.demo)
            changed[field] = value
            self.assertNotEqual(tp.checklist_key(changed), key)
        changed = copy.deepcopy(self.demo)
        changed["tasks"][0]["action"] += "（新要求）"
        self.assertNotEqual(tp.checklist_key(changed), key)

    def test_empty_checklist_renders_without_controls(self):
        self.plan["tasks"] = []
        parsed = ChecklistParser(tp.render(self.plan, self.report()))
        self.assertEqual(parsed.inputs, {})

    def test_compact_activity_times_preserve_date_and_offsets(self):
        same_day = tp.activity_time("2026-10-02T09:30:00+08:00", "2026-10-02T10:30:00+08:00")
        self.assertIn("10-02 · UTC+08:00", same_day)
        overnight = tp.activity_time("2026-10-02T23:30:00+08:00", "2026-10-03T01:30:00+08:00")
        self.assertIn("10-02 23:30 UTC+08:00", overnight)
        self.assertIn("10-03 01:30 UTC+08:00", overnight)
        cross_zone = tp.activity_time("2026-10-02T09:00:00+08:00", "2026-10-02T13:00:00+09:00")
        self.assertIn("10-02 09:00 UTC+08:00", cross_zone)
        self.assertIn("10-02 13:00 UTC+09:00", cross_zone)

    def test_domestic_trip_hides_utc_but_mixed_offsets_keep_it(self):
        rendered = tp.render(self.plan, self.report())
        self.assertNotIn("UTC+", rendered)
        self.plan["days"][0]["items"][0]["end"] = "2026-10-02T03:30:00+01:00"
        self.assertIn("UTC+08:00", tp.render(self.plan, self.report()))

    def test_detects_external_resources_but_allows_online_links(self):
        for snippet in ('<script src="app.js"></script>', '<img src="https://example.com/x.png">',
                        '<style>@import "x.css";</style>', '<style>body{background:url(bg.png)}</style>',
                        '<script>fetch("/plan.json")</script>',
                        '<svg><image href="photo.png"/></svg>',
                        '<button onclick="fetch(\'https://example.com\')">load</button>'):
            self.assertFalse(tp.audit(snippet)["passed"], snippet)
        self.assertTrue(tp.audit('<a href="https://example.com">来源</a>')["passed"])

    def test_no_overwrite_and_utf8_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plan.json"
            tp.write_new(path, json.dumps(self.demo, ensure_ascii=False))
            self.assertEqual(tp.read_json(path), self.demo)
            with self.assertRaises(FileExistsError):
                tp.write_new(path, "{}")

    def test_card_optional_sections_and_status(self):
        self.plan.pop("card")
        self.plan["status"] = "exploration"
        report = self.report()
        self.assertEqual(report["schema_errors"], [])
        page, message, count = tp.render_card(self.plan, report)
        self.assertEqual(count, 1)
        self.assertIn("草稿·还没定", page)
        self.assertIn("草稿·还没定", message)
        for absent in ("投票 · 在群里回复编号", "分工 · 等你认领", "AA · 已付款才结算"):
            self.assertNotIn(absent, page)
        self.assertIn("@杰纶hhh", page)

    def test_card_aa_remainder_and_planned_expense(self):
        aa = self.plan["card"]["aa"]
        aa["members"] = [{"id": ident, "name": ident} for ident in "abc"]
        aa["expenses"] = [
            {"label": "车费", "amount": "1.01", "status": "paid", "payer_id": "a", "participant_ids": ["a", "b", "c"]},
            {"label": "计划晚餐", "amount": "9.00", "status": "planned", "payer_id": None, "participant_ids": ["a", "b", "c"]},
        ]
        self.assertEqual(self.report()["schema_errors"], [])
        balances, planned, transfers = tp.aa_result(aa)
        self.assertEqual(balances, {"a": 67, "b": -34, "c": -33})
        self.assertEqual(planned[0][2], {"a": 300, "b": 300, "c": 300})
        self.assertEqual(transfers, (("b", "a", 34), ("c", "a", 33)))
        page, message, _ = tp.render_card(self.plan, self.report())
        self.assertIn("b → a 0.34 元", message)
        self.assertIn("计划支出（尚不结算）", message)
        self.assertNotIn("9.00 元\n建议转账", message)
        self.assertNotIn("CNY", page)
        self.assertIn("0.34 元", page)

    def test_card_aa_finds_fewer_transfers_than_first_match(self):
        aa = {"members": [{"id": ident, "name": ident} for ident in "abcd"], "expenses": [
            {"label": "一", "amount": "0.05", "status": "paid", "payer_id": "d", "participant_ids": ["a"]},
            {"label": "二", "amount": "0.04", "status": "paid", "payer_id": "c", "participant_ids": ["b"]},
        ]}
        balances, _, transfers = tp.aa_result(aa)
        self.assertEqual(balances, {"a": -5, "b": -4, "c": 4, "d": 5})
        self.assertEqual(len(transfers), 2)
        self.assertEqual(set(transfers), {("a", "d", 5), ("b", "c", 4)})

    def test_card_rejects_bad_optional_fields(self):
        examples = [
            {"aa": {"members": [], "expenses": [{"label": "x", "amount": "NaN", "status": "paid", "payer_id": "missing", "participant_ids": []}]}},
            {"polls": [{"question": "选哪个？", "options": ["只有一个"], "deadline": None}]},
            {"roles": [{"role": "", "assignee": None}]},
            {"meeting": {"time": "09:00", "place": "车站"}},
            {"phone_number": "13800138000"},
        ]
        for card in examples:
            with self.subTest(card=card):
                self.plan["card"] = card
                self.assertTrue(self.report()["schema_errors"])

    def test_card_does_not_export_private_plan_fields(self):
        secret = "13800138000"
        self.plan["bookings"][0]["confirmed_evidence"] = secret
        self.plan["places"][0]["address"] = "私人住址 " + secret
        self.plan["days"][0]["summary"] += " 手机号：" + secret
        self.plan["sources"][0]["summary"] = "订单号：ABC123"
        page, message, _ = tp.render_card(self.plan, self.report())
        for value in (secret, "ABC123", "私人住址"):
            self.assertNotIn(value, page + message)

    def test_card_long_trip_adds_pages_and_keeps_all_days_in_text(self):
        for number in range(4, 16):
            self.plan["days"].append({"date": f"2026-10-{number:02d}", "summary": f"第 {number} 天慢游", "intensity": "轻松",
                                      "intensity_basis": "演示", "items": [], "alternatives": []})
        page, message, count = tp.render_card(self.plan, self.report())
        self.assertGreater(count, 2)
        self.assertEqual(page.count('class="sheet"'), count)
        self.assertIn("2026-10-15  第 15 天慢游", message)

    def test_card_cli_generates_png_and_respects_existing_files(self):
        if not tp.available_browser():
            self.skipTest("此环境未安装可用于出图的 Chrome/Edge")
        command = [sys.executable, "-B", str(tp.ROOT / "scripts" / "travel_plan.py")]
        with tempfile.TemporaryDirectory(prefix="搭子卡-") as folder:
            output = Path(folder) / "out"
            first = subprocess.run(command + ["card", str(tp.ROOT / "assets" / "demo-plan.json"), "-o", str(output),
                                              "--as-of", AS_OF], capture_output=True)
            self.assertEqual(first.returncode, 1, first.stderr.decode("utf-8", "replace"))
            card = json.loads(first.stdout.decode("utf-8"))["card"]
            self.assertEqual(len(card["images"]), 2)
            self.assertTrue(Path(card["text"]).is_file())
            self.assertTrue(Path(card["html"]).is_file())
            for picture in card["images"]:
                self.assertEqual(tp.png_size(picture), (1080, 1440))
            again = subprocess.run(command + ["card", str(tp.ROOT / "assets" / "demo-plan.json"), "-o", str(output),
                                              "--as-of", AS_OF], capture_output=True)
            self.assertEqual(again.returncode, 2)
            self.assertIn("--force", again.stderr.decode("utf-8"))

    def test_card_reports_missing_browser_without_claiming_png(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tp, "available_browser", return_value=None):
            result = tp.create_card(self.demo, tp.validate(self.demo, AS_OF), folder)
            self.assertEqual(result["images"], [])
            self.assertIn("image_error", result)
            self.assertTrue(Path(result["text"]).is_file())
            self.assertTrue(Path(result["html"]).is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
