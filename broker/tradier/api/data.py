import json
import os
from datetime import datetime, timedelta

import pandas as pd

from broker.tradier.api.order_api import _parse_auth, _get_headers, BASE_URL
from broker.tradier.database.master_contract_db import SymToken, db_session
from database.token_db import get_br_symbol, get_oa_symbol
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)


class TradierPermissionError(Exception):
    """Custom exception for Tradier API permission errors"""
    pass


class TradierAPIError(Exception):
    """Custom exception for other Tradier API errors"""
    pass


def get_api_response(endpoint, auth, method="GET", payload=None):
    """
    Make an API request to Tradier's API.
    """
    access_token, _ = _parse_auth(auth)
    client = get_httpx_client()

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    url = f"{BASE_URL}{endpoint}"

    try:
        if method.upper() == "GET":
            response = client.get(url, headers=headers)
        elif method.upper() == "POST":
            headers["Content-Type"] = "application/json"
            response = client.post(url, headers=headers, json=payload)
        else:
            raise TradierAPIError(f"Unsupported HTTP method: {method}")

        if response.status_code == 401:
            raise TradierPermissionError("Authentication failed or token expired")

        if response.status_code == 403:
            error_data = response.json() if response.text else {}
            raise TradierPermissionError(
                f"Permission denied: {error_data.get('fault', {}).get('faultstring', 'Access denied')}"
            )

        response.raise_for_status()
        return response.json()

    except (TradierPermissionError, TradierAPIError):
        raise
    except Exception as e:
        logger.exception(f"Tradier API error: {e}")
        raise TradierAPIError(f"API request failed: {str(e)}")


def get_quotes(symbol, exchange, auth):
    """
    Get real-time quote for a symbol.
    
    Returns dict with: ltp, open, high, low, close, volume, prev_close
    """
    br_symbol = get_br_symbol(symbol, exchange)
    
    data = get_api_response(f"/markets/quotes?symbols={br_symbol}&greeks=false", auth)
    
    quotes = data.get("quotes", {})
    quote = quotes.get("quote", {})
    
    if isinstance(quote, list):
        quote = quote[0] if quote else {}
    
    return {
        "ltp": quote.get("last", 0),
        "open": quote.get("open", 0),
        "high": quote.get("high", 0),
        "low": quote.get("low", 0),
        "close": quote.get("close", 0),  # Previous close
        "volume": quote.get("volume", 0),
        "prev_close": quote.get("prevclose", 0),
        "bid": quote.get("bid", 0),
        "ask": quote.get("ask", 0),
        "change": quote.get("change", 0),
        "change_percent": quote.get("change_percentage", 0),
    }


def get_ltp(symbol, exchange, auth):
    """Get Last Traded Price for a symbol."""
    quote = get_quotes(symbol, exchange, auth)
    return quote.get("ltp", 0)


def get_market_depth(symbol, exchange, auth):
    """
    Get market depth / Level 2 data.
    Tradier provides basic bid/ask. Full depth requires streaming.
    """
    br_symbol = get_br_symbol(symbol, exchange)
    data = get_api_response(f"/markets/quotes?symbols={br_symbol}", auth)
    
    quotes = data.get("quotes", {})
    quote = quotes.get("quote", {})
    
    if isinstance(quote, list):
        quote = quote[0] if quote else {}
    
    return {
        "bid": [{"price": quote.get("bid", 0), "quantity": quote.get("bidsize", 0)}],
        "ask": [{"price": quote.get("ask", 0), "quantity": quote.get("asksize", 0)}],
    }


