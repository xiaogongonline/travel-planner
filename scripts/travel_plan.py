#!/usr/bin/env python3
"""Portable, offline travel-plan validation and HTML rendering. Standard library only."""
from __future__ import annotations

import argparse
from functools import lru_cache
from datetime import date, datetime
from decimal import Decimal
import html
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
STATUSES = {"exploration": "探索方案", "conditional": "条件性方案", "ready": "关键安排已落实"}
BOOKING = {"not_required": "无需预约", "not_open": "尚未开放", "pending": "待完成",
           "confirmed": "已确认", "unavailable": "无法落实", "unknown": "尚未核实"}
KINDS = {"verified": "已核实", "user": "用户提供", "estimate": "估算",
         "assumption": "假设", "unknown": "待核实"}

# A deliberately small strict contract: itinerary fields are required; card is optional.
# ("nullable", type) is the only special tuple; all other tuples are enums.
NSTR = ("nullable", str)
NNUM = ("nullable", float)
SOURCE = {"id": str, "name": str, "kind": ("official", "provider", "secondary", "user", "estimate", "demo"),
          "url": str, "checked_at": str, "valid_from": NSTR, "valid_to": NSTR,
          "applies_to": str, "summary": str, "recheck_on": NSTR, "conflict": str}
FACT = {"label": str, "value": str, "kind": tuple(KINDS), "basis": str,
        "source_ids": [str], "critical": bool}
PLACE = {"id": str, "name": str, "address": str, "search_terms": str,
         "notes": str, "facts": [FACT]}
BOOK = {"id": str, "title": str, "status": tuple(BOOKING), "critical": bool,
        "date": NSTR, "confirmed_evidence": str, "source_ids": [str], "notes": str}
ITEM = {"id": str, "title": str, "kind": ("visit", "transport", "meal", "rest", "lodging", "other"),
        "start": str, "end": str, "place_id": NSTR, "description": str,
        "minimum_minutes": float, "buffer_minutes": float, "booking_id": NSTR,
        "source_ids": [str], "windows": [{"start": str, "end": str}], "notes": str}
CARD = {
    "author": str,
    "meeting": {"time": NSTR, "place": str},
    "packing": [str],
    "roles": [{"role": str, "assignee": NSTR}],
    "polls": [{"question": str, "options": [str], "deadline": NSTR}],
    "aa": {"members": [{"id": str, "name": str}],
           "expenses": [{"label": str, "amount": str, "status": ("planned", "paid"),
                         "payer_id": NSTR, "participant_ids": [str]}]},
}
OPTIONAL_FIELDS = {"$": {"card"}, "$.card": set(CARD)}
SCHEMA = {
    "schema_version": int, "title": str, "revision": str, "generated_at": str,
    "is_demo": bool, "status": tuple(STATUSES),
    "trip": {"origin": str, "destination": str, "start_date": NSTR, "end_date": NSTR,
             "timezone": str, "travelers": int, "style": str, "lodging": str},
    "budget": {"currency": str, "limit": NNUM, "hard_limit": bool,
               "scope": ("total", "remaining"), "basis": str, "complete": bool},
    "constraints": [{"id": str, "text": str, "kind": ("hard", "preference"),
                     "status": ("satisfied", "violated", "unknown"), "evidence": str}],
    "assumptions": [str], "sources": [SOURCE], "places": [PLACE], "bookings": [BOOK],
    "days": [{"date": str, "summary": str, "intensity": ("轻松", "适中", "偏累", "待评估"),
              "intensity_basis": str, "items": [ITEM],
              "alternatives": [{"trigger": str, "action": str, "dependencies": str,
                                "status": ("checked", "conditional"), "source_ids": [str]}]}],
    "costs": [{"id": str, "category": str, "label": str, "unit_low": float, "unit_high": float,
               "quantity": float, "unit": str, "paid": float, "optional": bool,
               "kind": ("confirmed", "quote", "estimate"), "basis": str, "dates": [str], "source_ids": [str]}],
    "tasks": [{"id": str, "action": str, "deadline": NSTR, "channel": str, "impact": str,
               "status": ("pending", "done"), "critical": bool}],
    "risks": [{"trigger": str, "action": str, "critical": bool, "resolved": bool}],
    "emergency": [{"label": str, "contact": str, "basis": str, "source_ids": [str]}],
    "changes": [str],
    "card": CARD,
}


def schema_errors(value, spec=SCHEMA, path="$"):
    errors = []
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            return [f"{path}: 必须是对象"]
        for key in spec:
            if key not in value:
                if key not in OPTIONAL_FIELDS.get(path, set()):
                    errors.append(f"{path}.{key}: 缺少字段")
            else:
                errors.extend(schema_errors(value[key], spec[key], f"{path}.{key}"))
        for key in value.keys() - spec.keys():
            errors.append(f"{path}.{key}: 未知字段；不得夹带未定义的个人信息")
    elif isinstance(spec, list):
        if not isinstance(value, list):
            return [f"{path}: 必须是数组"]
        for i, member in enumerate(value):
            errors.extend(schema_errors(member, spec[0], f"{path}[{i}]"))
    elif isinstance(spec, tuple):
        if spec[0] == "nullable":
            if value is not None:
                errors.extend(schema_errors(value, spec[1], path))
        elif value not in spec:
            errors.append(f"{path}: 值必须为 {spec}")
    elif spec is float:
        if type(value) not in (int, float) or not 0 <= value <= 1_000_000_000:
            errors.append(f"{path}: 必须是 0 至 10 亿的有限数值")
    elif type(value) is not spec:
        errors.append(f"{path}: 类型应为 {spec.__name__}")
    return errors


def stamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or "T" not in value:
        raise ValueError("时间必须为含时区偏移的 ISO 8601")
    return result


def day(value):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期必须为 YYYY-MM-DD")
    return date.fromisoformat(value)


def web_url(value):
    parts = urlsplit(value)
    return parts.scheme in ("https", "http") and bool(parts.netloc) and not parts.username


