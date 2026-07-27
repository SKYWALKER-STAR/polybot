# Polybot — Polymarket 自动交易机器人

面向 Polymarket 的自动化交易框架，支持 **BTC 5分钟涨跌**二元市场与**任意市场**（多选事件、单二元市场等），内置四套策略：

| 策略 | 文件 | 说明 |
|---|---|---|
| `btc_5min` | [strategy/btc_5min.py](strategy/btc_5min.py) | 临近结算时押注涨/跌方向 |
| `btc_arb` | [strategy/btc_arb.py](strategy/btc_arb.py) | BTC 5min YES+NO 价差无风险套利（Split/Merge） |
| `multi_arb` | [strategy/multi_arb.py](strategy/multi_arb.py) | **多选市场套利监听**（任意 Polymarket 多选事件） |
| `slug_arb` | [strategy/slug_arb.py](strategy/slug_arb.py) | **通用 Slug 套利**（任意市场，通过 slug 配置，含链上 split/merge） |

四套策略可**同时运行**，互不干扰；也可通过 `.env` 单独开启任意组合。

### 策略开关速查

```ini
# 仅运行方向性策略
BTC_5MIN_ENABLED=true
ARB_ENABLED=false
ELECTION_ARB_ENABLED=false
SLUG_ARB_ENABLED=false

# 仅运行 BTC 套利策略（先开观察模式验证）
BTC_5MIN_ENABLED=false
ARB_ENABLED=true
ARB_OBSERVE_MODE=true

# 同时监听多个多选事件市场（逗号分隔）
ELECTION_MARKET_SLUGS=democratic-presidential-nominee-2028,republican-presidential-nominee-2028
ELECTION_ARB_ENABLED=true
ELECTION_ARB_OBSERVE_MODE=true

# 通用 Slug 套利：指定任意市场 slug，含链上 split/merge（建议先开观察模式）
SLUG_ARB_ENABLED=true
SLUG_ARB_OBSERVE_MODE=true
SLUG_ARB_MARKET_SLUGS=will-btc-reach-100k-2025,eth-price-end-of-2026

# 同时运行全部四套策略
BTC_5MIN_ENABLED=true
ARB_ENABLED=true
ELECTION_ARB_ENABLED=true
SLUG_ARB_ENABLED=true
```

> 四项均为 `false` 时，bot 启动会报错并退出，防止空跑。

---

## 项目结构

```
polybot/
├── config/
│   └── settings.py               # 类型化配置，从 .env 文件加载
├── core/
│   ├── client.py                 # Polymarket CLOB API 封装（含 split/merge）
│   ├── event_market_resolver.py  # 多选事件 Resolver + DataService（新增）
│   ├── market_data.py            # 行情查询 + 快照持久化
│   ├── market_resolver.py        # 自动跟踪下一个 5min 市场
│   ├── order_book.py             # 通用订单簿获取与指标分析
│   ├── order_manager.py          # 下单、取消订单、风控检查
│   ├── position_tracker.py       # 持仓跟踪（止损/止盈使用）
│   └── ws_market_feed.py         # WebSocket 毫秒级行情订阅
├── strategy/
│   ├── base.py                   # 抽象策略基类（接口定义）
│   ├── btc_5min.py               # BTC 5分钟方向性策略
│   ├── btc_arb.py                # BTC YES/NO 价差无风险套利策略
│   ├── multi_arb.py              # 多选市场套利策略
│   └── slug_arb.py               # 通用 Slug 套利策略（任意市场，含链上 split/merge）
├── database/
│   ├── connection.py             # SQLAlchemy 引擎与会话工厂
│   └── models.py                 # ORM 模型：orders / market_snapshots / audit_logs
├── audit/
│   └── logger.py                 # 操作审计，全量写入 PostgreSQL
├── bot.py                        # 程序入口与主循环
├── requirements.txt
└── .env.example
```

---

## 快速开始

### 1. 环境要求

