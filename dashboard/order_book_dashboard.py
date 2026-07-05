from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Header, Footer, Static
from textual.widgets import Header, Footer, Static, DataTable
from rich.table import Table
from rich.panel import Panel

from core.order_book import OrderBookMetrics

def depth_bar(value, max_value=100000, width=25):
    filled = int((value / max_value) * width)
    return "█" * filled + " " * (width - filled) 

# =========================
# Widgets
# =========================
class MarketPanel(Static):
    def __init__(self, token_name, **kwargs):
        super().__init__(**kwargs)
        self.token_name = token_name

    def on_mount(self):
        self.border_title=self.token_name
       
    def update_metrics(self, metrics):
        spread = getattr(metrics, "bid_ask_spread", None)
        depth_ratio = getattr(metrics, "depth_ratio", None)

        spread_text = f"{spread:.4f}" if spread is not None else "N/A"
        ratio_text = f"{depth_ratio:.4f}" if depth_ratio is not None else "N/A"

        self.update(
            f"""
[bold yellow]Spread:[/] {spread_text}
[bold cyan]Depth Ratio:[/] {ratio_text}
    """
        )
class ArbPanel(Static):
    def __init__(self, token_name, **kwargs):
        super().__init__(**kwargs)
        self.token_name = token_name

    def on_mount(self):
        self.border_title=self.token_name
       
    def update_metrics(self, metrics_up, metrics_down):
        # asks 列表降序存储（最低卖价在末尾），取 [-1] 为「卖1」
        # 需同时检查对象不为 None 且列表非 None / 非空，否则 [-1] 会 IndexError
        best_up_ask   = metrics_up.asks[-1].price   if (metrics_up   is not None and metrics_up.asks)   else None
        best_down_ask = metrics_down.asks[-1].price if (metrics_down is not None and metrics_down.asks) else None

        # bids 列表升序存储（最高买价在末尾），取 [-1] 为「买1」
        best_up_bid   = metrics_up.bids[-1].price   if (metrics_up   is not None and metrics_up.bids)   else None
        best_down_bid = metrics_down.bids[-1].price if (metrics_down is not None and metrics_down.bids) else None

        # 卖1+卖1（ask+ask）= Merge 套利成本；< 1.0 时存在 Merge 机会
        ask_sum = (best_up_ask + best_down_ask) if (best_up_ask is not None and best_down_ask is not None) else None
        # 买1+买1（bid+bid）= Split 套利收益；> 1.0 时存在 Split 机会
        bid_sum = (best_up_bid + best_down_bid) if (best_up_bid is not None and best_down_bid is not None) else None

        ask_sum_text = f"{ask_sum:.4f}" if ask_sum is not None else "N/A"
        bid_sum_text = f"{bid_sum:.4f}" if bid_sum is not None else "N/A"

        ask_hint = (f"  [bold green]← Merge 机会 (偏离 {1.0 - ask_sum:.4f})[/]"
                    if ask_sum is not None and ask_sum < 1.0 else "")
        bid_hint = (f"  [bold green]← Split 机会 (偏离 {bid_sum - 1.0:.4f})[/]"
                    if bid_sum is not None and bid_sum > 1.0 else "")

        self.update(
            f"""
[bold yellow]卖1+卖1 (ask+ask):[/] {ask_sum_text}{ask_hint}
[bold cyan]买1+买1 (bid+bid):[/] {bid_sum_text}{bid_hint}
    """
        )

class DepthPanel(Static):
    def __init__(self, token_name, **kwargs):
        super().__init__(**kwargs)
        self.token_name = token_name
    def on_mount(self):
        self.border_title=self.token_name

    def update_metrics(self, metrics, bid_price: float, ask_price: float, max_visible_shares: float = 100000):
        """
        更新深度面板（带全面的 None 值与崩溃防御）
        """
        # 防御 1：如果整个 metrics 数据包为 None，显示等待/空仓状态
        if metrics is None or not hasattr(metrics, 'ask_depth') or not hasattr(metrics, 'bid_depth'):
            self.update("[bold yellow]⚠️ 正在等待订单簿数据...[/]")
            return

        # 防御 2：安全提取 shares 和 notional，防止内部属性为 None
        ask_shares = getattr(metrics.ask_depth, 'shares', 0.0) or 0.0
        ask_notional = getattr(metrics.ask_depth, 'notional', 0.0) or 0.0
        bid_shares = getattr(metrics.bid_depth, 'shares', 0.0) or 0.0
        bid_notional = getattr(metrics.bid_depth, 'notional', 0.0) or 0.0

        # 防御 3：处理价格为 None 的情况，准备好安全的显示字符串
        ask_price_str = f"{ask_price:.2f}" if ask_price is not None else "--.--"
        bid_price_str = f"{bid_price:.2f}" if bid_price is not None else "--.--"

        # 4. 安全计算直方图长度
        def get_scaled_bar(shares_val):
            # 防止 max_visible_shares 为 0 导致除以 0 异常
            denominator = max_visible_shares if max_visible_shares > 0 else 100000
            percentage = min(shares_val / denominator, 1.0)
            bar_length = int(percentage * 20)
            return "█" * bar_length if bar_length > 0 else "▏"

        ask_bar = get_scaled_bar(ask_shares)
        bid_bar = get_scaled_bar(bid_shares)
        # 5. 安全渲染
        self.update(
            f"""
[bold red]ASK (Top N Depth)[/]
买1: {ask_price_str}  
张数: {ask_shares:>8.0f}  
资金: {ask_notional:>9.1f} USDC
[red]{ask_bar}[/]
[bold green]BID (Top N Depth)[/]
买1: {bid_price_str}  
张数: {bid_shares:>8.0f}  
资金: {bid_notional:>9.1f} USDC
[green]{bid_bar}[/]
"""
        )


