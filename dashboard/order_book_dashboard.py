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
    def update_metrics(self, metrics):
        liquidity = "HIGH" if metrics.depth_ratio > 0.7 else "MEDIUM" if metrics.depth_ratio > 0.4 else "LOW"

        self.update(
            f"""
[bold yellow]Spread:[/] {metrics.bid_ask_spread:.4f}
[bold cyan]Depth Ratio:[/] {metrics.depth_ratio:.4f}
"""
        )


class DepthPanel(Static):
    def update_metrics(self, metrics):
        bid_bar = depth_bar(metrics.bid_depth.shares)
        ask_bar = depth_bar(metrics.ask_depth.shares)

        self.update(
            f"""
[bold red]ASK[/]
0.47 {metrics.ask_depth.shares:>8.0f} [red]{ask_bar}[/]

[bold green]BID[/]
0.46 {metrics.bid_depth.shares:>8.0f} [green]{bid_bar}[/]
"""
        )


class SlippagePanel(Static):
    def update_metrics(self, metrics):
        buy_color = "red" if metrics.buy_slippage.slippage > 0.01 else "green"
        sell_color = "red" if metrics.sell_slippage.slippage > 0.01 else "green"

        self.update(
            f"""
Buy Avg Price: {metrics.buy_slippage.average_price:.4f}
Buy Slippage: [{buy_color}]{metrics.buy_slippage.slippage:.2%}[/]

Sell Avg Price: {metrics.sell_slippage.average_price:.4f}
Sell Slippage: [{sell_color}]{metrics.sell_slippage.slippage:.2%}[/]
"""
        )

class OrderBookPanel(DataTable):
    def on_mount(self):
        self.add_columns("ASK Price", "ASK Size", "BID Price", "BID Size")

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
        self.metrics: Optional[OrderBookMetrics] = None
        self.shutdown = False
# =========================
# DashBoard APP
# =========================

class OrderBookDashboard(App):
    CSS_PATH = "dashboard.tcss"

    def __init__(self, shared_state):
        super().__init__()
        self.shared_state = shared_state
        self.last_metrics = None

    def compose(self) -> ComposeResult:
        yield Header()

        with Horizontal(id="main-grid"):
            #self.market = MarketPanel(classes="panel")
            #self.depth = DepthPanel(classes="panel")
            #self.slippage = SlippagePanel(classes="panel")
            self.orderbook = OrderBookPanel(classes="pannel")
            #yield self.market
            #yield self.depth
            #yield self.slippage
            yield self.orderbook

        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.1, self.poll_state)

    def poll_state(self):
        metrics = self.shared_state.metrics

        if metrics is None:
            return

        if metrics == self.last_metrics:
            return

        self.last_metrics = metrics
        self.refresh_dashboard(metrics)

    def refresh_dashboard(self, metrics):
        #self.market.update_metrics(metrics)
        #self.depth.update_metrics(metrics)
        #self.slippage.update_metrics(metrics)
        self.orderbook.update_orderbook(metrics.asks, metrics.bids)