- Python 3.11+
- PostgreSQL 14+
- 已充值的 Polymarket 账号及对应钱包私钥

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，至少填写以下必填项：
#   PRIVATE_KEY           — 钱包私钥（hex）
#   BTC_5MIN_START_TIMESTAMP — 初始市场的 Unix 时间戳
#   DATABASE_URL          — PostgreSQL DSN
# 可选（仅当你需要 relayer/gasless 认证时）：
#   RELAYER_API_KEY
#   RELAYER_API_KEY_ADDRESS  — 与 RELAYER_API_KEY 绑定的钱包地址
```

### 4. 创建数据库

```sql
CREATE DATABASE polybot;
CREATE USER polybot WITH PASSWORD 'secret';
GRANT ALL PRIVILEGES ON DATABASE polybot TO polybot;
```

机器人首次启动时会自动调用 `init_db()` 创建所有数据表。

### 5. 以干跑模式运行（安全默认）

```bash
python bot.py
```

`DRY_RUN=true`（默认开启）时，策略正常运行、订单记录写入数据库，但**不会向交易所发送任何真实请求**。

### 6. 开启真实交易

仅在充分验证策略行为后再切换。

```bash
# 在 .env 中修改：
DRY_RUN=false
python bot.py
```

---

## 策略一：BTC 5分钟方向性策略（btc_5min）

**原理**：在市场结算前 N 秒，当 UP 或 DOWN 的 best_ask 落入目标价格区间时，押注该方向。

### 核心参数（`.env`）

```ini
# 押注金额（USDC）
STRATEGY_FOK_BET_USDC=5.0       # FOK 市价单，立即全成交或取消
STRATEGY_FAK_BET_USDC=0.0       # FAK 单（尽量成交，剩余取消）
STRATEGY_GTC_BET_USDC=5.0       # GTC 限价单
STRATEGY_HEDGE_BET_USDC=0.0     # 对冲反方向金额（0 = 关闭对冲）

# 触发条件
STRATEGY_ENTRY_SECONDS=60       # 距结算多少秒内开始检查
STRATEGY_TARGET_PRICE=0.80      # 目标入场价格（0~1）
STRATEGY_PRICE_TOLERANCE=0.03   # 价格容忍带（±3%）

# 止损 / 止盈
STRATEGY_STOP_LOSS_PCT=20.0     # 亏损 20% 触发止损（0 = 关闭）
STRATEGY_TAKE_PROFIT_PCT=50.0   # 盈利 50% 触发止盈（0 = 关闭）
```

---

## 策略二：YES/NO 价差无风险套利（btc_arb）

**原理**：Polymarket 二元市场中，任意数量的 YES + NO token 总可以合并（merge）成等额 pUSD，也可以将 pUSD 拆分（split）成等量 YES + NO。理论上：

```
YES_ask + NO_ask = $1.00（无套利均衡）
```

当市场情绪踩踏或流动性不平衡时，这个等式会偏离，产生**纯无风险价差**：

| 场景 | 条件 | 操作 | 利润来源 |
|---|---|---|---|
| **Merge 套利** | `YES_ask + NO_ask < $1` | 同时买入双边 → merge → 得到 $1 pUSD | `$1 - (YES_ask + NO_ask)` |
| **Split 套利** | `YES_bid + NO_bid > $1` | split $1 pUSD → 同时卖出双边 | `(YES_bid + NO_bid) - $1` |

两种模式均与 BTC 实际涨跌结果**完全无关**。

### 执行流程（毫秒级）

```
Merge 套利：
  asyncio.gather(
      FOK BUY YES @ask,    ← 两笔订单并发提交
      FOK BUY NO  @ask,
  )
  → merge_positions(condition_id, amount)  ← 链上合并

Split 套利：
  split_position(condition_id, amount)     ← 链上拆分
  asyncio.gather(
      FOK SELL YES @bid,   ← 两笔订单并发提交
      FOK SELL NO  @bid,
  )
```

### 启用套利策略（`.env`）

```ini
# 启用套利（默认关闭，与 btc_5min 可同时开启）
ARB_ENABLED=true

# 触发阈值（扣除 gas 后仍有利润才触发）
ARB_MIN_MERGE_SPREAD=0.008   # YES_ask + NO_ask ≤ 0.992 时触发
ARB_MIN_SPLIT_SPREAD=0.008   # YES_bid + NO_bid ≥ 1.008 时触发

# 资金规模
ARB_BASE_TRADE_USDC=50.0     # 单次套利基础规模
ARB_MAX_TRADE_USDC=100.0     # 单次套利上限