class SlippagePanel(Static):
    def update_metrics(self, metrics):
        # 防御 1：如果 metrics 为 None，显示等待/空仓状态
        if metrics is None or not hasattr(metrics, 'buy_slippage') or not hasattr(metrics, 'sell_slippage'):
            self.update("[bold yellow]⚠️ 正在等待滑点数据...[/]")
            return

        # 防御 2：安全提取滑点数据，防止内部属性为 None
        buy_slippage = getattr(metrics.buy_slippage, 'slippage', 0.0) or 0.0
        sell_slippage = getattr(metrics.sell_slippage, 'slippage', 0.0) or 0.0
        buy_avg_price = getattr(metrics.buy_slippage, 'average_price', 0.0) or 0.0
        sell_avg_price = getattr(metrics.sell_slippage, 'average_price', 0.0) or 0.0

        buy_color = "red" if buy_slippage > 0.01 else "green"
        sell_color = "red" if sell_slippage > 0.01 else "green"

        self.update(
            f"""
Buy Avg Price: {buy_avg_price:.4f}
Buy Slippage: [{buy_color}]{buy_slippage:.2%}[/]

Sell Avg Price: {sell_avg_price:.4f}
Sell Slippage: [{sell_color}]{sell_slippage:.2%}[/]
"""
        )

class OrderBookPanel(DataTable):
    def __init__(self, token_name, **kwargs):
        super().__init__(**kwargs)
        self.token_name = token_name

    def on_mount(self):
        self.add_columns("ASK Price", "ASK Size", "BID Price", "BID Size")
        self.border_title=self.token_name

    def update_orderbook(self, asks, bids):
        self.clear()

        rows = max(len(asks), len(bids))

        for i in range(rows):
            ask_price = f"{asks[i].price:.4f}" if i < len(asks) else ""
            ask_size = f"{asks[i].size:.2f}" if i < len(asks) else ""

            bid_price = f"{bids[i].price:.4f}" if i < len(bids) else ""
            bid_size = f"{bids[i].size:.2f}" if i < len(bids) else ""

            self.add_row(
                ask_price,
                ask_size,
                bid_price,
                bid_size,
            )

class SharedState:
    def __init__(self):
        self.metrics_up: Optional[OrderBookMetrics] = None
        self.metrics_down: Optional[OrderBookMetrics] = None
        self.shutdown = False
# =========================
# DashBoard APP
# =========================

class OrderBookDashboard(App):
    CSS_PATH = "dashboard.tcss"

    def __init__(self, shared_state):
        super().__init__()
        self.shared_state = shared_state
        self.last_metrics_up = None
        self.last_metrics_down = None

    def compose(self) -> ComposeResult:
        yield Header()

        with Horizontal(id="main-grid"):
            with Vertical(id="left-column", classes="column"):
                self.up_orderbook = OrderBookPanel(classes="panel",token_name="UP/YES Order Book")
                self.down_orderbook = OrderBookPanel(classes="panel",token_name="DOWN/NO Order Book")

                yield self.up_orderbook
                yield self.down_orderbook

            with Vertical(id="right-column", classes="column"):

                self.arb = ArbPanel(classes="panel",token_name="Arb Data")
                yield self.arb

                with Horizontal(classes="right-sub-horizontal"):
                    self.up_market = MarketPanel(classes="panel",token_name="UP/YES Market Data")
                    self.down_market = MarketPanel(classes="panel",token_name="DOWN/NO Market Data")
                    yield self.up_market
                    yield self.down_market

                with Horizontal(classes="right-sub-horizontal"):  
                    self.up_depth = DepthPanel(classes="panel",token_name="UP/YES N Depth")
                    self.down_depth = DepthPanel(classes="panel",token_name="DOWN/NO N Depth")
                    yield self.up_depth
                    yield self.down_depth

                with Horizontal(classes="right-sub-horizontal"):
                    self.up_n_orderbook = OrderBookPanel(classes="panel",token_name="UP/YES N OrderBook")
                    self.down_n_orderbook = OrderBookPanel(classes="panel",token_name="DOWN/NO N OrderBook")
                    yield self.up_n_orderbook
                    yield self.down_n_orderbook

        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.1, self.poll_state)

    def poll_state(self):
        metrics_up = self.shared_state.metrics_up
        metrics_down = self.shared_state.metrics_down

        if metrics_up is None or metrics_down is None:
            return

        if metrics_up   == self.last_metrics_up and metrics_down == self.last_metrics_down:
            return

        self.last_metrics_up = metrics_up
        self.last_metrics_down = metrics_down
        self.refresh_dashboard(metrics_up, metrics_down)

    def refresh_dashboard(self, metrics_up, metrics_down):

        self.up_market.update_metrics(metrics_up)
        self.down_market.update_metrics(metrics_down)

        self.up_depth.update_metrics(
            metrics_up,metrics_up.asks[-1].price if metrics_up.asks else None,
            metrics_up.bids[-1].price if metrics_up.bids else None)

        self.down_depth.update_metrics(
            metrics_down, metrics_down.asks[-1].price if metrics_down.asks else None,
            metrics_down.bids[-1].price if metrics_down.bids else None)

        self.up_orderbook.update_orderbook(metrics_up.asks, metrics_up.bids)
        self.down_orderbook.update_orderbook(metrics_down.asks, metrics_down.bids)

        self.up_n_orderbook.update_orderbook(metrics_up.asks[:-11:-1],metrics_up.bids[:-11:-1])
        self.down_n_orderbook.update_orderbook(metrics_down.asks[:-11:-1],metrics_down.bids[:-11:-1])

        self.arb.update_metrics(metrics_up,metrics_down)