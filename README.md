# 旅行规划 Skill

从一句模糊的出行想法开始，逐步了解旅行偏好，比较目的地，整理可核实的行程，并导出可离线阅读的手机旅行手帐。

![模拟旅行手帐预览](docs/preview.png)

> 截图和 `assets/demo-plan.json` 使用虚构地点、价格与订单，仅用于展示效果，不能作为真实出行依据。

## 安装

将本仓库克隆到 Codex 的 Skill 目录：

```powershell
# Windows PowerShell
git clone https://github.com/xiaogongonline/travel-planner.git "$HOME\.codex\skills\travel-planner"
```

```bash
# macOS / Linux
git clone https://github.com/xiaogongonline/travel-planner.git "$HOME/.codex/skills/travel-planner"
```

如果该目录已经存在，先自行备份或选用别的目录；`git clone` 不会覆盖已有文件。安装后打开新的 Codex 任务，让 Skill 列表刷新。

## 怎么用

不必先填完整表格。可以从一句话开始：

```text
请使用 $travel-planner。国庆想出去玩，但还没想好去哪。
```

也可以说明已知条件：

```text
请使用 $travel-planner。我从广州出发，国庆有四天，两个人，总预算 3000 元，不想早起，还没决定去哪。
```

已有行程时，可以只改一部分或做出发前检查：

```text
使用 $travel-planner，把已有行程的酒店换到这个地址，检查哪些交通、预算和预约需要调整。
```

Skill 会根据当前阶段工作：偏好和目的地尚未明确时先交流、比较；关键选择清楚后再排详细日程。需要查询营业、交通、价格或预约规则时，它会使用当前环境实际可用的搜索、地图或浏览器工具，保留来源和适用条件。查不到的事项会留在待核实清单中。

## 导出手机攻略

方向和关键安排明确后，可要求“导出手机离线攻略”。生成的 HTML 包含日程、地址、预算、预约状态和可勾选待办。核心内容直接写在文件里；待办勾选进度只尝试保存在打开它的浏览器中。

导出工具使用 Python 3 标准库，无须安装额外 Python 包。通常由 Codex 维护计划数据并调用脚本；也可以手动运行：

```text
python scripts/travel_plan.py init <work>/plan.json
python scripts/travel_plan.py check <work>/plan.json
python scripts/travel_plan.py render <work>/plan.json --out <work>/travel-plan.html
python scripts/travel_plan.py audit <work>/travel-plan.html
```

`init` 只生成待填骨架，不是旅行方案。未落实关键预约等事项时，`render` 仍可生成醒目标记的条件性草案，不能据此认定行程已经可出发。

## 能力边界

- 实时事实取决于使用时可用的查询工具；Skill 本身不提供地图 API、票价库存或持续监控。
- 它不代购、付款或预约。营业时间、交通和订单在出发前仍需按最新情况复核。
- HTML 的核心内容可离线读，但来源链接和实时导航需要联网。手机本地文件的打开方式因设备而异，建议出发前在自己的手机上试开。

详细规则见 [SKILL.md](SKILL.md)。脚本检查可运行 `python scripts/test_travel_plan.py`。

## 许可

MIT License，见 [LICENSE](LICENSE)。
