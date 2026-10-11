"""YFinanceClient contract tests — fake tickers, no network.

Pins the guarantees the pipeline relies on from the free provider:

1. End-inclusive prices. yfinance's ``end`` is exclusive, and exact_marks()
   asks for a single session with start == end; unpadded, that call returns
   no bars and every execution fails. The client must pad the request.
2. Fail-loud. Infrastructure failures raise YFinanceError; "no data" is an
   empty list. yfinance's own default swallows failures into an empty frame,
   which a backtest would read as "no signal".
3. Point-in-time. get_financial_metrics filters on the estimated filing
   date, never on the fiscal period end.
"""

import pandas as pd
import pytest

from hedge_fund.data import (
    DataClient,
    FDClient,
    YFinanceClient,
    YFinanceError,
    make_data_client,
)
from hedge_fund.data.yfinance_client import _ratio, _surprise


class FakeTicker:
    """Stands in for yfinance.Ticker and records every history() call."""

    def __init__(self, *, history=None, info=None, income=None, balance=None,
                 cashflow=None, earnings_history=None, news=None, history_error=None):
        self._history = history if history is not None else pd.DataFrame()
        self._history_error = history_error
        self.info = info or {}
        self.quarterly_income_stmt = income if income is not None else pd.DataFrame()
        self.quarterly_balance_sheet = balance if balance is not None else pd.DataFrame()
        self.quarterly_cashflow = cashflow if cashflow is not None else pd.DataFrame()
        self.income_stmt = self.balance_sheet = self.cashflow = pd.DataFrame()
        self.earnings_history = earnings_history if earnings_history is not None else pd.DataFrame()
        self.news = news or []
        self.history_calls: list[dict] = []

    def history(self, **kwargs):
        self.history_calls.append(kwargs)
        if self._history_error is not None:
            raise self._history_error
        return self._history


def client_for(ticker: FakeTicker) -> YFinanceClient:
    client = YFinanceClient()
    client._ticker = lambda symbol: ticker
    return client


def bars(*days, close=1.5):
    return pd.DataFrame(
        {"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": close, "Volume": 100},
        index=pd.DatetimeIndex(list(days)),
    )


# ---------------------------------------------------------------------------
# Protocol and factory
# ---------------------------------------------------------------------------

def test_satisfies_the_data_client_protocol():
    assert isinstance(YFinanceClient(), DataClient)


def test_make_data_client_defaults_to_yfinance(monkeypatch):
    monkeypatch.delenv("HEDGE_FUND_DATA_PROVIDER", raising=False)
    assert isinstance(make_data_client(), YFinanceClient)


def test_make_data_client_can_select_financial_datasets(monkeypatch):
    monkeypatch.setenv("HEDGE_FUND_DATA_PROVIDER", "FinancialDatasets")
    client = make_data_client()
    assert isinstance(client, FDClient)
    client.close()


# ---------------------------------------------------------------------------
# Prices: end-inclusive, session-dated, ascending
# ---------------------------------------------------------------------------

def test_single_session_request_returns_that_bar():
    """exact_marks() calls get_prices(ticker, session, session)."""
    t = FakeTicker(history=bars("2025-01-15"))
    prices = client_for(t).get_prices("AAPL", "2025-01-15", "2025-01-15")
    assert [p.time for p in prices] == ["2025-01-15"]
    # yfinance's end is exclusive, so the request must be padded by one day.
    assert t.history_calls[0]["start"] == "2025-01-15"
    assert t.history_calls[0]["end"] == "2025-01-16"


def test_prices_ask_yfinance_to_raise_instead_of_swallowing_failures():
    t = FakeTicker(history=bars("2025-01-15"))
    client_for(t).get_prices("AAPL", "2025-01-15", "2025-01-15")
    assert t.history_calls[0]["raise_errors"] is True


def test_prices_map_interval_names():
    t = FakeTicker(history=bars("2025-01-15"))
    client = client_for(t)
    client.get_prices("AAPL", "2025-01-01", "2025-01-15", interval="week")
    assert t.history_calls[-1]["interval"] == "1wk"
    with pytest.raises(YFinanceError):
        client.get_prices("AAPL", "2025-01-01", "2025-01-15", interval="minute")


def test_prices_sorted_ascending_with_session_dates():
    t = FakeTicker(history=bars("2025-01-16", "2025-01-15"))
    prices = client_for(t).get_prices("AAPL", "2025-01-15", "2025-01-16")
    assert [p.time for p in prices] == ["2025-01-15", "2025-01-16"]
    assert prices[0].close == 1.5
    assert prices[0].volume == 100


