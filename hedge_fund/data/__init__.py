"""v2 data pipeline — data provider protocol, clients, and response models.

Two providers implement the same `DataClient` protocol:

    YFinanceClient  Yahoo Finance via yfinance — free, no API key (default)
    FDClient        Financial Datasets — paid, needs FINANCIAL_DATASETS_API_KEY

`make_data_client()` picks one; everything downstream (backtests, paper
trading, the TUI) takes it as a `DataClient` and never knows which.
"""

import os

from hedge_fund.data.cached import CachedDataClient
from hedge_fund.data.client import FDClient, FDClientError
from hedge_fund.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsData,
    EarningsRecord,
    Filing,
    FinancialMetrics,
    InsiderTrade,
    Price,
)
from hedge_fund.data.protocol import DataClient
from hedge_fund.data.yfinance_client import YFinanceClient, YFinanceError

# Set to "financialdatasets" to switch back to the paid provider.
PROVIDER_ENV = "HEDGE_FUND_DATA_PROVIDER"
_FD_PROVIDER = "financialdatasets"


def make_data_client() -> DataClient:
    """The data client the pipeline runs on.

    Defaults to YFinanceClient (free, no API key). Set
    ``HEDGE_FUND_DATA_PROVIDER=financialdatasets`` to use the paid Financial
    Datasets API instead, which needs FINANCIAL_DATASETS_API_KEY.
    """
    if os.environ.get(PROVIDER_ENV, "").strip().lower() == _FD_PROVIDER:
        return FDClient()
    return YFinanceClient()


__all__ = [
    "CachedDataClient",
    "CompanyFacts",
    "CompanyNews",
    "DataClient",
    "Earnings",
    "EarningsData",
    "EarningsRecord",
    "FDClient",
    "FDClientError",
    "Filing",
    "FinancialMetrics",
    "InsiderTrade",
    "PROVIDER_ENV",
    "Price",
    "YFinanceClient",
    "YFinanceError",
    "make_data_client",
]