# 风控
ARB_COOLDOWN_SECONDS=3.0     # 两次套利之间的冷却期（等待链上确认）
ARB_LIQUIDITY_MIN_SIZE=10.0  # 订单簿最小深度（shares），低于此跳过
ARB_SLIPPAGE_TOLERANCE=0.002 # FOK 单价格容忍滑点（0.2 分）
ARB_ESTIMATED_GAS_USDC=0.005 # Polygon gas 估算，用于净利润过滤
```

### 单边未成交风险

套利使用 FOK 订单确保"全成交或取消"，但在极端情况下（双边 FOK 中仅一边成交），机器人会：

1. 记录 `ERROR` 级日志，标明残留方向
2. **不自动平仓**（避免在价格不利时损失扩大）
3. 建议人工检查并通过 Polymarket 界面手动处理

为降低此风险，建议将 `ARB_LIQUIDITY_MIN_SIZE` 设置为套利规模的 **2 倍以上**。

---

## 策略三：多选市场套利监听（multi_arb）

**原理**：Polymarket 多选事件（如大选、颁奖礼）中，每个候选结果对应一个独立的 **YES/NO 二元子市场**。与 BTC 5min 市场完全相同，每个子市场都支持 YES+NO merge 成 $1 的操作，因此可对每个结果独立检测 Merge / Split 套利机会：

| 场景 | 条件 | 操作 |
|---|---|---|
| **Merge 套利** | `YES_ask + NO_ask < $1` | 同时买入双边 → merge |
| **Split 套利** | `YES_bid + NO_bid > $1` | split $1 → 同时卖出双边 |

此外，策略还会在每 tick 记录**所有候选结果的 YES ask 价格之和**，方便观察跨结果定价偏差。

### 切换 / 增加监听的市场

`.env` 中用逗号分隔多个 slug，每个市场独立运行一套策略实例：

```ini
# slug = 事件 URL 最后一段路径，例如：
# https://polymarket.com/event/democratic-presidential-nominee-2028
#                              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

# 单个市场
ELECTION_MARKET_SLUGS=democratic-presidential-nominee-2028

# 同时监听多个市场（逗号分隔）
# ELECTION_MARKET_SLUGS=democratic-presidential-nominee-2028,republican-presidential-nominee-2028
# ELECTION_MARKET_SLUGS=democratic-presidential-nominee-2028,oscar-best-picture-2027,2026-fifa-world-cup-winner
```

机器人启动时会自动通过 Gamma API 解析每个事件下的所有候选结果及其 token，无需手动填写 token ID。

### 配置参数（`.env`）

```ini
ELECTION_ARB_ENABLED=true
ELECTION_ARB_OBSERVE_MODE=true      # true = 仅打印，不下单（建议首次启用时保持）

ELECTION_ARB_MIN_MERGE_SPREAD=0.005 # YES_ask + NO_ask ≤ 0.995 时触发
ELECTION_ARB_MIN_SPLIT_SPREAD=0.005 # YES_bid + NO_bid ≥ 1.005 时触发

ELECTION_ARB_BASE_TRADE_USDC=20.0   # 单次套利基础规模
ELECTION_ARB_MAX_TRADE_USDC=100.0   # 单次套利上限
ELECTION_ARB_COOLDOWN_SECONDS=5.0   # 两次套利之间的冷却期
ELECTION_ARB_LIQUIDITY_MIN_SIZE=5.0 # 订单簿最小深度（shares）
```

### 日志过滤

所有多选市场日志均以 `[multi_arb:{slug}][候选人名]` 为前缀，可精确过滤：

```bash
# 查看所有多选市场套利机会
grep "multi_arb:" logs/polybot.log

# 查看特定市场的行情日志
grep "multi_arb:democratic-presidential-nominee-2028" logs/polybot.log

# 仅看发现套利机会的行
grep "★ 套利机会 ★" logs/polybot.log
```

---

## 策略四：通用 Slug 套利（slug_arb）

**原理**：与 `btc_arb` / `multi_arb` 相同，基于 YES+NO merge/split 的无风险价差套利。区别在于：

| | btc_arb | multi_arb | **slug_arb** |
|---|---|---|---|
| 目标市场 | 仅 BTC 5min | 仅多选事件 | **任意市场（slug 配置）** |
| 市场数量 | 1 | 多个候选结果 | **一个或多个 slug** |
| 链上 merge | ✅ | ❌ | ✅ |
| 链上 split | ✅ | ❌ | ✅ |
| 市场发现 | 时间戳推导 | Gamma API | **Gamma API（按 slug）** |

`slug_arb` 是最通用的版本——只需在 `.env` 中填入任意 Polymarket 市场/事件的 slug，即可自动解析其下所有子市场（含多选事件的多个候选结果），并对每对 YES/NO token 独立检测并执行套利。

### 如何获取 slug

slug 即 Polymarket 市场或事件 URL 的最后一段路径：

```
https://polymarket.com/event/will-btc-reach-100k-by-end-of-2025
                             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                             slug = will-btc-reach-100k-by-end-of-2025

