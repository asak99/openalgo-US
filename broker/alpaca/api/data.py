import json
import os
from datetime import datetime, timedelta

import pandas as pd

from broker.alpaca.api.order_api import _get_base_url, _get_headers, _parse_auth
from broker.alpaca.database.master_contract_db import SymToken, db_session
from database.token_db import get_br_symbol, get_oa_symbol
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

# Alpaca Data API base URLs
DATA_BASE_URL = "https://data.alpaca.markets"


class AlpacaPermissionError(Exception):
    pass


class AlpacaAPIError(Exception):
    pass


def _get_data_headers(auth):
    """Build headers for Alpaca Data API."""
    api_key, api_secret, _, _ = _parse_auth(auth)
    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
        "Accept": "application/json",
    }


def get_api_response(endpoint, auth, base_url=None, method="GET", payload=None):
    """Make an API request to Alpaca's Data API."""
    client = get_httpx_client()
    headers = _get_data_headers(auth)
    if not base_url:
        base_url = DATA_BASE_URL
    url = f"{base_url}{endpoint}"

    try:
        if method.upper() == "GET":
            response = client.get(url, headers=headers)
        elif method.upper() == "POST":
            response = client.post(url, headers=headers, json=payload)
        else:
            raise AlpacaAPIError(f"Unsupported HTTP method: {method}")

        if response.status_code == 401:
            raise AlpacaPermissionError("Authentication failed or token expired")
        if response.status_code == 403:
            raise AlpacaPermissionError("Permission denied")

        response.raise_for_status()
        return response.json() if response.text else {}

    except (AlpacaPermissionError, AlpacaAPIError):
        raise
    except Exception as e:
        logger.exception(f"Alpaca Data API error: {e}")
        raise AlpacaAPIError(f"API request failed: {str(e)}")


def get_quotes(symbol, exchange, auth):
    """Get latest quote for a symbol."""
    br_symbol = get_br_symbol(symbol, exchange)

    # Use latest trade + latest quote
    try:
        trade_data = get_api_response(f"/v2/stocks/{br_symbol}/trades/latest", auth)
        quote_data = get_api_response(f"/v2/stocks/{br_symbol}/quotes/latest", auth)

        trade = trade_data.get("trade", {})
        quote = quote_data.get("quote", {})

        # Also get snapshot for OHLC
        snapshot = get_api_response(f"/v2/stocks/{br_symbol}/snapshot", auth)
        daily_bar = snapshot.get("dailyBar", snapshot.get("daily_bar", {}))
        prev_daily = snapshot.get("prevDailyBar", snapshot.get("prev_daily_bar", {}))

        ltp = trade.get("p", trade.get("price", 0))
        prev_close = prev_daily.get("c", prev_daily.get("close", 0))

        return {
            "ltp": ltp,
            "open": daily_bar.get("o", daily_bar.get("open", 0)),
            "high": daily_bar.get("h", daily_bar.get("high", 0)),
            "low": daily_bar.get("l", daily_bar.get("low", 0)),
            "close": prev_close,
            "volume": daily_bar.get("v", daily_bar.get("volume", 0)),
            "prev_close": prev_close,
            "bid": quote.get("bp", quote.get("bid_price", 0)),
            "ask": quote.get("ap", quote.get("ask_price", 0)),
            "change": ltp - prev_close if prev_close else 0,
            "change_percent": ((ltp - prev_close) / prev_close * 100) if prev_close else 0,
        }
    except Exception as e:
        logger.error(f"Error getting quotes for {br_symbol}: {e}")
        return {
            "ltp": 0, "open": 0, "high": 0, "low": 0, "close": 0,
            "volume": 0, "prev_close": 0, "bid": 0, "ask": 0,
            "change": 0, "change_percent": 0,
        }


def get_ltp(symbol, exchange, auth):
    """Get Last Traded Price."""
    quote = get_quotes(symbol, exchange, auth)
    return quote.get("ltp", 0)