def validate(plan, as_of=None):
    today = day(as_of) if as_of else date.today()
    result = {"schema_errors": schema_errors(plan), "blockers": [], "warnings": [],
              "effective_status": "exploration", "budget": {}, "checked_on": today.isoformat()}
    errors, blockers, warnings = result["schema_errors"], result["blockers"], result["warnings"]
    if errors:
        return result
    if plan["schema_version"] != 1:
        errors.append("schema_version: 仅支持 1")
    if plan["trip"]["travelers"] < 1:
        errors.append("trip.travelers: 必须至少为 1")
    # Check formats before arithmetic. Invalid data cannot be exported as a draft.
    dates = [("trip.start_date", plan["trip"]["start_date"]), ("trip.end_date", plan["trip"]["end_date"])]
    stamps = [("generated_at", plan["generated_at"])]
    for s in plan["sources"]:
        stamps.append((f"source:{s['id']}.checked_at", s["checked_at"]))
        dates.extend((f"source:{s['id']}.{k}", s[k]) for k in ("valid_from", "valid_to", "recheck_on"))
        if s["url"] and not web_url(s["url"]):
            errors.append(f"source:{s['id']}: 仅允许 http(s) 来源链接")
    for b in plan["bookings"]:
        dates.append((f"booking:{b['id']}.date", b["date"]))
    for c in plan["costs"]:
        dates.extend((f"cost:{c['id']}.dates", v) for v in c["dates"])
    for t in plan["tasks"]:
        if t["deadline"]:
            stamps.append((f"task:{t['id']}.deadline", t["deadline"]))
    card = plan.get("card", {})
    if card.get("meeting") and card["meeting"]["time"]:
        stamps.append(("card.meeting.time", card["meeting"]["time"]))
    for n, poll in enumerate(card.get("polls", []), 1):
        if poll["deadline"]:
            stamps.append((f"card.polls[{n}].deadline", poll["deadline"]))
        if len(poll["options"]) < 2 or any(not option.strip() for option in poll["options"]):
            errors.append(f"card.polls[{n}]: 至少两个非空选项")
        if not poll["question"].strip():
            errors.append(f"card.polls[{n}]: 问题不能为空")
    for n, role in enumerate(card.get("roles", []), 1):
        if not role["role"].strip():
            errors.append(f"card.roles[{n}]: 角色不能为空")
    if any(not item.strip() for item in card.get("packing", [])):
        errors.append("card.packing: 物品名称不能为空")
    aa = card.get("aa", {})
    members = aa.get("members", [])
    member_ids = [member["id"] for member in members]
    if len(set(member_ids)) != len(member_ids) or any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", ident) for ident in member_ids):
        errors.append("card.aa.members: 成员 ID 非法或重复")
    if any(not member["name"].strip() for member in members):
        errors.append("card.aa.members: 成员名不能为空")
    if len({member["name"].strip() for member in members}) != len(members):
        errors.append("card.aa.members: 群内显示名不能重复")
    for n, expense in enumerate(aa.get("expenses", []), 1):
        amount = expense["amount"]
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,2})?", amount) or Decimal(amount) <= 0:
            errors.append(f"card.aa.expenses[{n}]: 金额须为大于零、精确到分的十进制字符串")
        ids = expense["participant_ids"]
        if not ids or len(ids) != len(set(ids)) or any(ident not in member_ids for ident in ids):
            errors.append(f"card.aa.expenses[{n}]: 分摊成员为空、重复或不存在")
        payer = expense["payer_id"]
        if payer is not None and payer not in member_ids:
            errors.append(f"card.aa.expenses[{n}]: 付款人不存在")
        if expense["status"] == "paid" and payer is None:
            errors.append(f"card.aa.expenses[{n}]: 已付款支出必须指定付款人")
        if not expense["label"].strip():
            errors.append(f"card.aa.expenses[{n}]: 项目名称不能为空")
    for d in plan["days"]:
        dates.append(("day.date", d["date"]))
        for i in d["items"]:
            stamps.extend((f"item:{i['id']}.{k}", i[k]) for k in ("start", "end"))
            for w in i["windows"]:
                stamps.extend((f"window:{i['id']}.{k}", w[k]) for k in ("start", "end"))
    for path, value in dates:
        if value is not None:
            try:
                day(value)
            except (ValueError, TypeError):
                errors.append(f"{path}: 无效日期")
    for path, value in stamps:
        try:
            stamp(value)
        except (ValueError, TypeError):
            errors.append(f"{path}: 无效时间；使用 YYYY-MM-DDTHH:MM:SS+08:00 等带偏移格式")
    all_ids = set()
    for group in ("sources", "places", "bookings", "constraints", "costs", "tasks"):
        for obj in plan[group]:
            ident = obj["id"]
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", ident) or ident in all_ids:
                errors.append(f"{group}: 非法或重复 id {ident}")
            all_ids.add(ident)
    items = [i for d in plan["days"] for i in d["items"]]
    for i in items:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", i["id"]) or i["id"] in all_ids:
            errors.append(f"item: 非法或重复 id {i['id']}")
        all_ids.add(i["id"])
    sources = {s["id"]: s for s in plan["sources"]}
    places = {p["id"]: p for p in plan["places"]}
    bookings = {b["id"]: b for b in plan["bookings"]}

    def refs(obj, label):
        for sid in obj.get("source_ids", []):
            if sid not in sources:
                errors.append(f"{label}: 来源 {sid} 不存在")
    for group in ("bookings", "costs", "emergency"):
        for obj in plan[group]:
            refs(obj, group)
    for p in plan["places"]:
        for f in p["facts"]:
            refs(f, p["id"])
    for i in items:
        refs(i, i["id"])
        for key, catalog in (("place_id", places), ("booking_id", bookings)):
            if i[key] is not None and i[key] not in catalog:
                errors.append(f"{i['id']}: {key} 引用不存在")
    for d in plan["days"]:
        for a in d["alternatives"]:
            refs(a, "alternative")
    if errors:
        return result
    trip = plan["trip"]
    start = day(trip["start_date"]) if trip["start_date"] else None
    end = day(trip["end_date"]) if trip["end_date"] else None
    if start and end and start > end:
        errors.append("trip: 开始日期晚于结束日期")
    if not start or not end or not trip["origin"].strip() or not trip["destination"].strip():
        blockers.append("出发地、目的地或旅行日期尚未确定")
    if not plan["days"] or not items:
        blockers.append("尚无可执行日程")
    if not trip["lodging"].strip():
        warnings.append("住宿或无需住宿的说明尚未填写")
    if stamp(plan["generated_at"]).date() > today:
        warnings.append("文件生成时间晚于本次检查日期，请检查设备时间")

    def source_applies(sid, on_date, critical=True):
        s = sources[sid]
        issues = []
        if (s["valid_from"] and on_date < day(s["valid_from"])) or (s["valid_to"] and on_date > day(s["valid_to"])):
            issues.append(f"来源 {sid} 不适用于 {on_date}，需重新核实")
        if s["conflict"]:
            issues.append(f"来源 {sid} 存在冲突：{s['conflict']}")
        if s["recheck_on"] and today >= day(s["recheck_on"]):
            issues.append(f"来源 {sid} 已到复核日期 {s['recheck_on']}，需重新核实")
        (blockers if critical else warnings).extend(issues)

    for s in plan["sources"]:
        if s["valid_from"] and s["valid_to"] and day(s["valid_from"]) > day(s["valid_to"]):
            errors.append(f"source:{s['id']}: 适用日期倒置")
        if stamp(s["checked_at"]).date() > today:
            blockers.append(f"来源 {s['id']} 的核实日期在未来")
        if s["recheck_on"] and today >= day(s["recheck_on"]):
            warnings.append(f"来源 {s['id']} 已到复核日期 {s['recheck_on']}，需重新核实")
        if not s["applies_to"].strip():
            blockers.append(f"来源 {s['id']} 缺少适用条件")
        if s["kind"] in ("official", "provider", "secondary") and not s["url"]:
            blockers.append(f"来源 {s['id']} 缺少可追溯链接")
        if s["kind"] == "demo" and not plan["is_demo"]:
            blockers.append("模拟来源不可作为真实旅行证据")
    for c in plan["constraints"]:
        if c["kind"] == "hard" and (c["status"] != "satisfied" or not c["evidence"].strip()):
            blockers.append(f"硬约束未满足或未验证：{c['text']}")
    for b in plan["bookings"]:
        if b["status"] == "confirmed" and (not b["confirmed_evidence"].strip() or not b["date"]):
            blockers.append(f"预约 {b['title']} 缺少确认依据或日期")
        if b["critical"] and b["status"] not in ("confirmed", "not_required"):
            blockers.append(f"关键安排未落实：{b['title']}（{BOOKING[b['status']]}）")
        for sid in b["source_ids"]:
            if b["date"]:
                source_applies(sid, day(b["date"]), b["critical"])
    for p in plan["places"]:
        if not p["address"].strip():
            warnings.append(f"{p['name']} 地址未定；交通需复核")
        for f in p["facts"]:
            if f["kind"] == "verified" and not f["source_ids"]:
                blockers.append(f"{p['name']} / {f['label']} 声称已核实但没有来源")
            if f["critical"] and f["kind"] in ("unknown", "assumption"):
                blockers.append(f"关键事实未落实：{p['name']} / {f['label']}")
            if f["kind"] in ("estimate", "assumption", "user") and not f["basis"].strip():
                warnings.append(f"{p['name']} / {f['label']} 缺少依据")
            if f["kind"] == "verified" and any(sources[s]["kind"] in ("estimate", "user") for s in f["source_ids"]):
                blockers.append(f"{p['name']} / {f['label']} 不能把用户信息或估算标记为外部核实")
    seen_dates = set()
    timed = []
    for d in plan["days"]:
        on_date = day(d["date"])
        if d["date"] in seen_dates:
            errors.append(f"重复日程日期 {d['date']}")
        seen_dates.add(d["date"])
        if (start and on_date < start) or (end and on_date > end):
            blockers.append(f"日程 {d['date']} 超出旅行日期")
        if not d["items"]:
            warnings.append(f"{d['date']} 尚无活动；明确是否自由日")
        if d["intensity"] == "待评估" or not d["intensity_basis"].strip():
            warnings.append(f"{d['date']} 强度尚未评估")
        for i in d["items"]:
            a, b = stamp(i["start"]), stamp(i["end"])
            timed.append((a, b, i))
            if a.date() != on_date:
                blockers.append(f"{i['title']} 开始日期与所在日不一致")
            if b <= a:
                blockers.append(f"{i['title']} 结束不晚于开始")
            if end and b.date() > end:
                blockers.append(f"{i['title']} 结束时间超出旅行结束日")
            duration = (b - a).total_seconds() / 60
            if duration < i["minimum_minutes"] + i["buffer_minutes"]:
                blockers.append(f"{i['title']} 时间槽不足以容纳所需时长及缓冲")
            if i["kind"] == "transport" and i["buffer_minutes"] == 0:
                warnings.append(f"{i['title']} 没有交通缓冲，请确认是否合理")
            for w in i["windows"]:
                if stamp(w["end"]) <= stamp(w["start"]):
                    errors.append(f"{i['title']} 可用时间窗倒置")
            if i["windows"] and not any(stamp(w["start"]) <= a and b <= stamp(w["end"]) for w in i["windows"]):
                blockers.append(f"{i['title']} 不在所填可用时间窗内")
            if i["booking_id"]:
                booking = bookings[i["booking_id"]]
                if booking["date"] and booking["date"] != a.date().isoformat():
                    blockers.append(f"{i['title']} 与预约日期不一致")
            linked = list(i["source_ids"])
            if i["place_id"]:
                for f in places[i["place_id"]]["facts"]:
                    for sid in f["source_ids"]:
                        source_applies(sid, on_date, f["critical"])
            for sid in linked:
                source_applies(sid, on_date)
        for a in d["alternatives"]:
            if a["status"] == "checked" and not a["dependencies"].strip():
                blockers.append(f"{d['date']} 备选缺少依赖检查")
            for sid in a["source_ids"]:
                source_applies(sid, on_date, False)
    # One shared itinerary; parallel subgroups require separate plans, not overlapping events.
    timed.sort(key=lambda t: t[0])
    latest_end = None
    latest_title = ""
    for a, b, i in timed:
        if latest_end is not None and a < latest_end:
            blockers.append(f"活动时间重叠：{latest_title} / {i['title']}")
        if latest_end is None or b > latest_end:
            latest_end, latest_title = b, i["title"]
    if start and end and (end - start).days < 370:
        missing = (end - start).days + 1 - len(seen_dates)
        if missing > 0:
            warnings.append(f"旅行范围中有 {missing} 天未列日程，需说明自由日或交通日")
    for t in plan["tasks"]:
        if t["status"] == "pending":
            if t["critical"]:
                blockers.append(f"关键待办未完成：{t['action']}")
            if t["deadline"] and stamp(t["deadline"]).date() < today:
                warnings.append(f"待办已过截止日期：{t['action']}")
    for r in plan["risks"]:
        if r["critical"] and not r["resolved"]:
            blockers.append(f"关键未知或风险未解决：{r['trigger']}")
    totals = {"required_low": Decimal(0), "required_high": Decimal(0), "remaining_low": Decimal(0),
              "remaining_high": Decimal(0), "paid": Decimal(0), "optional_low": Decimal(0), "optional_high": Decimal(0)}
    for c in plan["costs"]:
        lo, hi = cost_range(c)
        paid = Decimal(str(c["paid"]))
        if lo > hi or paid > hi:
            errors.append(f"费用 {c['label']} 区间倒置或已支付超过总价上端")
        if c["kind"] == "confirmed" and lo != hi:
            blockers.append(f"费用 {c['label']} 标记已确认却仍是区间")
        if not c["basis"].strip():
            warnings.append(f"费用 {c['label']} 缺少计价条件")
        if c["optional"]:
            totals["optional_low"] += lo
            totals["optional_high"] += hi
        else:
            totals["required_low"] += lo
            totals["required_high"] += hi
            totals["remaining_low"] += max(Decimal(0), lo - paid)
            totals["remaining_high"] += max(Decimal(0), hi - paid)
            totals["paid"] += paid
        if c["source_ids"] and not c["dates"]:
            blockers.append(f"费用 {c['label']} 缺少报价适用日期")
        for on_date in c["dates"]:
            if (start and day(on_date) < start) or (end and day(on_date) > end):
                blockers.append(f"费用 {c['label']} 的适用日期超出旅行范围")
            for sid in c["source_ids"]:
                source_applies(sid, day(on_date))
    result["budget"] = {k: float(v.quantize(Decimal("0.01"))) for k, v in totals.items()}
    budget = plan["budget"]
    if not budget["complete"] or not plan["costs"]:
        blockers.append("费用范围尚未完整覆盖，不能确认预算可行性")
    if budget["limit"] is not None:
        value = totals["required_high" if budget["scope"] == "total" else "remaining_high"]
        if value > Decimal(str(budget["limit"])):
            (blockers if budget["hard_limit"] else warnings).append("必需费用区间上端超过预算上限")
    if plan["status"] == "ready" and blockers:
        warnings.append("请求的 ready 状态已降级，不能宣称关键安排均已落实")
    result["blockers"] = list(dict.fromkeys(blockers))
    result["warnings"] = [issue for issue in dict.fromkeys(warnings) if issue not in result["blockers"]]
    result["effective_status"] = "exploration" if plan["status"] == "exploration" else ("conditional" if blockers else plan["status"])
    return result