def test_bar_without_a_close_is_skipped():
    frame = bars("2025-01-15", "2025-01-16")
    frame.loc[frame.index[0], "Close"] = float("nan")
    prices = client_for(FakeTicker(history=frame)).get_prices("AAPL", "2025-01-15", "2025-01-16")
    assert [p.time for p in prices] == ["2025-01-16"]


# ---------------------------------------------------------------------------
# Fail-loud contract
# ---------------------------------------------------------------------------

def test_no_bars_is_no_data():
    assert client_for(FakeTicker()).get_prices("ZZZZ", "2025-01-15", "2025-01-15") == []


def test_prices_missing_error_is_no_data():
    from yfinance.exceptions import YFPricesMissingError
    t = FakeTicker(history_error=YFPricesMissingError("ZZZZ", ""))
    assert client_for(t).get_prices("ZZZZ", "2025-01-15", "2025-01-15") == []


def test_network_failure_raises():
    t = FakeTicker(history_error=ConnectionError("boom"))
    with pytest.raises(YFinanceError):
        client_for(t).get_prices("AAPL", "2025-01-15", "2025-01-15")


def test_rate_limit_retries_then_raises(monkeypatch):
    from yfinance.exceptions import YFRateLimitError
    monkeypatch.setattr("hedge_fund.data.yfinance_client.time.sleep", lambda s: None)
    t = FakeTicker(history_error=YFRateLimitError())
    with pytest.raises(YFinanceError, match="rate limited"):
        client_for(t).get_prices("AAPL", "2025-01-15", "2025-01-15")
    assert len(t.history_calls) == 4  # three retries, then give up


# ---------------------------------------------------------------------------
# Financial metrics: point-in-time
# ---------------------------------------------------------------------------

INCOME = pd.DataFrame({
    pd.Timestamp("2024-09-30"): {"Total Revenue": 100.0, "Gross Profit": 40.0, "Operating Income": 20.0,
                                 "Net Income": 10.0, "Basic EPS": 1.0, "Basic Average Shares": 10.0,
                                 "Interest Expense": -2.0},
    pd.Timestamp("2024-06-30"): {"Total Revenue": 90.0, "Gross Profit": 36.0, "Operating Income": 18.0,
                                 "Net Income": 9.0, "Basic EPS": 0.9, "Basic Average Shares": 10.0,
                                 "Interest Expense": -2.0},
    pd.Timestamp("2023-09-30"): {"Total Revenue": 80.0, "Gross Profit": 30.0, "Operating Income": 15.0,
                                 "Net Income": 8.0, "Basic EPS": 0.8, "Basic Average Shares": 10.0,
                                 "Interest Expense": -2.0},
})
BALANCE = pd.DataFrame({
    col: {"Total Assets": 200.0, "Stockholders Equity": 100.0, "Current Assets": 50.0,
          "Current Liabilities": 25.0, "Total Debt": 50.0, "Inventory": 10.0}
    for col in INCOME.columns
})
CASHFLOW = pd.DataFrame({col: {"Free Cash Flow": 5.0} for col in INCOME.columns})


def _statements(**overrides):
    return FakeTicker(income=INCOME, balance=BALANCE, cashflow=CASHFLOW,
                      history=bars("2024-09-30", close=overrides.get("close", 1.5)))


def test_metrics_filter_on_estimated_filing_date_not_report_period():
    """A quarter ending 2024-09-30 is public ~40 days later. As of
    2024-10-15 it must be invisible; as of 2024-11-15 it must appear."""
    client = client_for(_statements())
    before = client.get_financial_metrics("AAPL", "2024-10-15")
    assert [m.report_period for m in before] == ["2024-06-30", "2023-09-30"]
    after = client.get_financial_metrics("AAPL", "2024-11-15")
    assert after[0].report_period == "2024-09-30"
    assert after[0].filing_date == "2024-11-09"  # 2024-09-30 + 40 days
    assert all(m.filing_date <= "2024-11-15" for m in after)


def test_metrics_ratios_and_annualized_pe():
    m = client_for(_statements(close=2.0)).get_financial_metrics("AAPL", "2024-12-31")[0]
    assert m.period == "quarterly"
    assert m.gross_margin == pytest.approx(0.4)
    assert m.operating_margin == pytest.approx(0.2)
    assert m.net_margin == pytest.approx(0.1)
    assert m.return_on_equity == pytest.approx(0.1)
    assert m.return_on_assets == pytest.approx(0.05)
    assert m.debt_to_equity == pytest.approx(0.5)
    assert m.current_ratio == pytest.approx(2.0)
    assert m.quick_ratio == pytest.approx(1.6)
    assert m.interest_coverage == pytest.approx(10.0)
    assert m.earnings_per_share == pytest.approx(1.0)
    assert m.book_value_per_share == pytest.approx(10.0)
    assert m.free_cash_flow_per_share == pytest.approx(0.5)
    # Market cap is the close on the filing date times shares: 2.0 * 10.
    assert m.market_cap == pytest.approx(20.0)
    # P/E annualizes a quarter's net income: 20 / (10 * 4).
    assert m.price_to_earnings_ratio == pytest.approx(0.5)
    # Growth is year over year: 2024-09-30 against 2023-09-30.
    assert m.revenue_growth == pytest.approx(0.25)
    assert m.earnings_growth == pytest.approx(0.25)