https://polymarket.com/event/democratic-presidential-nominee-2028
                             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                             slug = democratic-presidential-nominee-2028
                             （多选事件：自动解析所有候选结果）
```

### 配置参数（`.env`）

```ini
# ---- 基本开关 -------------------------------------------------------
# 启用策略
SLUG_ARB_ENABLED=true

# 观察模式（强烈建议首次启用时保持 true）
# true  = 每 tick INFO 打印实时订单簿市价；发现机会时打印完整快照；不下单不链上操作
# false = 执行真实的 FOK 下单 + 链上 merge_positions / split_position
SLUG_ARB_OBSERVE_MODE=true

# 目标市场 slug 列表，逗号分隔，支持同时监控多个市场
# 支持二元市场（单 YES/NO 对）和多选事件（自动解析多个候选结果子市场）
SLUG_ARB_MARKET_SLUGS=will-btc-reach-100k-2025,eth-price-end-of-2026

# ---- 触发阈值 -------------------------------------------------------
SLUG_ARB_MIN_MERGE_SPREAD=0.008   # YES_ask + NO_ask ≤ 0.992 时触发 Merge 套利
SLUG_ARB_MIN_SPLIT_SPREAD=0.008   # YES_bid + NO_bid ≥ 1.008 时触发 Split 套利

# ---- 资金规模 -------------------------------------------------------
SLUG_ARB_BASE_TRADE_USDC=50.0     # 单次套利基础规模（USDC）
SLUG_ARB_MAX_TRADE_USDC=100.0     # 单次套利上限（USDC）

# ---- 风控 -----------------------------------------------------------
SLUG_ARB_COOLDOWN_SECONDS=3.0     # 两次套利之间的冷却期（等待链上确认）
SLUG_ARB_LIQUIDITY_MIN_SIZE=10.0  # 订单簿最小深度（shares），低于此跳过
SLUG_ARB_SLIPPAGE_TOLERANCE=0.002 # FOK 单价格容忍滑点（0.2 分）
SLUG_ARB_ESTIMATED_GAS_USDC=0.005 # Polygon gas 估算（USDC），用于净利润过滤
```

### 推荐上线流程

```
第一步：观察模式运行 1～2 天，确认日志中出现合理的套利机会
  SLUG_ARB_ENABLED=true
  SLUG_ARB_OBSERVE_MODE=true
  SLUG_ARB_MARKET_SLUGS=<你的目标 slug>

第二步：调整阈值（通过日志中 merge_sum / split_sum 偏离数据）
  SLUG_ARB_MIN_MERGE_SPREAD=0.005   ← 可适当降低以捕获更多机会
  SLUG_ARB_MIN_SPLIT_SPREAD=0.005

第三步：确认无误后关闭观察模式，并先以小规模测试
  SLUG_ARB_OBSERVE_MODE=false
  SLUG_ARB_BASE_TRADE_USDC=5.0
  DRY_RUN=false
```

### 执行流程（毫秒级）

```
Merge 套利：
  asyncio.gather(
      FOK BUY YES @ask,    ← 两笔订单并发提交
      FOK BUY NO  @ask,
  )
  → merge_positions(condition_id, amount)  ← 链上合并

Split 套利：
  split_position(condition_id, amount)     ← 链上拆分
  asyncio.gather(
      FOK SELL YES @bid,   ← 两笔订单并发提交
      FOK SELL NO  @bid,
  )
```

### 日志过滤

所有日志均以 `[slug_arb:{slug}][{市场名}]` 为前缀：

```bash
# 查看所有 slug_arb 套利机会
grep "slug_arb:" logs/polybot.log

# 查看特定市场的实时价格日志
grep "slug_arb:will-btc-reach-100k-2025" logs/polybot.log

# 仅看发现套利机会的行（跨所有策略）
grep "★ 套利机会 ★" logs/polybot.log