def cost_range(cost):
    quantity = Decimal(str(cost["quantity"]))
    return tuple(Decimal(str(cost[key])) * quantity for key in ("unit_low", "unit_high"))


def esc(value):
    return html.escape(str(value), quote=True)


def ul(values):
    return "<ul>" + "".join(f"<li>{esc(v)}</li>" for v in values) + "</ul>" if values else "<p class=\"muted\">无额外事项。</p>"


def ref_links(ids, order, names):
    return " ".join(
        f'<a class="source-ref" href="#source-{esc(s)}" title="资料 {esc(s)}：{esc(names.get(s, ""))}">'
        f'<sup>{order.get(s, "?")}</sup></a>' for s in ids)


def date_label(value, show_tz=True):
    if not value:
        return "待定"
    d = stamp(value)
    label = f"{d:%m-%d %H:%M}"
    if show_tz:
        label += f" UTC{d:%z}"[:-2] + ":" + f"{d:%z}"[-2:]
    return label


def section(ident, title, body):
    number = ident[4:] if ident.startswith("day-") else ""
    heading = (f'<header class="day-heading"><span class="day-number" aria-hidden="true">{int(number):02d}</span>'
               f'<h2>{esc(title)}</h2></header>') if number else f'<h2>{esc(title)}</h2>'
    return f'<section id="{ident}" class="panel{" day-panel" if number else ""}">{heading}{body}</section>'


