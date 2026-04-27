# Mapping OpenAlgo API Request https://openalgo.in/docs
# Mapping Alpaca Trading API https://docs.alpaca.markets/docs/trading

from database.token_db import get_br_symbol
from utils.logging import get_logger

logger = get_logger(__name__)


def _parse_auth(auth):
    """Parse combined auth token into components."""
    parts = auth.split(":")
    if len(parts) >= 4:
        return parts[0], parts[1], parts[2], parts[3]
    elif len(parts) == 3:
        return parts[0], parts[1], parts[2], "paper"
    elif len(parts) == 2:
        return parts[0], parts[1], "", "paper"
    return auth, "", "", "paper"


def transform_data(data):
    """
    Transforms OpenAlgo API request to Alpaca order format.

    OpenAlgo -> Alpaca mapping:
    - symbol -> symbol
    - action -> side (buy/sell)
    - pricetype -> type (market/limit/stop/stop_limit)
    - product -> time_in_force (day/gtc/ioc/fok)
    - quantity -> qty
    - price -> limit_price
    - trigger_price -> stop_price
    """
    symbol = get_br_symbol(data["symbol"], data["exchange"])
    exchange = data.get("exchange", "")

    # Determine if this is an options order
    is_option = exchange == "US_OPTIONS" or _is_option_symbol(symbol)

    transformed = {
        "symbol": symbol,
        "qty": str(data["quantity"]),
        "side": data["action"].lower(),
        "type": map_order_type(data.get("pricetype", "MARKET")),
        "time_in_force": map_time_in_force(data.get("product", "MIS")),
    }

    # Add price fields based on order type
    pricetype = data.get("pricetype", "MARKET").upper()
    if pricetype == "LIMIT":
        transformed["limit_price"] = str(data.get("price", "0"))
    elif pricetype == "SL-M":
        transformed["stop_price"] = str(data.get("trigger_price", data.get("price", "0")))
    elif pricetype == "SL":
        transformed["limit_price"] = str(data.get("price", "0"))
        transformed["stop_price"] = str(data.get("trigger_price", "0"))

    return transformed


def transform_modify_order_data(data):
    """Transform modify order data from OpenAlgo to Alpaca format."""
    result = {
        "qty": str(data.get("quantity", "0")),
        "time_in_force": "day",
    }

    pricetype = data.get("pricetype", "MARKET").upper()
    if pricetype in ("LIMIT", "SL"):
        result["limit_price"] = str(data.get("price", "0"))
    if pricetype in ("SL", "SL-M"):
        result["stop_price"] = str(data.get("trigger_price", "0"))

    result["type"] = map_order_type(pricetype)
    return result


def map_order_type(pricetype):
    """Maps OpenAlgo pricetype to Alpaca order type."""
    mapping = {
        "MARKET": "market",
        "LIMIT": "limit",
        "SL": "stop_limit",
        "SL-M": "stop",
    }
    return mapping.get(pricetype, "market")


def map_time_in_force(product):
    """
    Maps OpenAlgo product type to Alpaca time_in_force.
    
    - MIS (Intraday) -> day
    - CNC (Cash) -> gtc
    - NRML (Normal) -> day
    """
    mapping = {
        "MIS": "day",
        "CNC": "gtc",
        "NRML": "day",
    }
    return mapping.get(product, "day")


def map_product_type(product):
    """Maps OpenAlgo product type."""
    mapping = {"CNC": "CNC", "NRML": "NRML", "MIS": "MIS"}
    return mapping.get(product, "MIS")


def reverse_map_product_type(exchange, time_in_force):
    """Reverse maps Alpaca time_in_force to OpenAlgo product type."""
    mapping = {
        "day": "MIS",
        "gtc": "CNC",
        "ioc": "MIS",
        "fok": "MIS",
    }
    return mapping.get(time_in_force, "MIS")


def _is_option_symbol(symbol):
    """Check if a symbol looks like an OCC option symbol."""
    if not symbol or len(symbol) < 10:
        return False
    try:
        suffix = symbol[-15:]
        date_part = suffix[:6]
        cp = suffix[6]
        strike = suffix[7:]
        return date_part.isdigit() and cp in ("C", "P") and strike.isdigit()
    except (IndexError, ValueError):
        return False