def get_history(symbol, exchange, auth, interval="day", start_date=None, end_date=None):
    """
    Get historical OHLCV data for a symbol.
    
    Args:
        symbol: OpenAlgo symbol
        exchange: Exchange code
        auth: Auth token
        interval: 'daily', 'weekly', 'monthly', or minute intervals ('1min', '5min', '15min')
        start_date: Start date string (YYYY-MM-DD)
        end_date: End date string (YYYY-MM-DD)
    
    Returns:
        DataFrame with OHLCV data
    """
    br_symbol = get_br_symbol(symbol, exchange)
    
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")
    if not start_date:
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    
    # Map interval
    interval_map = {
        "1m": "1min",
        "3m": "5min",  # Tradier doesn't support 3min, use 5min
        "5m": "5min",
        "10m": "15min",
        "15m": "15min",
        "30m": "15min",
        "1h": "daily",
        "1d": "daily",
        "1w": "weekly",
        "1M": "monthly",
        "day": "daily",
        "week": "weekly",
        "month": "monthly",
    }
    
    tradier_interval = interval_map.get(interval, "daily")
    
    # Use timesales for intraday, history for daily+
    if tradier_interval in ("1min", "5min", "15min"):
        endpoint = (
            f"/markets/timesales?symbol={br_symbol}"
            f"&interval={tradier_interval}"
            f"&start={start_date}&end={end_date}"
        )
        data = get_api_response(endpoint, auth)
        
        series = data.get("series", {})
        if not series:
            return pd.DataFrame()
        
        bars = series.get("data", [])
        if isinstance(bars, dict):
            bars = [bars]
        
        df = pd.DataFrame(bars)
        if not df.empty:
            df = df.rename(columns={
                "timestamp": "datetime",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
            })
    else:
        endpoint = (
            f"/markets/history?symbol={br_symbol}"
            f"&interval={tradier_interval}"
            f"&start={start_date}&end={end_date}"
        )
        data = get_api_response(endpoint, auth)
        
        history = data.get("history", {})
        if not history:
            return pd.DataFrame()
        
        bars = history.get("day", [])
        if isinstance(bars, dict):
            bars = [bars]
        
        df = pd.DataFrame(bars)
        if not df.empty:
            df = df.rename(columns={"date": "datetime"})
    
    return df


def get_option_chain(symbol, exchange, auth, expiration=None):
    """
    Get option chain for a symbol.
    
    Args:
        symbol: Underlying symbol
        exchange: Exchange
        auth: Auth token
        expiration: Expiration date (YYYY-MM-DD), if None returns nearest
    
    Returns:
        dict with calls and puts lists
    """
    br_symbol = get_br_symbol(symbol, exchange)
    
    # First get expirations if not provided
    if not expiration:
        exp_data = get_api_response(
            f"/markets/options/expirations?symbol={br_symbol}", auth
        )
        expirations = exp_data.get("expirations", {})
        date_list = expirations.get("date", [])
        if isinstance(date_list, str):
            date_list = [date_list]
        if date_list:
            expiration = date_list[0]  # Nearest expiration
        else:
            return {"calls": [], "puts": []}
    
    # Get option chain
    data = get_api_response(
        f"/markets/options/chains?symbol={br_symbol}&expiration={expiration}&greeks=true",
        auth,
    )
    
    options = data.get("options", {})
    option_list = options.get("option", [])
    if isinstance(option_list, dict):
        option_list = [option_list]
    
    calls = []
    puts = []
    
    for opt in option_list:
        option_data = {
            "symbol": opt.get("symbol", ""),
            "strike": opt.get("strike", 0),
            "expiry": opt.get("expiration_date", ""),
            "ltp": opt.get("last", 0),
            "bid": opt.get("bid", 0),
            "ask": opt.get("ask", 0),
            "volume": opt.get("volume", 0),
            "oi": opt.get("open_interest", 0),
            "iv": opt.get("greeks", {}).get("mid_iv", 0) if opt.get("greeks") else 0,
            "delta": opt.get("greeks", {}).get("delta", 0) if opt.get("greeks") else 0,
            "gamma": opt.get("greeks", {}).get("gamma", 0) if opt.get("greeks") else 0,
            "theta": opt.get("greeks", {}).get("theta", 0) if opt.get("greeks") else 0,
            "vega": opt.get("greeks", {}).get("vega", 0) if opt.get("greeks") else 0,
        }
        
        if opt.get("option_type") == "call":
            calls.append(option_data)
        else:
            puts.append(option_data)
    
    return {"calls": calls, "puts": puts}


def get_option_expirations(symbol, exchange, auth):
    """Get available option expiration dates for a symbol."""
    br_symbol = get_br_symbol(symbol, exchange)
    data = get_api_response(
        f"/markets/options/expirations?symbol={br_symbol}", auth
    )
    expirations = data.get("expirations", {})
    dates = expirations.get("date", [])
    if isinstance(dates, str):
        dates = [dates]
    return dates