def activity_time(start, end, show_tz=True):
    first, last = stamp(start), stamp(end)
    if first.date() == last.date() and first.utcoffset() == last.utcoffset():
        context = f'{first:%m-%d} · UTC{date_label(start).split(" UTC")[1]}' if show_tz else f'{first:%m-%d}'
    else:
        context = f'{date_label(start)} → {date_label(end)}'
    return (f'<p class="time"><time datetime="{esc(start)}">{first:%H:%M}</time>'
            f'<span class="time-end">— {last:%H:%M}</span><small>{esc(context)}</small></p>')


def checklist_key(plan):
    # Re-exporting unchanged content preserves progress; actual content/revision changes reset it.
    content = {key: value for key, value in plan.items() if key != "generated_at"}
    encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "travel-planner:tasks:v1:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def display_currency(currency):
    return "元" if currency == "CNY" else currency


def render(plan, report):
    if report["schema_errors"]:
        raise ValueError("结构错误，拒绝生成")
    places = {p["id"]: p for p in plan["places"]}
    bookings = {b["id"]: b for b in plan["bookings"]}
    source_order = {s["id"]: n for n, s in enumerate(plan["sources"], 1)}
    source_names = {s["id"]: s["name"] for s in plan["sources"]}
    # Single-offset trips (e.g. domestic) read fine without UTC annotations;
    # mixed offsets mean cross-timezone travel, where the annotation prevents misreading.
    offsets = {stamp(i[k]).utcoffset() for d in plan["days"] for i in d["items"] for k in ("start", "end")}
    show_tz = len(offsets) > 1

    def refs(ids):
        return ref_links(ids, source_order, source_names)
    nav = '<a href="#overview">这一趟</a><a href="#tasks">出发清单</a>'
    body = ""
    demo = '<p class="demo-note">演示手帐 · 地点、价格与订单均为模拟，不可用于出行</p>' if plan["is_demo"] else ""
    trip = plan["trip"]
    tasks_total = len(plan["tasks"])
    tasks_done = sum(1 for t in plan["tasks"] if t["status"] == "done")
    b = report["budget"]
    currency = esc(display_currency(plan["budget"]["currency"]))
    budget_range = f'{b["required_low"]:,.0f}–{b["required_high"]:,.0f}' if plan["costs"] else ""
    dates = f'{esc(trip["start_date"] or "日期待定")} — {esc(trip["end_date"] or "日期待定")}'
    meta_bits = [dates, f'{trip["travelers"]} 人同行']
    if budget_range:
        meta_bits.append(f'预计必需花费 {budget_range} {currency}')
    overview = demo + '<div class="cover">'
    overview += f'<p class="cover-meta">{" · ".join(meta_bits)}</p>'
    overview += ('<p class="kicker">把日子，过在路上</p>'
                 f'<h1>{esc(plan["title"])}</h1><p class="lead">{esc(trip["origin"] or "出发地待定")} '
                 '<svg class="lead-arrow" viewBox="0 0 60 24" fill="none" aria-hidden="true">'
                 '<path d="M2 19 C18 5 38 21 55 6 M47 4l9 1-2 9" stroke="currentColor" stroke-width="2" '
                 'stroke-linecap="round" stroke-linejoin="round"/></svg>'
                 f' {esc(trip["destination"] or "目的地待定")}</p>')
    overview += f'<p class="trip-style">{esc(trip["style"])}</p>'
    day_fig = f'{len(plan["days"])}<small> 天</small>' if plan["days"] else "待定"
    budget_fig = f'{budget_range}<small> {currency}</small>' if budget_range else "路上再算"
    todo_fig = f'{tasks_total - tasks_done}<small> 件待办 · 共 {tasks_total} 件</small>' if tasks_total else "暂无清单"
    overview += ('<dl class="cover-figures">'
                 f'<div><dt>行程</dt><dd>{day_fig}</dd></div>'
                 f'<div><dt>预计必需花费</dt><dd>{budget_fig}</dd></div>'
                 f'<div><dt>出发清单</dt><dd>{todo_fig}</dd></div></dl></div>')
    overview += f'<div class="stay-note"><span class="eyebrow">{esc(STATUSES[report["effective_status"]])}</span><p><span class="muted">住在哪里</span><br>{esc(trip["lodging"] or "还没定，确认后再看看怎么走")}</p></div>'
    if report["blockers"]:
        overview += '<div class="alert"><strong>出发前，还差这几件事</strong>' + ul(report["blockers"]) + "</div>"
    if report["warnings"]:
        overview += "<h3>需要留意</h3>" + ul(report["warnings"])
    if plan["assumptions"] or plan["constraints"]:
        overview += '<details class="planning-notes"><summary>这趟旅行，按你的节奏</summary>'
        if plan["assumptions"]:
            overview += "<h3>暂时这样打算</h3>" + ul(plan["assumptions"])
        if plan["constraints"]:
            labels = {"satisfied": "已安排", "violated": "还需调整", "unknown": "待确认"}
            overview += ul(f'{c["text"]} · {labels[c["status"]]} · {c["evidence"] or "待确认"}' for c in plan["constraints"])
        overview += '</details>'
    body += section("overview", "我的旅行手帐", overview)
    todo = ""
    for t in sorted(plan["tasks"], key=lambda t: (t["status"] == "done", not t["critical"], t["deadline"] or "9999")):
        done = t["status"] == "done"
        todo += (f'<article class="task checklist-task{" is-done" if done else ""}" data-task-id="{esc(t["id"])}">'
                 f'<span class="tag"><span data-task-status>{"已完成" if done else "待办"}</span>{" · 关键" if t["critical"] else ""}</span>'
                 f'<h3><label class="task-label" for="task-check-{esc(t["id"])}">'
                 f'<input type="checkbox" id="task-check-{esc(t["id"])}" autocomplete="off" disabled{" checked" if done else ""}>'
                 f'<span>{esc(t["action"])}</span></label></h3>'
                 f'<p class="task-deadline">{esc(date_label(t["deadline"], show_tz)) + " 前完成" if t["deadline"] else "时间待定"}</p>'
                 f'<p>{esc(t["channel"])}</p><p class="muted">耽误了会影响：{esc(t["impact"])}</p></article>')
    if todo:
        todo = (f'<div data-checklist-key="{checklist_key(plan)}">'
                '<p class="section-intro">一件件准备好，就可以轻装出发。</p>'
                '<p class="save-status" id="task-save-status" role="status" aria-live="polite">启用脚本后，可以勾选并保存进度。</p>'
                '<details class="checklist-help"><summary>关于勾选记录</summary><p>进度保存在当前浏览器；换设备、清理数据或改变文件打开方式后可能无法恢复。勾选只记录个人进度，订单状态和上方核验结论仍以生成时的记录为准。</p></details>'
                + todo + '</div>')
    tasks_section = section("tasks", "出发前的小清单", todo or "<p>暂无记录；不代表所有预约都已完成。</p>")
    for number, d in enumerate(sorted(plan["days"], key=lambda x: x["date"]), 1):
        ident = f"day-{number}"
        nav += f'<a href="#{ident}">第 {number} 天</a>'
        daily = f'<p class="route">{esc(d["summary"])}</p><p><span class="tag">{esc(d["intensity"])}</span> {esc(d["intensity_basis"])}</p>'
        for i in sorted(d["items"], key=lambda x: stamp(x["start"])):
            daily += f'<article class="stop">{activity_time(i["start"], i["end"], show_tz)}<div class="stop-body"><h3>{esc(i["title"])}</h3><p>{esc(i["description"])}</p>'
            if i["place_id"]:
                p = places[i["place_id"]]
                daily += f'<p><strong>{esc(p["name"])}</strong></p><div class="address"><span id="address-{esc(i["id"])}">{esc(p["address"] or "地址待确认")}</span>'
                daily += f'<button type="button" class="copy" hidden data-copy="address-{esc(i["id"])}">复制地址</button></div>'
                daily += f'<p class="muted">地图搜索词：{esc(p["search_terms"] or p["name"])}</p><p>{esc(p["notes"])}</p>'
                for f in p["facts"]:
                    tone = "unk" if f["kind"] in ("assumption", "unknown") else ""
                    daily += (f'<p class="fact"><strong>{esc(f["label"])}</strong>：{esc(f["value"])} '
                              f'<span class="fact-kind {tone}">{esc(KINDS[f["kind"]])}</span>'
                              f'<br><small>{esc(f["basis"])} {refs(f["source_ids"])}</small></p>')
            if i["kind"] == "transport":
                daily += f'<p>路程/交通依据见上方说明；预计至少 {i["minimum_minutes"]:g} 分钟，另留 {i["buffer_minutes"]:g} 分钟缓冲。</p>'
            if i["booking_id"]:
                b = bookings[i["booking_id"]]
                daily += f'<p class="booking"><strong>预约：{esc(BOOKING[b["status"]])}</strong> · {esc(b["title"])}</p>'
            if i["notes"]:
                daily += f'<p class="note">{esc(i["notes"])}</p>'
            daily += refs(i["source_ids"]) + "</div></article>"
        for a in d["alternatives"]:
            daily += f'<aside class="alternative"><h3>如果 {esc(a["trigger"])}</h3><p>{esc(a["action"])}</p><p>依赖：{esc(a["dependencies"])}</p><p>状态：{"已检查" if a["status"] == "checked" else "条件待落实"} {refs(a["source_ids"])}</p></aside>'
        body += section(ident, f'第 {number} 天 · {d["date"]}', daily)
    body += tasks_section
    nav += '<a href="#budget">旅行花费</a><a href="#bookings">预约</a><a href="#sources">资料夹</a>'
    costs = ""
    for c in plan["costs"]:
        low, high = cost_range(c)
        costs += f'<article class="cost"><h3>{esc(c["label"])}{" · 可花可不花" if c["optional"] else ""}</h3><p class="amount">{low:,.2f}–{high:,.2f} {currency}</p><p class="cost-detail">单价 {c["unit_low"]:g}–{c["unit_high"]:g} / {esc(c["unit"])} × {c["quantity"]:g}；已付 {c["paid"]:g}</p><p>{esc(c["basis"])} {refs(c["source_ids"])}</p></article>'
    b = report["budget"]
    budget_intro = f'<p>{esc(plan["budget"]["basis"])} · 预算上限：{esc(plan["budget"]["limit"] if plan["budget"]["limit"] is not None else "未设定")} {currency}（{"全程总额" if plan["budget"]["scope"] == "total" else "剩余支出"}）</p>'
    budget_intro += f'<div class="totals"><p>一定要花的 <strong>{b["required_low"]:,.2f}–{b["required_high"]:,.2f}</strong></p><p>其中已付 {b["paid"]:,.2f} · 路上还要付 {b["remaining_low"]:,.2f}–{b["remaining_high"]:,.2f}</p><p>可花可不花的 {b["optional_low"]:,.2f}–{b["optional_high"]:,.2f}；以上均为 {currency}</p></div>'
    body += section("budget", "这趟，大概花多少", budget_intro + '<div class="cost-list">' + costs + '</div>')
    booking_html = ""
    for b in plan["bookings"]:
        booking_html += f'<article class="task"><h3>{esc(b["title"])}</h3><p><span class="tag">{esc(BOOKING[b["status"]])}</span> {esc(b["date"] or "日期待定")}</p><p>{esc(b["confirmed_evidence"] or "还没拿到确认凭证")}</p><p>{esc(b["notes"])} {refs(b["source_ids"])}</p></article>'
    body += section("bookings", "订好的，和还在等的", booking_html or "<p>尚无记录。</p>")
    if plan["risks"]:
        body += section("risks", "变化时怎么办", ul(f'{r["trigger"]} → {r["action"]}（{"已处理" if r["resolved"] else "待处理"}）' for r in plan["risks"]))
    if plan["emergency"]:
        body += section("emergency", "紧急联系", "".join(f'<p><strong>{esc(e["label"])}：{esc(e["contact"])}</strong><br>{esc(e["basis"])} {refs(e["source_ids"])}</p>' for e in plan["emergency"]))
    evidence = ""
    for n, s in enumerate(plan["sources"], 1):
        link = f'<a href="{esc(s["url"])}" target="_blank" rel="noopener noreferrer">打开出处（需联网）</a>' if s["url"] else "无在线链接"
        conflict = f'<p class="source-conflict">记录之间有出入：{esc(s["conflict"])}</p>' if s["conflict"] else ""
        evidence += (f'<article id="source-{esc(s["id"])}" class="source">'
                     f'<h3><span class="source-no">{n}</span>{esc(s["name"])}<span class="source-id">{esc(s["id"])}</span></h3>'
                     f'<p>{esc(s["summary"])}</p>{conflict}<p>{link}</p>'
                     f'<details class="source-meta"><summary>查证记录</summary>'
                     f'<p>查于 {esc(date_label(s["checked_at"], show_tz))}；{esc(s["applies_to"])}</p>'
                     f'<p>适用于 {esc(s["valid_from"] or "起始适用日期未确认")} 至 {esc(s["valid_to"] or "截止适用日期未确认")}；约定复核：{esc(s["recheck_on"] or "按变化触发")}</p>'
                     f'</details></article>')
    body += section("sources", "随身资料夹", evidence or "<p>尚无外部来源，不能视为已完成事实核实。</p>")
    if plan["changes"]:
        body += section("changes", "本版变化", ul(plan["changes"]))
    author = public_text(plan.get("card", {}).get("author") or "杰纶hhh")
    footer = (f'<span class="footer-credit">由 travel-planner 生成 · @{esc(author)}</span>'
              f'第 {esc(plan["revision"])} 版手帐 · 整理于 {esc(date_label(plan["generated_at"], show_tz))} · {esc(report["checked_on"])} 做过一轮格式检查。整理时间不等于各条信息的核实时间。')
    template = (ROOT / "assets" / "travel-template.html").read_text(encoding="utf-8")
    # One-pass substitution keeps user content containing template syntax inert.
    tokens = {"TITLE": esc(plan["title"]), "NAV": nav, "BODY": body, "FOOTER": footer}
    return re.sub(r"@@(TITLE|NAV|BODY|FOOTER)@@", lambda m: tokens[m[1]], template)


class DependencyParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.issues = []
        self.scripts = []
        self.styles = []
        self.mode = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for key in ("src", "poster", "data", "srcset"):
            value = attrs.get(key, "")
            if value and not value.startswith("data:"):
                self.issues.append(f"{tag}.{key}: 外部或本地附属资源 {value[:120]}")
        if tag == "link":
            self.issues.append("link: 不能依赖外部样式、字体或预加载")
        if tag in ("iframe", "object", "embed", "base"):
            self.issues.append(f"{tag}: 不允许作为自包含核心内容")
        if tag == "meta" and attrs.get("http-equiv", "").lower() == "refresh":
            self.issues.append("meta refresh: 自动跳转")
        if tag == "script" and attrs.get("type") == "module":
            self.issues.append("module script: 本地兼容性需独立验证")
        if tag in ("image", "use"):
            for key in ("href", "xlink:href"):
                value = attrs.get(key, "")
                if value and not value.startswith(("data:", "#")):
                    self.issues.append(f"SVG {tag}.{key}: 非自包含资源")
        for key, value in attrs.items():
            if key.startswith("on") and value:
                self.scripts.append(value)
        if tag in ("script", "style"):
            self.mode = tag
        if attrs.get("style"):
            self.styles.append(attrs["style"])

    def handle_endtag(self, tag):
        if tag == self.mode:
            self.mode = None

    def handle_data(self, data):
        if self.mode == "script":
            self.scripts.append(data)
        if self.mode == "style":
            self.styles.append(data)


