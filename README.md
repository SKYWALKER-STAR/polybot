# Polybot — Polymarket 自动交易机器人

面向 Polymarket 的自动化交易框架，主要针对 **BTC 5分钟涨跌** 二元预测市场。

## 项目结构

```
polybot/
├── config/
│   └── settings.py        # 类型化配置，从 .env 文件加载
├── core/
│   ├── client.py          # Polymarket CLOB API 封装（认证、签名）
│   ├── market_data.py     # 行情查询 + 快照持久化
│   └── order_manager.py   # 下单、取消订单、风控检查
├── strategy/
│   ├── base.py            # 抽象策略基类（接口定义）
│   └── btc_5min.py        # BTC 5分钟策略占位实现 ← 主要编写入口
├── database/
│   ├── connection.py      # SQLAlchemy 引擎与会话工厂
│   └── models.py          # ORM 模型：orders / trades / market_snapshots / audit_logs
├── audit/
│   └── logger.py          # 操作审计，全量写入 PostgreSQL
├── bot.py                 # 程序入口与主循环，策略工厂在此切换
├── requirements.txt
└── .env.example
```

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
# 编辑 .env，填写 PRIVATE_KEY、BTC_5MIN_* 市场 ID、DATABASE_URL
```

### 4. 创建数据库

```sql
CREATE DATABASE polybot;
CREATE USER polybot WITH PASSWORD 'secret';
GRANT ALL PRIVILEGES ON DATABASE polybot TO polybot;
```

机器人在首次启动时会自动调用 `init_db()` 创建所有数据表。

### 5. 以干跑模式运行（安全默认）

```bash
python bot.py
```

`DRY_RUN=true`（默认开启）时，策略正常运行、订单记录写入数据库，但**不会向交易所发送任何真实请求**。

### 6. 开启真实交易

仅在充分验证策略行为正确后再切换。

```bash
# 在 .env 中修改：
DRY_RUN=false

python bot.py
```

---

## 实现交易策略

打开 [strategy/btc_5min.py](strategy/btc_5min.py)，在 `_generate_signal()` 中填写信号逻辑：

```python
def _generate_signal(self, yes_data: MarketData, no_data: MarketData) -> Signal:
    # 在此填写你的信号逻辑
    # 返回 Signal.BUY_YES、Signal.BUY_NO 或 Signal.NONE
    ...
```

`yes_data` 和 `no_data` 是 [`MarketData`](core/market_data.py) 数据类，包含最优买卖价、中间价、最新成交价以及完整的委托簿数据。

价格阈值和下单大小可通过同文件的 `StrategyConfig` 调整。

若需切换为完全不同的策略，继承 `BaseStrategy`、实现 `on_tick()`，然后修改 [bot.py](bot.py) 末尾的 `_build_strategy()` 工厂函数即可。

---

## 数据库表说明

| 表名 | 用途 |
|---|---|
| `orders` | 机器人尝试发出的每一笔订单 |
| `trades` | 每次成交事件的明细 |
| `market_snapshots` | 历史委托簿快照（用于分析与回测） |
| `audit_logs` | 所有机器人操作的不可变审计记录 |

---

## 安全机制

| 机制 | 说明 |
|---|---|
| `DRY_RUN=true` | 默认开启，跳过所有真实交易所调用 |
| `MAX_ORDER_SIZE_USDC` | 单笔订单名义价值上限 |
| `MAX_POSITION_SIZE_USDC` | 单方向总持仓上限 |
| 审计追踪 | 每个操作（含失败、干跑跳过）均写入 PostgreSQL |
| 优雅关闭 | 收到 SIGINT / SIGTERM 后干净停止 |

---

## 环境变量参考

完整列表及说明见 [.env.example](.env.example)。
