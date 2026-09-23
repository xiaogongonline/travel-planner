"""Offline regression tests, with synthetic fixtures and automatically cleaned temporary files."""
import copy
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest
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
        self.assertEqual(total["remaining_low"], 830)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
