# 行程数据契约 v1

机器契约是 scripts/travel_plan.py 中的 SCHEMA，完整实例为 [demo-plan.json](../assets/demo-plan.json)。所有字段必填；无记录用空数组，允许未知的日期/引用/预算上限用 null。未知字段会被拒绝，防止悄悄丢失信息或夹带完整订单个人资料。金额、数量、分钟数限制在 0 至 10 亿；拒绝 NaN/Infinity 及明显超出旅行场景的数值。

init 生成顶层骨架。先采集与决策，再填写数据；不能为了通过校验给未知字段造值。探索阶段不必录入所有候选的完整数据。

## 顶层

| 字段 | 含义 |
| --- | --- |
| schema_version | 固定 1 |
| title/revision/generated_at | 标题、版本字符串、带时区的生成时间 |
| is_demo | 模拟数据必须 true；页面持续显示演示标识 |
| status | exploration / conditional / ready；脚本只能降级，不能替人确认 |
| trip | origin/destination/start_date/end_date/timezone/travelers/style/lodging |
| budget | currency/limit/hard_limit/scope/basis/complete |
| constraints | id/text/kind(hard或preference)/status(satisfied、violated、unknown)/evidence |
| assumptions/changes | 字符串数组，保存假设及本版变化 |
| sources/places/bookings/days/costs/tasks/risks/emergency | 见下文 |

ID 全局唯一，字母开头，余下只用字母、数字、下划线和连字符。日期为 YYYY-MM-DD。具体时刻如 2026-10-02T09:00:00+08:00；不能用无日期的 09:00 或无时区时间。跨时区活动起终点各带当地 offset，脚本以实际时间比较。timezone 作为阅读说明，不代替事件 offset。

单份数据是一组同行者的顺序行程。分组并行活动应保存独立子行程并交代集合，不把重叠当成顺序活动。跨午夜活动放入开始日；booking.date 指该项开始日，酒店多晚条件写 notes。

## sources

字段：id/name/kind/url/checked_at/valid_from/valid_to/applies_to/summary/recheck_on/conflict。

kind 为 official/provider/secondary/user/estimate/demo；后三类 url 可空。外部来源只能使用 http(s) URL。checked_at 含时区，valid_from/valid_to/recheck_on 为日期或 null。applies_to 写适用人数、日期、票种/房型及其他限制；summary 是证据摘要；冲突未解决就写 conflict，不能写 verified。

来源适用日期覆盖引用它的活动。费用按 cost.dates 中的实际消费日期检查，单晚报价不需要覆盖整段旅行，也不能扩大其真实适用期。不要把查询日期填成有效期。适用期未知不能靠 null 宣称未来确定；必要时设置 recheck_on 和关键待办。

## places 与 facts

place 字段：id/name/address/search_terms/notes/facts。酒店未定可用区域并说明尚未选店；地址未知留空。距离、交通方式、换乘、出入口等写 transport 活动 description，并附 source_ids 或说明估算。

fact 字段：label/value/kind/basis/source_ids/critical。kind 为 verified/user/estimate/assumption/unknown。verified 必须有来源；estimate、user、assumption 写依据；critical unknown/assumption 阻止 ready。事实文字保留开放日、最后入场、票种条件，不都简化为一个时间范围。

## bookings 与 tasks

booking 字段：id/title/status/critical/date/confirmed_evidence/source_ids/notes。

status 为 not_required/not_open/pending/confirmed/unavailable/unknown。confirmed 必须有日期及用户确认摘要或可见确认依据。critical 未完成会降级。订单时间、房型等放 notes，不写身份证、票码、支付信息。

task 字段：id/action/deadline/channel/impact/status/critical。deadline 为带时区时间或 null；status pending/done。预约窗口未公布时 deadline=null，在 channel/impact 说明下一次查询时机，不编期限。

status 是 HTML 待办的初始值。页面勾选仅记录浏览器本地个人进度，不自动回写数据、确认订单或改变核验结论；保存与内容版本隔离规则见 delivery.md。

## days 与活动

day 字段：date/summary/intensity/intensity_basis/items/alternatives。intensity 为轻松/适中/偏累/待评估；basis 解释步行、爬升、连续活动、换乘、作息。自由日 items 可空，但写明原因。

item 字段：id/title/kind/start/end/place_id/description/minimum_minutes/buffer_minutes/booking_id/source_ids/windows/notes。

- kind 为 visit/transport/meal/rest/lodging/other。
- place_id/booking_id 可 null，source_ids 可空。关键依赖须用 booking 或 task 显式录入，不通过省略关系避开检查。
- minimum_minutes + buffer_minutes 必须能放进 start/end 时间槽，均非负。
- windows 是含 start/end 的对象数组，可用游览/活动窗口。活动完整落在其中一个窗口。未知则 []，并用关键事实/待办标未知，不能把 [] 当全天开放。
- 交通和缓冲作为 transport 项单列，避免同时写成与游览重叠的时间段。

alternative 字段：trigger/action/dependencies/status/source_ids。status checked/conditional；dependencies 说明替代是否预约、开放、可到达、费用变化；未经核实保留 conditional。

## 费用

cost 字段：id/category/label/unit_low/unit_high/quantity/unit/paid/optional/kind/basis/dates/source_ids。dates 为实际消费/报价适用日期数组（YYYY-MM-DD）；引用来源时必须提供日期。来源需要覆盖这些日期；不同夜晚价格不同时分别建费用行。

全部按 budget.currency 计价；quantity 是实际计费数量（房晚、人次、车次等），脚本不再次乘 travelers。paid 是该行总已付金额，不是单价。kind 为 confirmed/quote/estimate。confirmed 是定价，上下端相同；部分付款按已付总额扣减待支付。总预算含已付；scope=remaining 比较未付。optional 单独显示，不掩盖必需费用超限。备用金作为必需费用行。

budget.complete 只有费用范围、人数、数量、备用金等人工核对完成后才 true；未 complete 不能 ready。硬上限比较必需费用区间上端，软预算超出显示提示。基于同一消费场景形成区间；不同方案单独存文件。

## 风险与紧急信息

risk 字段：trigger/action/critical/resolved；关键未解决风险阻断 ready。

emergency 字段：label/contact/basis/source_ids；只保存相关、核实的号码，不自动填某国通用号码。

## 校验边界

脚本检查类型、引用、日期、窗口、重叠、时间槽、显式硬约束、关键依赖、来源适用期和预算。它不理解“太累”、不查询真实票价、不会发现未录入的闭馆日。check 不是独立事实核验。必须按 planning/evidence/delivery 完成人工语义审查。