def audit(text):
    parser = DependencyParser()
    parser.feed(text)
    script = "\n".join(parser.scripts)
    style = "\n".join(parser.styles)
    if re.search(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|importScripts)\s*\(|\bimport\s*(?:\(|[{'\"*])|navigator\.sendBeacon|serviceWorker", script):
        parser.issues.append("script: 存在网络或模块加载接口，需移除核心依赖")
    if re.search(r"@import\b", style, re.I):
        parser.issues.append("CSS @import")
    for resource in re.findall(r"url\(\s*['\"]?([^)'\"\s]+)", style, re.I):
        if not resource.startswith(("data:", "#")):
            parser.issues.append(f"CSS 外部资源 {resource[:120]}")
    return {"passed": not parser.issues, "issues": parser.issues,
            "scope": "静态依赖检查；仍需浏览器断网、本地文件和目标设备验证"}


def blank():
    return {"schema_version": 1, "title": "待规划旅行", "revision": "1",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "is_demo": False, "status": "exploration",
            "trip": {"origin": "", "destination": "", "start_date": None, "end_date": None,
                     "timezone": "待确认", "travelers": 1, "style": "", "lodging": ""},
            "budget": {"currency": "CNY", "limit": None, "hard_limit": False,
                       "scope": "total", "basis": "人数、费用范围、已支付口径待确认", "complete": False},
            **{k: [] for k in ("constraints", "assumptions", "sources", "places", "bookings",
                              "days", "costs", "tasks", "risks", "emergency", "changes")}}


def read_json(path):
    def reject(value):
        raise ValueError(f"非法 JSON 数值 {value}")
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), parse_constant=reject)


def write_new(path, content, force=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w" if force else "x", encoding="utf-8", newline="\n") as f:
        f.write(content)


PRIVATE_PATTERN = re.compile(
    r"(?:1[3-9]\d{9}|\d{17}[\dXx]|(?:身份证|护照|订单号|票码|手机号|详细住址)\s*[:：]?\s*\S+)", re.I)


def public_text(value):
    """Keep the share outputs on an allowlist, with a second check on free text."""
    return PRIVATE_PATTERN.sub("[私人信息已略]", str(value))


def short(value, limit=36):
    value = public_text(value).strip()
    return value if len(value) <= limit else value[:limit - 1] + "…"


def card_time(value):
    return stamp(value).strftime("%m月%d日 %H:%M") if value else ""


def money(cents):
    return f'{Decimal(cents) / Decimal(100):.2f}'


def card_cost(plan):
    required = [cost_range(c) for c in plan["costs"] if not c["optional"]]
    if not required:
        return "待估算"
    people = Decimal(plan["trip"]["travelers"])
    low = sum((pair[0] for pair in required), Decimal(0)) / people
    high = sum((pair[1] for pair in required), Decimal(0)) / people
    suffix = "（已录入部分，费用未齐）" if not plan["budget"]["complete"] else ""
    return f'{low:,.2f}–{high:,.2f} {display_currency(plan["budget"]["currency"])}{suffix}'


def aa_result(aa):
    members = aa.get("members", [])
    order = [member["id"] for member in members]
    balances = {ident: 0 for ident in order}
    planned = []
    for expense in aa.get("expenses", []):
        cents = int(Decimal(expense["amount"]) * 100)
        participants = [ident for ident in order if ident in expense["participant_ids"]]
        each, remainder = divmod(cents, len(participants))
        shares = {ident: each + (index < remainder) for index, ident in enumerate(participants)}
        if expense["status"] == "planned":
            planned.append((expense["label"], cents, shares))
            continue
        balances[expense["payer_id"]] += cents
        for ident, share in shares.items():
            balances[ident] -= share

    @lru_cache(None)
    def settle(state):
        first = next((index for index, amount in enumerate(state) if amount), None)
        if first is None:
            return ()
        best = None
        for other in range(first + 1, len(state)):
            if state[first] * state[other] >= 0:
                continue
            amount = min(abs(state[first]), abs(state[other]))
            payer, receiver = (first, other) if state[first] < 0 else (other, first)
            updated = list(state)
            updated[payer] += amount
            updated[receiver] -= amount
            candidate = ((order[payer], order[receiver], amount),) + settle(tuple(updated))
            if best is None or (len(candidate), candidate) < (len(best), best):
                best = candidate
        return best or ()

    transfers = settle(tuple(balances.values()))
    return balances, planned, transfers


def card_content(plan, report):
    card = plan.get("card", {})
    trip = plan["trip"]
    start, end = trip["start_date"], trip["end_date"]
    days = (day(end) - day(start)).days + 1 if start and end and day(end) >= day(start) else None
    date_text = (f"{start}—{end}" if start and end else "日期待定") + (f" · {days} 天" if days else "")
    status = report["effective_status"]
    status_text = {"exploration": "草稿·还没定", "conditional": "条件性方案·待确认", "ready": "关键安排已落实"}[status]
    meeting = card.get("meeting") or {}
    meeting_text = " · ".join(x for x in (card_time(meeting.get("time")), meeting.get("place")) if x) or "集合时间与地点待定"
    reminders = [public_text(x) for x in report["blockers"]]
    if not reminders:
        reminders = [public_text(r["trigger"] + "：" + r["action"]) for r in plan["risks"] if not r["resolved"]]
    if not reminders:
        reminders = ["出发前再次核对交通、预约与天气。"]
    aa = card.get("aa") or {}
    balances, planned, transfers = aa_result(aa)
    names = {member["id"]: public_text(member["name"]) for member in aa.get("members", [])}
    return {"destination": public_text(trip["destination"] or "目的地待定"),
            "origin": public_text(trip["origin"] or "出发地待定"),
            "date": date_text, "people": trip["travelers"], "status": status_text,
            "demo": plan["is_demo"], "meeting": public_text(meeting_text),
            "cost": card_cost(plan), "days": [(d["date"], public_text(d["summary"])) for d in sorted(plan["days"], key=lambda d: d["date"])],
            "packing": [public_text(x) for x in card.get("packing", [])],
            "reminders": reminders, "roles": card.get("roles", []),
            "polls": card.get("polls", []), "names": names,
            "balances": balances, "planned": planned, "transfers": transfers,
            "aa_expenses": bool(aa.get("expenses")),
            "aa_paid": any(x["status"] == "paid" for x in aa.get("expenses", [])),
            "paid_expenses": [x for x in aa.get("expenses", []) if x["status"] == "paid"],
            "currency": display_currency(plan["budget"]["currency"]),
            "author": public_text(card.get("author") or "杰纶hhh")}


def card_text(info):
    lines = [f'🏕 {info["destination"]}搭子卡',
             f'{info["origin"]}出发｜{info["date"]}｜{info["people"]} 人',
             info["status"]]
    if info["demo"]:
        lines.append("演示数据 · 地点、价格与安排均为虚构")
    lines.extend((f'集合：{info["meeting"]}', f'人均必需费用：{info["cost"]}', "", "行程"))
    lines.extend(f'{date}  {summary}' for date, summary in info["days"])
    if not info["days"]:
        lines.append("行程待整理")
    lines.extend(("", "必带物品：" + ("、".join(info["packing"]) if info["packing"] else "待整理"),
                  "注意：" + "；".join(info["reminders"][:3])))
    if info["polls"]:
        lines.extend(("", "投票（在群里回复编号）"))
        for i, poll in enumerate(info["polls"], 1):
            lines.append(f'{chr(64 + i) if i <= 26 else i}. {public_text(poll["question"])}')
            lines.extend(f'{chr(64 + i) if i <= 26 else i}{n} {public_text(option)}' for n, option in enumerate(poll["options"], 1))
            if poll["deadline"]:
                lines.append(f'截止：{card_time(poll["deadline"])}')
    if info["roles"]:
        lines.extend(("", "分工接龙（认领后写上名字）"))
        lines.extend(f'{n}. {public_text(role["role"])} → {public_text(role["assignee"] or "待认领")}'
                     for n, role in enumerate(info["roles"], 1))
    if info["names"] and info["aa_expenses"]:
        lines.extend(("", "AA · 已付款结算"))
        for expense in info["paid_expenses"]:
            members = "、".join(info["names"][ident] for ident in expense["participant_ids"])
            payer = info["names"][expense["payer_id"]]
            lines.append(f'{public_text(expense["label"])} {expense["amount"]} {info["currency"]} · {payer}垫付 · {members}分摊')
        for ident, cents in info["balances"].items():
            if cents:
                lines.append(f'{info["names"][ident]} {"应收" if cents > 0 else "应付"} {money(abs(cents))} {info["currency"]}')
        if info["transfers"]:
            lines.append("建议转账：")
            lines.extend(f'{info["names"][payer]} → {info["names"][receiver]} {money(cents)} {info["currency"]}'
                         for payer, receiver, cents in info["transfers"])
        elif not any(info["balances"].values()):
            lines.append("已结清，无需转账" if info["aa_paid"] else "尚无已付款，无需转账")
        if info["planned"]:
            lines.append("计划支出（尚不结算）：")
            for label, cents, shares in info["planned"]:
                detail = "、".join(f'{info["names"][ident]} {money(share)}' for ident, share in shares.items())
                lines.append(f'{public_text(label)} {money(cents)} {info["currency"]} · {detail} 分摊')
    lines.extend(("", f'由 travel-planner 生成 · @{info["author"]}'))
    return "\n".join(lines) + "\n"


def render_card(plan, report):
    if report["schema_errors"]:
        raise ValueError("结构错误，拒绝生成搭子卡")
    info = card_content(plan, report)
    pages = []

    def heading(title):
        return f'<h2 class="section-title">{esc(title)}</h2>'

    def row(label, value, style="line-row"):
        return f'<div class="{style}"><b>{esc(label)}</b><span>{esc(short(value, 43))}</span></div>'

    first_days = info["days"][:3]
    overview = ('<div class="hero"><div class="eyebrow">TRAVEL COMPANION / 一起出发</div>'
                f'<h1>{esc(short(info["destination"], 14))}</h1>'
                f'<p class="subtitle">从 {esc(short(info["origin"], 30))} 出发，路上见。</p></div>'
                '<div class="summary">'
                f'<div class="stat"><small>旅行日期</small><strong>{esc(short(info["date"], 31))}</strong></div>'
                f'<div class="stat"><small>同行人数</small><strong>{info["people"]} 人</strong></div>'
                f'<div class="stat"><small>计划天数</small><strong>{len(info["days"])} 天日程</strong></div></div>'
                f'<div class="status"><span class="star">✦</span>{esc(info["status"])}</div>'
                f'<div class="meeting"><b>集合</b><span>{esc(short(info["meeting"], 45))}</span></div>'
                f'<div class="cost"><b>人均必需费用</b><strong>{esc(info["cost"])}</strong></div>'
                + heading("每天怎么走")
                + ("".join(row(d, s, "day-row") for d, s in first_days) if first_days else '<p class="tiny">行程待整理</p>')
                + heading("随身带上")
                + '<div class="chips">'
                + ("".join(f'<span class="chip">{esc(short(x, 15))}</span>' for x in info["packing"][:6])
                   if info["packing"] else '<span class="tiny">必带物品待整理</span>')
                + '</div>' + heading("出发前留意")
                + "".join(f'<p class="reminder">{esc(short(x, 55))}</p>' for x in info["reminders"][:3]))
    pages.append(overview)

    # Subsequent sheets have a fixed content budget. Keep every row; shorten only
    # its visual excerpt. The text edition carries full free-text content.
    groups = []
    if len(info["days"]) > 3:
        groups.append(("行程续篇", [(row(d, s, "day-row"), 1) for d, s in info["days"][3:]]))
    if info["polls"]:
        entries = []
        for i, poll in enumerate(info["polls"], 1):
            prefix = chr(64 + i) if i <= 26 else str(i)
            options = list(enumerate(poll["options"], 1))
            for offset in range(0, len(options), 5):
                subset = options[offset:offset + 5]
                title = f'{prefix}. {public_text(poll["question"])}'
                if offset:
                    title += "（续）"
                block = f'<div class="poll"><div class="poll-title">{esc(short(title, 39))}</div>'
                block += "".join(f'<div class="poll-option"><b>{prefix}{n}</b>　{esc(short(option, 48))}</div>' for n, option in subset)
                if poll["deadline"]:
                    block += f'<div class="poll-deadline">截止 {esc(card_time(poll["deadline"]))}</div>'
                entries.append((block + '</div>', 1 + len(subset)))
        groups.append(("投票 · 在群里回复编号", entries))
    if info["roles"]:
        groups.append(("分工 · 等你认领", [
            (row(role["role"], role["assignee"] or "待认领"), 1)
            for role in info["roles"]]))
    if info["names"] and info["aa_expenses"]:
        entries = []
        for expense in info["paid_expenses"]:
            payer = info["names"][expense["payer_id"]]
            entries.append((row("已付·" + public_text(expense["label"]), f'{expense["amount"]} {info["currency"]} · {payer}垫付'), 1))
        for ident, cents in info["balances"].items():
            if cents:
                entries.append((row(info["names"][ident], f'{"应收" if cents > 0 else "应付"} {money(abs(cents))} {info["currency"]}'), 1))
        for payer, receiver, cents in info["transfers"]:
            entries.append((f'<div class="line-row transfer"><span>{esc(short(info["names"][payer], 15))} → {esc(short(info["names"][receiver], 15))}　{money(cents)} {esc(info["currency"])}</span></div>', 1))
        if not info["transfers"] and not any(info["balances"].values()):
            note = "已结清，无需转账" if info["aa_paid"] else "尚无已付款，无需转账"
            entries.append((f'<div class="line-row"><span>{note}</span></div>', 1))
        for label, cents, _ in info["planned"]:
            entries.append((row("计划·未付款", f'{public_text(label)} {money(cents)} {info["currency"]}'), 1))
        groups.append(("AA · 已付款才结算", entries))

    secondary, used, active_group = [], 0, None
    for title, entries in groups:
        for block, units in entries:
            if used + units + (1 if active_group != title else 0) > 19 and secondary:
                pages.append('<div class="body-secondary">' + "".join(secondary) + '</div>')
                secondary, used, active_group = [], 0, None
            if active_group != title:
                secondary.append(heading(title))
                used += 1
                active_group = title
            secondary.append(block)
            used += units
    if secondary:
        pages.append('<div class="body-secondary">' + "".join(secondary) + '</div>')

    page_html = []
    total = len(pages)
    for number, body in enumerate(pages, 1):
        demo = '<div class="demo">演示数据 · 请勿用于真实出行</div>' if info["demo"] else ""
        page_html.append('<article class="sheet" id="page-' + str(number) + '">'
                         '<div class="topline"><span class="stamp">搭子卡</span><span>一起走，慢慢玩</span></div>'
                         + demo + body
                         + f'<div class="sheet-foot"><span>完整信息见群聊文字版</span><strong>由 travel-planner 生成 · @{esc(info["author"])}</strong><span>{number:02d} / {total:02d}</span></div>'
                         '</article>')
    template = (ROOT / "assets" / "dazi-card-template.html").read_text(encoding="utf-8")
    tokens = {"TITLE": esc(info["destination"]), "PAGES": "".join(page_html)}
    html_page = re.sub(r"@@(TITLE|PAGES)@@", lambda match: tokens[match[1]], template)
    return html_page, card_text(info), total


def available_browser():
    candidates = [shutil.which(name) for name in ("chrome", "chromium", "chromium-browser", "msedge", "google-chrome")]
    candidates.extend((r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                       r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                       "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                       "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"))
    return next((str(path) for path in candidates if path and Path(path).is_file()), None)


def png_size(path):
    with Path(path).open("rb") as stream:
        header = stream.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or len(header) < 24:
        raise ValueError("浏览器未生成有效 PNG")
    return struct.unpack(">II", header[16:24])


def screenshot_cards(browser, source, destinations):
    with tempfile.TemporaryDirectory(prefix="travel-card-browser-") as profile:
        with tempfile.TemporaryDirectory(prefix="travel-card-images-") as temp:
            shots = []
            for number, target in enumerate(destinations, 1):
                shot = Path(temp) / f"card-{number:02d}.png"
                command = [browser, "--headless", "--disable-gpu", "--no-first-run",
                           "--disable-background-networking", "--disable-sync", "--hide-scrollbars",
                           "--force-device-scale-factor=1", "--window-size=1080,1440",
                           f"--user-data-dir={profile}", f"--screenshot={shot}",
                           Path(source).resolve().as_uri() + f"#page-{number}"]
                done = subprocess.run(command, capture_output=True, timeout=40)
                if done.returncode or not shot.is_file():
                    detail = done.stderr.decode("utf-8", "replace")[-300:]
                    raise RuntimeError(f"浏览器出图失败（第 {number} 页）：{detail}")
                if png_size(shot) != (1080, 1440):
                    raise RuntimeError(f"浏览器图片尺寸异常：{png_size(shot)}；需要 1080×1440")
                shots.append(shot)
            for shot, target in zip(shots, destinations):
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                os.replace(shot, target)


def create_card(plan, report, output_dir, force=False):
    html_page, txt, count = render_card(plan, report)
    check = audit(html_page)
    if not check["passed"]:
        raise ValueError("搭子卡模板离线静态检查失败：" + "; ".join(check["issues"]))
    folder = Path(output_dir)
    source, text_file = folder / "dazi-card.html", folder / "dazi-card.txt"
    destination = short(plan["trip"]["destination"] or "目的地待定", 18)
    safe_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", destination).strip(" .") or "目的地待定"
    start = plan["trip"]["start_date"] or "日期待定"
    images = [folder / f"搭子卡-{safe_name}-{start}-{n:02d}.png" for n in range(1, count + 1)]
    targets = [source, text_file, *images]
    if not force:
        existing = next((path for path in targets if path.exists()), None)
        if existing:
            raise FileExistsError(17, "目标文件已存在", str(existing))
    write_new(source, html_page, force)
    write_new(text_file, txt, force)
    browser = available_browser()
    result = {"html": str(source.resolve()), "text": str(text_file.resolve()),
              "images": [], "expected_images": [str(path.resolve()) for path in images]}
    if not browser:
        result["image_error"] = "未找到本机 Chrome/Edge；请由当前 Agent 的浏览器工具自动截图后交付 PNG。"
        return result
    try:
        screenshot_cards(browser, source, images)
        result["images"] = [str(path.resolve()) for path in images]
        if force:
            prefix = f"搭子卡-{safe_name}-{start}-"
            retained = set(images)
            for old in folder.iterdir():
                if old.is_file() and old.name.startswith(prefix) and old.suffix == ".png" and old not in retained:
                    old.unlink()
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        result["image_error"] = str(exc)
    return result


def main():
    # CLI JSON has a stable encoding even when Windows redirects a GBK stream.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="cmd", required=True)
    for command in ("init", "check", "render", "audit", "card"):
        p = commands.add_parser(command)
        p.add_argument("path")
        if command in ("init", "render", "card"):
            p.add_argument("--force", action="store_true")
        if command in ("check", "render", "card"):
            p.add_argument("--as-of", help="检查日期 YYYY-MM-DD，默认运行机器本地日期")
        if command == "render":
            p.add_argument("--out", required=True)
        if command == "card":
            p.add_argument("-o", "--out", required=True, help="搭子卡输出目录")
    args = parser.parse_args()
    try:
        if args.cmd == "init":
            write_new(args.path, json.dumps(blank(), ensure_ascii=False, indent=2) + "\n", args.force)
            result, code = {"created": str(Path(args.path).resolve()), "status": "exploration"}, 0
        elif args.cmd == "audit":
            result = audit(Path(args.path).read_text(encoding="utf-8"))
            code = 0 if result["passed"] else 1
        else:
            plan = read_json(args.path)
            result = validate(plan, args.as_of)
            code = 2 if result["schema_errors"] else (1 if result["blockers"] else 0)
            if args.cmd == "render" and not result["schema_errors"]:
                output = render(plan, result)
                check = audit(output)
                if not check["passed"]:
                    raise ValueError("模板离线静态检查失败：" + "; ".join(check["issues"]))
                write_new(args.out, output, args.force)
                result["output"] = str(Path(args.out).resolve())
            if args.cmd == "card" and not result["schema_errors"]:
                result["card"] = create_card(plan, result, args.out, args.force)
                if result["card"].get("image_error"):
                    code = 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except FileExistsError as exc:
        message = f"目标文件已存在：{exc.filename}。请换一个文件名；确认需要覆盖时添加 --force。"
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (ValueError, OSError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
