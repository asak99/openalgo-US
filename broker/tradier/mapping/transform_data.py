# Mapping OpenAlgo API Request https://openalgo.in/docs
# Mapping Tradier Brokerage API https://documentation.tradier.com/

from database.token_db import get_br_symbol
from utils.logging import get_logger

logger = get_logger(__name__)


def _parse_auth(auth):
    """Parse combined auth token into (access_token, account_id)."""
    if ":" in auth:
        parts = auth.split(":", 1)
        return parts[0], parts[1]
    return auth, ""


def transform_data(data):
    """
    Transforms OpenAlgo API request to Tradier order format.

    OpenAlgo -> Tradier mapping:
    - symbol -> symbol (for equities) or full OCC symbol (for options)
    - action -> side (buy/sell/buy_to_open/sell_to_close etc.)
    - pricetype -> type (market/limit/stop/stop_limit)
    - product -> duration (day/gtc/pre/post)
    - quantity -> quantity
    - price -> price (for limit orders)
    - trigger_price -> stop (for stop orders)
    """
    symbol = get_br_symbol(data["symbol"], data["exchange"])
    exchange = data.get("exchange", "")

    # Determine order class: equity or option
    is_option = exchange in ("US_OPTIONS",) or _is_option_symbol(symbol)

    # Map action
    action = data["action"].upper()
    if is_option:
        side = map_option_side(action, data)
    else:
        side = action.lower()  # buy or sell

    transformed = {
        "class": "option" if is_option else "equity",
        "symbol": symbol,
        "side": side,
        "quantity": str(data["quantity"]),
        "type": map_order_type(data.get("pricetype", "MARKET")),
        "duration": map_duration(data.get("product", "MIS")),
        "price": str(data.get("price", "0")),
        "stop": str(data.get("trigger_price", "0")),
        "tag": "openalgo",
    }

    # For option orders, we need the OCC option symbol
    if is_option:
        transformed["option_symbol"] = symbol

    # Remove price/stop if not needed
    if transformed["type"] == "market":
        transformed.pop("price", None)
        transformed.pop("stop", None)
    elif transformed["type"] == "limit":
        transformed.pop("stop", None)
    elif transformed["type"] == "stop":
        transformed.pop("price", None)
        transformed["stop"] = str(data.get("trigger_price", data.get("price", "0")))

    return transformed


def transform_modify_order_data(data):
    """Transform modify order data from OpenAlgo to Tradier format."""
    return {
        "type": map_order_type(data.get("pricetype", "MARKET")),
        "duration": "day",
        "price": str(data.get("price", "0")),
        "stop": str(data.get("trigger_price", "0")),
        "quantity": str(data.get("quantity", "0")),
    }


def map_order_type(pricetype):
    """Maps OpenAlgo pricetype to Tradier order type."""
    mapping = {
        "MARKET": "market",
        "LIMIT": "limit",
        "SL": "stop_limit",
        "SL-M": "stop",
    }
    return mapping.get(pricetype, "market")


def map_duration(product):
    """
    Maps OpenAlgo product type to Tradier duration.

    For US markets:
    - MIS (Intraday) -> day
    - CNC (Delivery/Cash) -> gtc
    - NRML (Normal) -> day
    """
    mapping = {
        "MIS": "day",
        "CNC": "gtc",
        "NRML": "day",
    }
    return mapping.get(product, "day")


def map_product_type(product):
    """Maps OpenAlgo product type for US markets."""
    mapping = {
        "CNC": "CNC",
        "NRML": "NRML",
        "MIS": "MIS",
    }
    return mapping.get(product, "MIS")


def reverse_map_product_type(exchange, product):
    """Reverse maps Tradier product/duration to OpenAlgo product type."""
    # Tradier uses duration, map it back
    duration_mapping = {
        "day": "MIS",
        "gtc": "CNC",
        "pre": "MIS",
        "post": "MIS",
    }
    return duration_mapping.get(product, "MIS")


def map_option_side(action, data):
    """
    Map OpenAlgo BUY/SELL to Tradier option sides.
    Uses position context to determine open/close.
    """
    # Default to open for new positions
    position_size = int(data.get("position_size", "0")) if data.get("position_size") else None

    if action == "BUY":
        return "buy_to_open"
    elif action == "SELL":
        return "sell_to_close"
    return action.lower()


def _is_option_symbol(symbol):
    """Check if a symbol looks like an OCC option symbol (e.g., AAPL240119C00100000)."""
    if not symbol or len(symbol) < 10:
        return False
    # OCC format: ROOT + YYMMDD + C/P + STRIKE (8 digits)
    try:
        # Check if last 15 chars match option pattern
        suffix = symbol[-15:]
        date_part = suffix[:6]
        cp = suffix[6]
        strike = suffix[7:]
        return date_part.isdigit() and cp in ("C", "P") and strike.isdigit()
    except (IndexError, ValueError):
        return False