# 查看套利执行结果（成功 / 失败）
grep -E "✅|MERGE 风险|SPLIT 卖单未" logs/polybot.log
```

---

## 数据库表说明

## 通用订单簿分析

`core/order_book.py` 现在提供了可复用的 `OrderBookService`。它复用现有 `MarketDataService` 的 HTTP / WebSocket 取数能力，对任意 token 统一输出：

- 买卖价差 `bid_ask_spread`
- 买盘深度 `bid_depth`
- 卖盘深度 `ask_depth`
- 买卖深度比 `depth_ratio`
- 买入 / 卖出滑点 `buy_slippage` / `sell_slippage`

### 用法示例：分析当前 BTC 5min 市场

```python
from core.client import PolymarketClient
from core.market_data import MarketDataService
from core.market_resolver import MarketResolver
from core.order_book import OrderBookService

client = PolymarketClient()
await client.connect()

resolver = MarketResolver(initial_timestamp=1780073700)
market_data_service = MarketDataService(client=client, resolver=resolver)
order_book_service = OrderBookService(market_data_service)

market = await resolver.get_active_market()
report = await order_book_service.analyze_token(
  market.up_token_id,
  outcome="UP",
  condition_id=market.condition_id,
  market_slug=market.slug,
  market_end_time=market.end_time,
  gamma_price=market.up_price,
  slippage_notional=50.0,
)

print(report.metrics.bid_ask_spread)
print(report.metrics.bid_depth.notional)
print(report.metrics.ask_depth.notional)
print(report.metrics.depth_ratio)
print(report.metrics.buy_slippage.slippage)
```

### 用法示例：灵活选择多选事件中的目标市场

```python
from core.client import PolymarketClient
from core.event_market_resolver import EventMarketResolver
from core.market_data import MarketDataService
from core.market_resolver import MarketResolver
from core.order_book import OrderBookService

client = PolymarketClient()
await client.connect()

market_data_service = MarketDataService(
  client=client,
  resolver=MarketResolver(initial_timestamp=1780073700),
)
order_book_service = OrderBookService(market_data_service)

event_resolver = EventMarketResolver("democratic-presidential-nominee-2028")
event_info = await event_resolver.get_market_info()
target_market = next(m for m in event_info.markets if m.title == "Kamala Harris")

report = await order_book_service.analyze_token(
  target_market.yes_token_id,
  outcome="YES",
  condition_id=target_market.condition_id,
  market_slug=event_info.event_slug,
  gamma_price=target_market.yes_price,
  slippage_notional=25.0,
  prefer_ws=False,
)

print(report.market_data.best_bid, report.market_data.best_ask)
print(report.metrics.sell_slippage.slippage)
```

如果你已经拿到 token_id，也可以直接构造 `OrderBookTarget`，再用 `analyze_target()` / `analyze_targets()` 批量分析多个目标市场。

| 表名 | 用途 |
|---|---|
| `orders` | 机器人尝试发出的每一笔订单（含套利单） |
| `market_snapshots` | 历史委托簿快照（用于分析与回测） |
| `audit_logs` | 所有机器人操作的不可变审计记录 |

---

## 安全机制

| 机制 | 说明 |
|---|---|
| `DRY_RUN=true` | 默认开启，跳过所有真实交易所调用 |
| `MAX_ORDER_SIZE_USDC` | 单笔订单名义价值上限 |
| `MAX_POSITION_SIZE_USDC` | 单方向总持仓上限 |
| `ARB_COOLDOWN_SECONDS` | BTC 套利链上 tx 冷却，防止 nonce 冲突 |
| `ARB_LIQUIDITY_MIN_SIZE` | BTC 套利流动性门槛，薄市场不进场 |
| `ELECTION_ARB_OBSERVE_MODE=true` | 多选市场仅打印套利机会，不执行任何交易（默认开启） |
| `ELECTION_ARB_LIQUIDITY_MIN_SIZE` | 多选市场流动性门槛，低于此跳过 |
| `SLUG_ARB_OBSERVE_MODE=true` | 通用 Slug 套利仅打印，不下单不链上操作（默认开启） |
| `SLUG_ARB_LIQUIDITY_MIN_SIZE` | Slug 套利流动性门槛，低于此跳过 |
| `SLUG_ARB_COOLDOWN_SECONDS` | Slug 套利链上 tx 冷却，防止 nonce 冲突 |
| 审计追踪 | 每个操作（含失败、干跑跳过）均写入 PostgreSQL |
| 优雅关闭 | 收到 SIGINT / SIGTERM 后干净停止，打印各策略统计 |

---

## 环境变量完整参考

完整列表及说明见 [.env.example](.env.example)。
