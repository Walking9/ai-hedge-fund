"""Yahoo Finance (yfinance) data client — the free, key-less DataClient.

Implements the same DataClient protocol as FDClient using the `yfinance`
library, so the whole pipeline (backtests, paper trading, the TUI) runs with no
API key and no subscription. `make_data_client()` returns this by default.

Fail-loud contract (see protocol.DataClient): an empty list / None means the
data genuinely does not exist, and infrastructure failures RAISE. yfinance's
own default is the opposite — it logs and hands back an empty frame, which the
pipeline would read as "no signal" and silently poison a backtest. So every
price fetch here passes ``raise_errors=True`` and maps yfinance's typed
exceptions:

    YFPricesMissingError / YFTickerMissingError -> [] (a data fact)
    YFRateLimitError                            -> retry, then raise
    anything else (YFException, network, parse)  -> raise YFinanceError

Prices are the anchor of that contract. yfinance's fundamentals path offers no
reliable way to tell "this company has no statements" from "the fetch failed"
(it swallows the error and returns an empty frame), so there we surface typed
exceptions when they are raised and otherwise treat empty as no-data. That is a
weaker guarantee than the price path, and worth knowing.

Point-in-time caveat (the part that matters for backtests): Yahoo does not
expose SEC filing dates. We approximate ``filing_date`` as ``report_period``
plus the statutory filing deadline — 40 days for a 10-Q, 60 for a 10-K, on the
large-accelerated-filer rules. 40 days is conservative: a 10-Q is due then and
is often filed earlier, so the approximation never leaks the future for
quarterly rows. For annual rows a 10-K is due in 60 days, so an early filer can
leak up to the difference. Every metrics row is filtered on this date, so a
backtest only ever sees data that was (per this approximation) public by its
as_of date.

Coverage vs Financial Datasets:
- Prices: full OHLCV history, the same day/week/month intervals.
- Fundamentals: ~4-5 quarters or 4-5 years of Yahoo's free statements. The
  snapshot needs 4 filed periods, so an LLM agent abstains more readily here
  than on the paid source, most visibly just before a filing.
- Earnings: EPS actual vs. estimate with a derived BEAT/MISS/MEET label.
- Company facts: name, sector, industry, exchange, location (no CIK/SIC).
- News: recent Yahoo headlines only.
- Insider trades: NOT available — returns [] (the protocol's "no data").

PEAD note: PEADModel defaults to ``announcement_only=True``, which accepts only
8-K rows. Yahoo carries no 8-K data, so PEAD forms no view on this provider
unless it is constructed with ``announcement_only=False``, where it fires on the
estimated 10-Q filing date — a lagged proxy for the announcement, not the
announcement itself.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

from hedge_fund.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsData,
    EarningsRecord,
    FinancialMetrics,
    InsiderTrade,
    Price,
)

logger = logging.getLogger(__name__)

# Statutory filing deadlines (large accelerated filer), used as the
# point-in-time proxy for "when did this become public". See module docstring.
_FILING_DELAY_DAYS = {"quarterly": 40, "annual": 60}

# Same backoff shape as FDClient, for the one failure yfinance marks as
# retryable (HTTP 429 from Yahoo).
_RETRY_DELAYS = (5, 15, 30)

# Financial Datasets interval names -> yfinance interval strings.
_INTERVALS = {"day": "d", "week": "wk", "month": "mo"}


class YFinanceError(Exception):
    """A Yahoo Finance call failed for infrastructure reasons (network, rate
    limit, parse). Distinct from "no data exists" — that returns empty. A
    backtest must crash on this, not treat it as no-data.
    """


class YFinanceClient:
    """DataClient backed by Yahoo Finance via yfinance.

    Usage::

        with YFinanceClient() as yf_client:
            prices = yf_client.get_prices("AAPL", "2024-01-01", "2024-12-31")

    One client per thread: the underlying yfinance Ticker objects (and the
    HTTP session inside them) are not shared-safe.
    """

    def __init__(self) -> None:
        self._tickers: dict[str, object] = {}
        # Yahoo's profile/statement endpoints are slow and rate-limited, and
        # one run asks for the same ticker's fundamentals repeatedly. Cache
        # per client instance; a client lives for a single run, and the disk
        # cache in CachedDataClient covers reuse across runs.
        self._info: dict[str, dict] = {}
        self._statements: dict[tuple[str, str], tuple] = {}

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "YFinanceClient":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        """Drop cached tickers and parsed payloads."""
        self._tickers.clear()
        self._info.clear()
        self._statements.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ticker(self, symbol: str):
        """The yfinance Ticker for *symbol*, created once per client."""
        import yfinance as yf

        if symbol not in self._tickers:
            self._tickers[symbol] = yf.Ticker(symbol)
        return self._tickers[symbol]

    def _info_for(self, ticker: str) -> dict:
        """Yahoo's profile blob, cached. Empty dict when unavailable."""
        if ticker in self._info:
            return self._info[ticker]
        info: dict = {}
        try:
            info = self._ticker(ticker).info or {}
        except Exception as exc:  # noqa: BLE001 - profile is best-effort
            logger.debug("yfinance info unavailable for %s: %s", ticker, exc)
            info = {}
        self._info[ticker] = info
        return info

    @staticmethod
    def _exceptions():
        """yfinance's typed exceptions, imported lazily (yfinance is heavy)."""
        from yfinance.exceptions import (
            YFException,
            YFInvalidPeriodError,
            YFPricesMissingError,
            YFRateLimitError,
            YFTickerMissingError,
        )

        return (
            YFException,
            YFInvalidPeriodError,
            YFPricesMissingError,
            YFRateLimitError,
            YFTickerMissingError,
        )

    def _history(self, ticker: str, **kwargs) -> pd.DataFrame:
        """Fetch price bars, honouring the fail-loud contract.

        Returns an empty frame for "this data does not exist" (unknown or
        delisted ticker, no bars in the window). Raises YFinanceError for
        everything else — rate limits (after retrying), network errors, and
        anything yfinance raises that is not a data fact.
        """
        (
            YFException,
            YFInvalidPeriodError,
            YFPricesMissingError,
            YFRateLimitError,
            YFTickerMissingError,
        ) = self._exceptions()

        t = self._ticker(ticker)
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                return t.history(raise_errors=True, auto_adjust=False, **kwargs)
            except YFRateLimitError as exc:
                if delay is None:
                    raise YFinanceError(
                        f"yfinance rate limited for {ticker} after "
                        f"{len(_RETRY_DELAYS)} retries"
                    ) from exc
                logger.info(
                    "yfinance rate limited, retrying in %ds (attempt %d/%d)",
                    delay, attempt + 1, len(_RETRY_DELAYS),
                )
                time.sleep(delay)
            except (YFPricesMissingError, YFTickerMissingError):
                # A delisted or unknown symbol, or no bars in this window:
                # a data fact, not a failure.
                return pd.DataFrame()
            except YFInvalidPeriodError as exc:
                raise YFinanceError(f"yfinance rejected the request for {ticker}: {exc}") from exc
            except YFException as exc:
                raise YFinanceError(f"yfinance history failed for {ticker}: {exc}") from exc
            except Exception as exc:  # noqa: BLE001 - curl_cffi/network/parse
                raise YFinanceError(f"yfinance history failed for {ticker}: {exc}") from exc

        raise YFinanceError(f"yfinance rate limited for {ticker}")  # pragma: no cover

    def _statements_for(self, ticker: str, freq: str) -> tuple:
        """(income, balance, cashflow) frames for *ticker*, cached.

        ``freq`` is "quarterly" or "annual". Empty frames mean Yahoo has no
        statements for this ticker; typed yfinance exceptions become
        YFinanceError.
        """
        key = (ticker, freq)
        if key in self._statements:
            return self._statements[key]

        (
            YFException,
            _,
            _,
            _,
            YFTickerMissingError,
        ) = self._exceptions()

        t = self._ticker(ticker)
        quarterly = freq == "quarterly"
        try:
            income = t.quarterly_income_stmt if quarterly else t.income_stmt
            balance = t.quarterly_balance_sheet if quarterly else t.balance_sheet
            cashflow = t.quarterly_cashflow if quarterly else t.cashflow
        except YFTickerMissingError:
            frames: tuple = (pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
        except YFException as exc:
            raise YFinanceError(f"yfinance financials failed for {ticker}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - network/parse
            raise YFinanceError(f"yfinance financials failed for {ticker}: {exc}") from exc
        else:
            frames = tuple(
                f if isinstance(f, pd.DataFrame) else pd.DataFrame()
                for f in (income, balance, cashflow)
            )

        self._statements[key] = frames
        return frames

    @staticmethod
    def _safe(val) -> float | None:
        """Coerce to a finite float, or None (NaN/Inf/None/unparseable)."""
        if val is None or isinstance(val, bool):
            return None
        try:
            f = float(val)
        except (TypeError, ValueError):
            return None
        return None if math.isnan(f) or math.isinf(f) else f

    @staticmethod
    def _day(ts) -> str:
        """A bar's timestamp as its session date, YYYY-MM-DD.

        yfinance indexes daily bars in the exchange's timezone, so the local
        date is the session date.
        """
        return pd.Timestamp(ts).strftime("%Y-%m-%d")

    @staticmethod
    def _end_exclusive(end_date: str) -> str:
        """yfinance's ``end`` is exclusive; every caller here means inclusive.

        This is load-bearing: exact_marks() asks for a single session with
        start == end, which under exclusive-end semantics returns no bars at
        all and fails the execution step.
        """
        return (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    @staticmethod
    def _yf_interval(interval: str, multiplier: int) -> str:
        suffix = _INTERVALS.get(interval)
        if suffix is None:
            raise YFinanceError(
                f"unsupported interval {interval!r}; expected one of "
                f"{sorted(_INTERVALS)}"
            )
        return f"1{suffix}" if multiplier == 1 else f"{multiplier}{suffix}"

    @staticmethod
    def _prior_year_column(columns: list, col):
        """The column one year before *col* (within 45 days), or None.

        Works for quarterly (four columns back) and annual (one back) alike,
        which is what lets the same growth code serve both frequencies.
        """
        target = pd.Timestamp(col) - timedelta(days=365)
        best, best_gap = None, None
        for other in columns:
            gap = abs((pd.Timestamp(other) - target).days)
            if best_gap is None or gap < best_gap:
                best, best_gap = other, gap
        return best if best_gap is not None and best_gap <= 45 else None

    # ------------------------------------------------------------------
    # Prices
    # ------------------------------------------------------------------

    def get_prices(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        interval: str = "day",
        interval_multiplier: int = 1,
    ) -> list[Price]:
        """Fetch OHLCV bars over [start_date, end_date], inclusive of both."""
        df = self._history(
            ticker,
            start=start_date,
            end=self._end_exclusive(end_date),
            interval=self._yf_interval(interval, interval_multiplier),
        )
        if df is None or df.empty:
            return []

        prices: list[Price] = []
        for ts, row in df.sort_index().iterrows():
            close = self._safe(row.get("Close"))
            if close is None:
                # A bar with no usable close can value nothing; skip it rather
                # than invent one.
                logger.debug("%s: skipping bar %s with no close", ticker, ts)
                continue
            # Open/high/low are unused by the pipeline but required by Price;
            # a missing one falls back to the close rather than dropping the
            # session (marks and executions key off the close).
            def ohlc(key):
                value = self._safe(row.get(key))
                return close if value is None else value

            prices.append(Price(
                open=ohlc("Open"),
                close=close,
                high=ohlc("High"),
                low=ohlc("Low"),
                volume=int(self._safe(row.get("Volume")) or 0),
                time=self._day(ts),
            ))
        return prices

    # ------------------------------------------------------------------
    # Financial metrics
    # ------------------------------------------------------------------

    def get_financial_metrics(
        self,
        ticker: str,
        end_date: str,
        period: str = "ttm",
        limit: int = 10,
    ) -> list[FinancialMetrics]:
        """Fetch financial metrics that were PUBLIC as of *end_date*.

        Point-in-time: rows are filtered on ``filing_date`` (report_period plus
        the statutory deadline — see the module docstring), never on
        ``report_period``, which would leak the 40-60 days before a filing was
        public into a backtest.

        ``period="annual"`` reads the yearly statements; anything else reads the
        quarterly ones. Rows are labelled with the frequency actually read, so a
        caller asking for "ttm" gets quarterly rows (Yahoo's free tier has no
        TTM series; the latest quarter is the closest available proxy) and can
        see that in ``period``.
        """
        freq = "annual" if period == "annual" else "quarterly"
        info = self._info_for(ticker)
        income, balance, cashflow = self._statements_for(ticker, freq)

        if income.empty:
            return self._metrics_from_info(ticker, info, end_date, limit)

        columns = list(income.columns)
        delay = timedelta(days=_FILING_DELAY_DAYS[freq])
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        rows: list[FinancialMetrics] = []

        # One price fetch covers every quarter's market cap (rather than one
        # request per column). Start at the earliest report period so each row
        # can be valued at its own filing date.
        closes = self._closes_for_market_cap(ticker, columns, end_date)

        for col in columns:
            report_period = self._day(col)
            filing_date = (pd.Timestamp(col) + delay).strftime("%Y-%m-%d")
            if datetime.strptime(filing_date, "%Y-%m-%d") > end_dt:
                continue  # not public yet as of end_date

            def row_val(df, key):
                if df is None or df.empty or key not in df.index:
                    return None
                return self._safe(df.loc[key, col])

            revenue = row_val(income, "Total Revenue")
            gross_profit = row_val(income, "Gross Profit")
            operating_income = row_val(income, "Operating Income")
            net_income = row_val(income, "Net Income")
            eps = row_val(income, "Basic EPS")
            diluted_eps = row_val(income, "Diluted EPS")
            shares = row_val(income, "Basic Average Shares")
            interest_expense = row_val(income, "Interest Expense")

            total_assets = row_val(balance, "Total Assets")
            total_equity = row_val(balance, "Stockholders Equity")
            current_assets = row_val(balance, "Current Assets")
            current_liabilities = row_val(balance, "Current Liabilities")
            total_debt = row_val(balance, "Total Debt")
            inventory = row_val(balance, "Inventory")

            fcf = row_val(cashflow, "Free Cash Flow")

            # Year-over-year growth, from the same period a year earlier.
            prior = self._prior_year_column(columns, col)

            def prior_val(key):
                if prior is None or key not in income.index:
                    return None
                return self._safe(income.loc[key, prior])

            revenue_growth = _growth(revenue, prior_val("Total Revenue"))
            earnings_growth = _growth(net_income, prior_val("Net Income"))

            market_cap = self._market_cap(shares, closes, filing_date)

            rows.append(FinancialMetrics(
                ticker=ticker,
                report_period=report_period,
                period=freq,
                filing_date=filing_date,
                market_cap=market_cap,
                # Annualized so the ratio is a P/E, not a quarter's
                # price-to-earnings (which would read 4x too high).
                price_to_earnings_ratio=_ratio(
                    market_cap,
                    net_income * 4 if freq == "quarterly" and net_income is not None else net_income,
                ),
                gross_margin=_ratio(gross_profit, revenue),
                operating_margin=_ratio(operating_income, revenue),
                net_margin=_ratio(net_income, revenue),
                return_on_equity=_ratio(net_income, total_equity),
                return_on_assets=_ratio(net_income, total_assets),
                debt_to_equity=_ratio(total_debt, total_equity),
                current_ratio=_ratio(current_assets, current_liabilities),
                quick_ratio=_ratio(
                    (current_assets - inventory)
                    if current_assets is not None and inventory is not None
                    else current_assets,
                    current_liabilities,
                ),
                interest_coverage=_ratio(
                    operating_income,
                    abs(interest_expense) if interest_expense is not None else None,
                ),
                revenue_growth=revenue_growth,
                earnings_growth=earnings_growth,
                earnings_per_share=eps if eps is not None else diluted_eps,
                book_value_per_share=_ratio(total_equity, shares),
                free_cash_flow_per_share=_ratio(fcf, shares),
            ))

        rows.sort(key=lambda r: r.report_period, reverse=True)
        if not rows:
            return self._metrics_from_info(ticker, info, end_date, limit)
        return rows[:limit]

    def _closes_for_market_cap(self, ticker: str, columns: list, end_date: str) -> dict[str, float]:
        """Session date -> close, spanning every report period, in one fetch."""
        if not columns:
            return {}
        start = min(self._day(col) for col in columns)
        bars = self.get_prices(ticker, start, end_date)
        return {bar.time[:10]: bar.close for bar in bars}

    @staticmethod
    def _market_cap(shares: float | None, closes: dict[str, float], filing_date: str) -> float | None:
        """Shares outstanding times the close on the filing date.

        Point-in-time: the last close at or before the filing date, so the
        figure is what was knowable then. None when shares or prices are
        missing.
        """
        if not shares or not closes:
            return None
        eligible = [day for day in closes if day <= filing_date]
        if not eligible:
            return None
        return closes[max(eligible)] * shares

    def _metrics_from_info(self, ticker, info, end_date, limit) -> list[FinancialMetrics]:
        """Latest-only fallback for tickers Yahoo has no statements for.

        These values are current, not point-in-time, so the row is dated
        ``end_date`` and labelled "ttm" — the same shape FDClient returns for
        a TTM row. Callers that need true point-in-time history should treat
        this as a last resort (the snapshot does: it reads the latest FILED
        metrics row, never get_market_cap).
        """
        if not info:
            return []
        return [FinancialMetrics(
            ticker=ticker,
            report_period=end_date,
            period="ttm",
            filing_date=end_date,
            market_cap=self._safe(info.get("marketCap")),
            enterprise_value=self._safe(info.get("enterpriseValue")),
            price_to_earnings_ratio=self._safe(info.get("trailingPE")),
            price_to_book_ratio=self._safe(info.get("priceToBook")),
            price_to_sales_ratio=self._safe(info.get("priceToSalesTrailing12Months")),
            enterprise_value_to_ebitda_ratio=self._safe(info.get("enterpriseToEbitda")),
            enterprise_value_to_revenue_ratio=self._safe(info.get("enterpriseToRevenue")),
            gross_margin=self._safe(info.get("grossMargins")),
            operating_margin=self._safe(info.get("operatingMargins")),
            net_margin=self._safe(info.get("profitMargins")),
            return_on_equity=self._safe(info.get("returnOnEquity")),
            return_on_assets=self._safe(info.get("returnOnAssets")),
            debt_to_equity=self._safe(info.get("debtToEquity")),
            current_ratio=self._safe(info.get("currentRatio")),
            quick_ratio=self._safe(info.get("quickRatio")),
            revenue_growth=self._safe(info.get("revenueGrowth")),
            earnings_growth=self._safe(info.get("earningsGrowth")),
            earnings_per_share=self._safe(info.get("trailingEps")),
            book_value_per_share=self._safe(info.get("bookValue")),
        )][:limit]

    # ------------------------------------------------------------------
    # News
    # ------------------------------------------------------------------

    def get_news(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[CompanyNews]:
        """Recent Yahoo headlines, newest first, filtered to <= end_date.

        Yahoo serves only a short recent window, so this is thin compared with
        the paid source. A fetch failure returns [] — news is enrichment, not a
        valuation input, and it must not break a run.
        """
        try:
            items = self._ticker(ticker).news or []
        except Exception as exc:  # noqa: BLE001 - enrichment only
            logger.debug("yfinance news unavailable for %s: %s", ticker, exc)
            return []

        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
        results: list[CompanyNews] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            # yfinance 0.2.5x nests everything under "content"; older releases
            # are flat. Read both shapes.
            content = item.get("content") if isinstance(item.get("content"), dict) else item
            date_str = self._news_date(item, content, end_dt)
            if date_str is None and self._news_published_at(item, content) is not None:
                continue  # published after end_date
            provider = content.get("provider")
            if isinstance(provider, dict):
                source = provider.get("displayName") or provider.get("name")
            else:
                source = provider or item.get("publisher")
            url = content.get("canonicalUrl")
            if isinstance(url, dict):
                url = url.get("url")
            results.append(CompanyNews(
                ticker=ticker,
                title=content.get("title") or item.get("title") or "",
                source=source or "Yahoo Finance",
                date=date_str,
                url=url or item.get("link"),
            ))
            if len(results) >= limit:
                break
        return results

    @classmethod
    def _news_published_at(cls, item: dict, content: dict):
        """The article's publication time, or None when Yahoo omits it."""
        stamp = content.get("pubDate") or item.get("providerPublishTime")
        if stamp is None:
            return None
        if isinstance(stamp, (int, float)):
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        try:
            parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    @classmethod
    def _news_date(cls, item: dict, content: dict, end_dt: datetime) -> str | None:
        """The article date, or None when undated or published after end_date."""
        published = cls._news_published_at(item, content)
        if published is None:
            return None
        if published.date() > end_dt.date():
            return None
        return published.strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    # Insider trades (not available from Yahoo)
    # ------------------------------------------------------------------

    def get_insider_trades(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[InsiderTrade]:
        """Always empty: Yahoo Finance carries no insider-transaction feed."""
        return []

    # ------------------------------------------------------------------
    # Company facts
    # ------------------------------------------------------------------

    def get_company_facts(self, ticker: str) -> CompanyFacts | None:
        """Company metadata from Yahoo's profile. None when unknown."""
        info = self._info_for(ticker)
        if not info:
            return None
        location = ", ".join(
            part for part in (info.get("city"), info.get("country")) if part
        ) or None
        return CompanyFacts(
            ticker=ticker,
            name=info.get("shortName") or info.get("longName"),
            sector=info.get("sector"),
            industry=info.get("industry"),
            exchange=info.get("exchange"),
            location=location,
            is_active=True,
        )

    # ------------------------------------------------------------------
    # Earnings
    # ------------------------------------------------------------------

    def get_earnings(self, ticker: str) -> Earnings | None:
        """The latest reported quarter's headline figures."""
        records = self.get_earnings_history(ticker, limit=1)
        if not records:
            return None
        latest = records[0]
        return Earnings(
            ticker=ticker,
            report_period=latest.report_period,
            fiscal_period=None,
            quarterly=latest.quarterly,
        )

    def get_earnings_history(
        self,
        ticker: str,
        limit: int = 12,
    ) -> list[EarningsRecord]:
        """Reported quarters as EarningsRecords, newest first.

        Yahoo indexes this by fiscal period end and carries the consensus EPS
        estimate alongside the actual, so ``eps_surprise`` is derived here
        (BEAT / MISS / MEET) — it is what PEAD keys off. ``source_type`` is
        "10-Q" and ``filing_date`` is the estimated filing date (see the module
        docstring); Yahoo has no 8-K announcement dates, so PEAD's
        announcement-only default finds nothing on this provider.
        """
        try:
            hist = self._ticker(ticker).earnings_history
        except Exception as exc:  # noqa: BLE001 - no earnings is a data fact
            logger.debug("yfinance earnings history unavailable for %s: %s", ticker, exc)
            return []
        if hist is None or getattr(hist, "empty", True):
            return []

        delay = timedelta(days=_FILING_DELAY_DAYS["quarterly"])
        records: list[EarningsRecord] = []
        for idx, row in hist.iterrows():
            report_period = self._day(idx)
            actual = self._safe(row.get("EPS"))
            if actual is None:
                actual = self._safe(row.get("Reported EPS"))
            estimate = self._safe(row.get("EPS Estimate"))
            records.append(EarningsRecord(
                ticker=ticker,
                report_period=report_period,
                source_type="10-Q",
                filing_date=(pd.Timestamp(idx) + delay).strftime("%Y-%m-%d"),
                quarterly=EarningsData(
                    earnings_per_share=actual,
                    estimated_earnings_per_share=estimate,
                    eps_surprise=_surprise(actual, estimate),
                ),
            ))

        records.sort(key=lambda r: r.report_period, reverse=True)
        return records[:limit]

    # ------------------------------------------------------------------
    # Market cap
    # ------------------------------------------------------------------

    def get_market_cap(self, ticker: str, end_date: str) -> float | None:
        """Market cap as of *end_date*: shares outstanding times that close.

        Computed from prices so it is point-in-time; Yahoo's ``marketCap``
        field is a live value and is used only when shares are unavailable.
        """
        info = self._info_for(ticker)
        shares = self._safe(info.get("sharesOutstanding"))
        if shares:
            bars = self.get_prices(ticker, end_date, end_date)
            if bars:
                return bars[-1].close * shares
        return self._safe(info.get("marketCap"))


def _growth(latest: float | None, prior: float | None) -> float | None:
    """Year-over-year growth rate. None unless both values are usable.

    A non-positive prior period has no meaningful growth rate, so it yields
    None rather than a sign-flipped number.
    """
    if latest is None or prior is None or prior <= 0:
        return None
    return (latest - prior) / prior


def _surprise(actual: float | None, estimate: float | None) -> str | None:
    """BEAT / MISS / MEET from an actual vs. consensus EPS print.

    None when either side is missing — the protocol's "no data", which keeps
    PEAD from reading a missing estimate as a surprise.
    """
    if actual is None or estimate is None:
        return None
    if estimate == 0:
        return "MEET" if actual == 0 else ("BEAT" if actual > 0 else "MISS")
    diff = (actual - estimate) / abs(estimate)
    if diff > 0.005:
        return "BEAT"
    if diff < -0.005:
        return "MISS"
    return "MEET"


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    """numerator / denominator, or None when undefined or non-finite."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    try:
        val = numerator / denominator
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return None if math.isnan(val) or math.isinf(val) else val