def get_market_depth(symbol, exchange, auth):
    """Get market depth (basic bid/ask from Alpaca)."""
    br_symbol = get_br_symbol(symbol, exchange)

    try:
        quote_data = get_api_response(f"/v2/stocks/{br_symbol}/quotes/latest", auth)
        quote = quote_data.get("quote", {})

        return {
            "bid": [{"price": quote.get("bp", 0), "quantity": quote.get("bs", 0)}],
            "ask": [{"price": quote.get("ap", 0), "quantity": quote.get("as", 0)}],
        }
    except Exception as e:
        logger.error(f"Error getting depth for {br_symbol}: {e}")
        return {"bid": [], "ask": []}


def get_history(symbol, exchange, auth, interval="day", start_date=None, end_date=None):
    """
    Get historical OHLCV data.

    Alpaca intervals: 1Min, 5Min, 15Min, 30Min, 1Hour, 1Day, 1Week, 1Month
    """
    br_symbol = get_br_symbol(symbol, exchange)

    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")
    if not start_date:
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")

    # Map interval
    interval_map = {
        "1m": "1Min", "3m": "5Min", "5m": "5Min",
        "10m": "15Min", "15m": "15Min", "30m": "30Min",
        "1h": "1Hour", "1d": "1Day", "1w": "1Week", "1M": "1Month",
        "day": "1Day", "week": "1Week", "month": "1Month",
    }
    alpaca_interval = interval_map.get(interval, "1Day")

    try:
        endpoint = (
            f"/v2/stocks/{br_symbol}/bars?"
            f"timeframe={alpaca_interval}"
            f"&start={start_date}&end={end_date}"
            f"&limit=10000&adjustment=raw"
        )
        data = get_api_response(endpoint, auth)

        bars = data.get("bars", [])
        if not bars:
            return pd.DataFrame()

        df = pd.DataFrame(bars)
        rename_map = {
            "t": "datetime", "o": "open", "h": "high",
            "l": "low", "c": "close", "v": "volume",
        }
        df = df.rename(columns=rename_map)
        return df

    except Exception as e:
        logger.error(f"Error getting history for {br_symbol}: {e}")
        return pd.DataFrame()


def get_option_chain(symbol, exchange, auth, expiration=None):
    """Get option chain for a symbol using Alpaca Options API."""
    br_symbol = get_br_symbol(symbol, exchange)

    try:
        # Get option contracts
        params = f"underlying_symbols={br_symbol}&status=active"
        if expiration:
            params += f"&expiration_date={expiration}"
        else:
            params += "&expiration_date_gte=" + datetime.now().strftime("%Y-%m-%d")
            params += "&limit=1000"

        # Use trading API for options contracts
        api_key, api_secret, _, url_flag = _parse_auth(auth)
        base_url = "https://paper-api.alpaca.markets" if url_flag == "paper" else "https://api.alpaca.markets"

        headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
            "Accept": "application/json",
        }

        client = get_httpx_client()
        response = client.get(f"{base_url}/v2/options/contracts?{params}", headers=headers)

        if response.status_code != 200:
            logger.error(f"Option chain error: {response.text}")
            return {"calls": [], "puts": []}

        data = response.json()
        contracts = data.get("option_contracts", [])

        calls = []
        puts = []

        for contract in contracts:
            option_data = {
                "symbol": contract.get("symbol", ""),
                "strike": float(contract.get("strike_price", 0)),
                "expiry": contract.get("expiration_date", ""),
                "ltp": float(contract.get("close_price", 0)),
                "bid": 0,
                "ask": 0,
                "volume": 0,
                "oi": int(contract.get("open_interest", 0) or 0),
                "iv": 0,
                "delta": 0, "gamma": 0, "theta": 0, "vega": 0,
            }

            if contract.get("type") == "call":
                calls.append(option_data)
            else:
                puts.append(option_data)

        return {"calls": calls, "puts": puts}

    except Exception as e:
        logger.error(f"Error getting option chain for {br_symbol}: {e}")
        return {"calls": [], "puts": []}


def get_option_expirations(symbol, exchange, auth):
    """Get available option expiration dates."""
    chain = get_option_chain(symbol, exchange, auth)
    expirations = set()

    for opt in chain.get("calls", []) + chain.get("puts", []):
        exp = opt.get("expiry", "")
        if exp:
            expirations.add(exp)

    return sorted(list(expirations))