def test_metrics_value_every_period_from_one_price_fetch():
    t = _statements()
    client_for(t).get_financial_metrics("AAPL", "2024-12-31")
    assert len(t.history_calls) == 1
    assert t.history_calls[0]["start"] == "2023-09-30"


def test_metrics_respect_limit():
    assert len(client_for(_statements()).get_financial_metrics("AAPL", "2024-12-31", limit=2)) == 2


def test_metrics_fall_back_to_profile_when_no_statements():
    t = FakeTicker(info={"marketCap": 3.0e12, "trailingPE": 30.0, "returnOnEquity": 1.5})
    rows = client_for(t).get_financial_metrics("AAPL", "2024-12-31")
    assert len(rows) == 1
    assert rows[0].period == "ttm"
    assert rows[0].filing_date == "2024-12-31"
    assert rows[0].market_cap == 3.0e12


def test_unknown_ticker_has_no_metrics():
    assert client_for(FakeTicker()).get_financial_metrics("ZZZZ", "2024-12-31") == []


# ---------------------------------------------------------------------------
# Earnings, facts, market cap, news, insiders
# ---------------------------------------------------------------------------

def test_earnings_history_derives_surprise_newest_first():
    hist = pd.DataFrame(
        {"EPS Estimate": [1.0, 1.0, 1.0], "EPS": [1.2, 0.8, 1.0]},
        index=pd.DatetimeIndex(["2024-03-31", "2024-06-30", "2024-09-30"]),
    )
    records = client_for(FakeTicker(earnings_history=hist)).get_earnings_history("AAPL")
    assert [r.report_period for r in records] == ["2024-09-30", "2024-06-30", "2024-03-31"]
    assert [r.quarterly.eps_surprise for r in records] == ["MEET", "MISS", "BEAT"]
    assert records[0].source_type == "10-Q"
    assert records[0].filing_date == "2024-11-09"
    assert records[0].quarterly.estimated_earnings_per_share == 1.0


def test_surprise_boundaries():
    assert _surprise(None, 1.0) is None
    assert _surprise(1.004, 1.0) == "MEET"
    assert _surprise(1.1, 1.0) == "BEAT"
    assert _surprise(-0.5, -1.0) == "BEAT"  # a smaller loss than expected


def test_company_facts_from_profile():
    t = FakeTicker(info={"shortName": "Apple", "sector": "Technology", "industry": "Consumer Electronics",
                         "exchange": "NMS", "city": "Cupertino", "country": "United States"})
    facts = client_for(t).get_company_facts("AAPL")
    assert facts.name == "Apple"
    assert facts.sector == "Technology"
    assert facts.location == "Cupertino, United States"
    assert client_for(FakeTicker()).get_company_facts("ZZZZ") is None


def test_market_cap_is_point_in_time():
    t = FakeTicker(info={"sharesOutstanding": 10, "marketCap": 999.0}, history=bars("2024-12-31", close=3.0))
    assert client_for(t).get_market_cap("AAPL", "2024-12-31") == pytest.approx(30.0)


def test_news_reads_the_nested_schema_and_filters_by_date():
    article = {"content": {"title": "Apple beats", "pubDate": "2024-12-31T14:00:00Z",
                           "provider": {"displayName": "Reuters"},
                           "canonicalUrl": {"url": "https://example.com/a"}}}
    later = {"content": {"title": "Later", "pubDate": "2025-01-05T14:00:00Z",
                         "provider": {"displayName": "Reuters"}}}
    news = client_for(FakeTicker(news=[article, later])).get_news("AAPL", "2024-12-31")
    assert len(news) == 1
    assert news[0].source == "Reuters"
    assert news[0].date == "2024-12-31"
    assert news[0].url == "https://example.com/a"


def test_insider_trades_unavailable():
    assert client_for(FakeTicker()).get_insider_trades("AAPL", "2024-12-31") == []


def test_ratio_guards():
    assert _ratio(1.0, 0) is None
    assert _ratio(None, 1.0) is None
    assert _ratio(1.0, 4.0) == 0.25
