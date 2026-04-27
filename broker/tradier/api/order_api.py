import json
import os
import threading
import time
import urllib.parse

from broker.tradier.mapping.transform_data import (
    map_product_type,
    reverse_map_product_type,
    transform_data,
    transform_modify_order_data,
    _parse_auth,
)
from database.auth_db import get_auth_token
from database.token_db import get_br_symbol, get_oa_symbol
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

BASE_URL = "https://api.tradier.com/v1"


def _get_headers(auth):
    """Build Tradier API headers from combined auth token."""
    access_token, _ = _parse_auth(auth)
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }


def _get_account_id(auth):
    """Extract account_id from combined auth token."""
    _, account_id = _parse_auth(auth)
    return account_id


def get_api_response(endpoint, auth, method="GET", payload=None):
    """
    Make an API request to Tradier's API using shared httpx client.

    Args:
        endpoint (str): API endpoint path
        auth (str): Combined auth token (access_token:account_id)
        method (str): HTTP method
        payload (dict/str, optional): Request payload

    Returns:
        dict: API response data
    """
    client = get_httpx_client()
    headers = _get_headers(auth)
    url = f"{BASE_URL}{endpoint}"

    try:
        if method.upper() == "GET":
            response = client.get(url, headers=headers)
        elif method.upper() == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            if isinstance(payload, dict):
                payload = urllib.parse.urlencode(payload)
            response = client.post(url, headers=headers, content=payload)
        elif method.upper() == "PUT":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            if isinstance(payload, dict):
                payload = urllib.parse.urlencode(payload)
            response = client.put(url, headers=headers, content=payload)
        elif method.upper() == "DELETE":
            response = client.delete(url, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        response.raise_for_status()
        return response.json()

    except Exception as e:
        error_msg = str(e)
        try:
            if hasattr(e, "response") and e.response is not None:
                error_detail = e.response.json()
                error_msg = error_detail.get("fault", {}).get("faultstring", error_msg)
        except Exception:
            pass
        logger.exception(f"Tradier API request failed: {error_msg}")
        raise


def get_order_book(auth):
    account_id = _get_account_id(auth)
    return get_api_response(f"/accounts/{account_id}/orders", auth)


def get_trade_book(auth):
    """Tradier doesn't have a separate trade book; use order book with filled status."""
    account_id = _get_account_id(auth)
    return get_api_response(f"/accounts/{account_id}/orders", auth)


def get_positions(auth):
    account_id = _get_account_id(auth)
    return get_api_response(f"/accounts/{account_id}/positions", auth)


def get_holdings(auth):
    """For US markets, holdings are the same as positions (no separate T+1 settlement view)."""
    return get_positions(auth)


# --- Per-Symbol Smart Order Lock ---
_symbol_locks = {}
_symbol_locks_lock = threading.Lock()

# --- Position Book Cache ---
_position_cache = {}
_position_cache_lock = threading.Lock()
_POSITION_CACHE_TTL = 1.0


def _get_symbol_lock(symbol, exchange, product):
    key = f"{symbol}:{exchange}:{product}"
    with _symbol_locks_lock:
        if key not in _symbol_locks:
            _symbol_locks[key] = threading.Lock()
        return _symbol_locks[key]


def _get_cached_positions(auth):
    with _position_cache_lock:
        now = time.monotonic()
        cached = _position_cache.get(auth)
        if cached and (now - cached["timestamp"]) < _POSITION_CACHE_TTL:
            logger.info("Position book served from cache")
            return cached["data"]

    positions_data = get_positions(auth)

    with _position_cache_lock:
        _position_cache[auth] = {"data": positions_data, "timestamp": time.monotonic()}

    return positions_data


def _invalidate_position_cache(auth):
    with _position_cache_lock:
        _position_cache.pop(auth, None)


def get_open_position(tradingsymbol, exchange, product, auth):
    """Get current open position quantity for a symbol."""
    tradingsymbol = get_br_symbol(tradingsymbol, exchange)
    positions_data = _get_cached_positions(auth)
    net_qty = "0"

    if positions_data:
        positions = positions_data.get("positions", {})
        if positions and positions != "null":
            position_list = positions.get("position", [])
            # Handle single position (Tradier returns dict instead of list)
            if isinstance(position_list, dict):
                position_list = [position_list]

            for position in position_list:
                if position.get("symbol") == tradingsymbol:
                    net_qty = str(position.get("quantity", 0))
                    logger.info(f"Net Quantity {net_qty}")
                    break

    return net_qty


def place_order_api(data, auth):
    """Place an order via Tradier API."""
    account_id = _get_account_id(auth)
    newdata = transform_data(data)

    # Build the order payload
    payload = {
        "class": newdata["class"],
        "symbol": newdata["symbol"],
        "side": newdata["side"],
        "quantity": newdata["quantity"],
        "type": newdata["type"],
        "duration": newdata["duration"],
        "tag": newdata.get("tag", "openalgo"),
    }

    # Add price fields based on order type
    if newdata["type"] == "limit":
        payload["price"] = newdata.get("price", "0")
    elif newdata["type"] == "stop":
        payload["stop"] = newdata.get("stop", "0")
    elif newdata["type"] == "stop_limit":
        payload["price"] = newdata.get("price", "0")
        payload["stop"] = newdata.get("stop", "0")

    # For option orders, use option_symbol
    if newdata["class"] == "option":
        payload["option_symbol"] = newdata.get("option_symbol", newdata["symbol"])

    logger.info(f"Tradier order payload: {payload}")

    client = get_httpx_client()
    headers = _get_headers(auth)
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    url = f"{BASE_URL}/accounts/{account_id}/orders"
    encoded_payload = urllib.parse.urlencode(payload)

    response = client.post(url, headers=headers, content=encoded_payload)

    logger.info(f"Tradier raw response: status={response.status_code}, body={response.text}")

    response_data = response.json()
    logger.info(f"Response from place_order_api: {response_data}")

    # Parse response
    order_response = response_data.get("order", {})
    if order_response.get("status") == "ok" or order_response.get("id"):
        orderid = str(order_response.get("id", ""))
    else:
        orderid = None
        # Check for errors
        errors = response_data.get("errors", {})
        if errors:
            error_list = errors.get("error", [])
            if isinstance(error_list, str):
                error_list = [error_list]
            logger.error(f"Tradier order errors: {error_list}")

    response.status = response.status_code
    return response, response_data, orderid


def place_smartorder_api(data, auth):
    """Place a smart order that manages position sizing."""
    AUTH_TOKEN = auth

    res = None
    response_data = {"status": "error", "message": "No action required or invalid parameters"}
    orderid = None

    try:
        symbol = data.get("symbol")
        exchange = data.get("exchange")
        product = data.get("product")

        if not all([symbol, exchange, product]):
            logger.info("Missing required parameters in place_smartorder_api")
            return res, response_data, orderid

        symbol_lock = _get_symbol_lock(symbol, exchange, product)

        with symbol_lock:
            position_size = int(data.get("position_size", "0"))

            current_position = int(
                get_open_position(symbol, exchange, map_product_type(product), AUTH_TOKEN)
            )

            logger.info(f"position_size: {position_size}")
            logger.info(f"Open Position: {current_position}")

            action = None
            quantity = 0

            if position_size == 0 and current_position == 0:
                action = data.get("action", "BUY").upper()
                quantity = int(data.get("quantity", "0"))
            elif position_size == 0 and current_position > 0:
                action = "SELL"
                quantity = abs(current_position)
            elif position_size == 0 and current_position < 0:
                action = "BUY"
                quantity = abs(current_position)
            elif current_position == 0:
                action = "BUY" if position_size > 0 else "SELL"
                quantity = abs(position_size)
            else:
                if position_size > current_position:
                    action = "BUY"
                    quantity = position_size - current_position
                elif position_size < current_position:
                    action = "SELL"
                    quantity = current_position - position_size

            if action and quantity > 0:
                order_data = data.copy()
                order_data["action"] = action
                order_data["quantity"] = str(quantity)

                res, response, orderid = place_order_api(order_data, AUTH_TOKEN)

                _invalidate_position_cache(AUTH_TOKEN)

                return res, response, orderid
            else:
                logger.info("No action required or invalid quantity")
                response_data = {"status": "success", "message": "No action needed. Position already matched."}
                return res, response_data, orderid

    except Exception as e:
        error_msg = f"Error in place_smartorder_api: {e}"
        logger.exception(error_msg)
        response_data = {"status": "error", "message": error_msg}
        return res, response_data, orderid

    return res, response_data, orderid


def close_all_positions(current_api_key, auth):
    """Close all open positions."""
    AUTH_TOKEN = auth
    positions_response = get_positions(AUTH_TOKEN)

    positions = positions_response.get("positions", {})
    if not positions or positions == "null":
        return {"message": "No Open Positions Found"}, 200

    position_list = positions.get("position", [])
    if isinstance(position_list, dict):
        position_list = [position_list]

    if not position_list:
        return {"message": "No Open Positions Found"}, 200

    for position in position_list:
        qty = int(position.get("quantity", 0))
        if qty == 0:
            continue

        action = "SELL" if qty > 0 else "BUY"
        quantity = abs(qty)

        symbol = position.get("symbol", "")
        # Map back to OpenAlgo symbol
        # For US stocks, symbol is usually the same
        exchange = "NYSE"  # Default; will be resolved from token DB

        place_order_payload = {
            "apikey": current_api_key,
            "strategy": "Squareoff",
            "symbol": symbol,
            "action": action,
            "exchange": exchange,
            "pricetype": "MARKET",
            "product": "MIS",
            "quantity": str(quantity),
        }

        logger.info(f"Close position payload: {place_order_payload}")
        _, api_response, _ = place_order_api(place_order_payload, AUTH_TOKEN)
        logger.info(f"Close position response: {api_response}")

    return {"status": "success", "message": "All Open Positions SquaredOff"}, 200


def cancel_order(orderid, auth):
    """Cancel an existing order."""
    account_id = _get_account_id(auth)

    try:
        client = get_httpx_client()
        headers = _get_headers(auth)

        response = client.delete(
            f"{BASE_URL}/accounts/{account_id}/orders/{orderid}",
            headers=headers,
        )

        response.raise_for_status()
        data = response.json()
        logger.info(f"Cancel order response: {data}")

        order_resp = data.get("order", {})
        if order_resp.get("status") == "ok" or response.status_code == 200:
            return {"status": "success", "orderid": orderid}, 200
        else:
            return {
                "status": "error",
                "message": data.get("errors", {}).get("error", "Failed to cancel order"),
            }, response.status_code

    except Exception as e:
        error_msg = str(e)
        logger.exception(f"Error canceling order {orderid}: {error_msg}")
        return {"status": "error", "message": f"Failed to cancel order: {error_msg}"}, 500


def modify_order(data, auth):
    """Modify an existing order."""
    account_id = _get_account_id(auth)
    newdata = transform_modify_order_data(data)

    payload = {
        "type": newdata["type"],
        "duration": newdata.get("duration", "day"),
        "quantity": newdata["quantity"],
    }

    if newdata["type"] in ("limit", "stop_limit"):
        payload["price"] = newdata.get("price", "0")
    if newdata["type"] in ("stop", "stop_limit"):
        payload["stop"] = newdata.get("stop", "0")

    logger.info(f"Modify order payload: {payload}")

    client = get_httpx_client()
    headers = _get_headers(auth)
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    encoded_payload = urllib.parse.urlencode(payload)

    response = client.put(
        f"{BASE_URL}/accounts/{account_id}/orders/{data['orderid']}",
        headers=headers,
        content=encoded_payload,
    )

    response_data = response.json()
    logger.info(f"Modify order response: {response_data}")

    response.status = response.status_code

    order_resp = response_data.get("order", {})
    if order_resp.get("status") == "ok" or response.status_code == 200:
        return {"status": "success", "orderid": str(order_resp.get("id", data["orderid"]))}, 200
    else:
        return {
            "status": "error",
            "message": response_data.get("errors", {}).get("error", "Failed to modify order"),
        }, response.status_code


def cancel_all_orders_api(data, auth):
    """Cancel all open/pending orders."""
    AUTH_TOKEN = auth
    order_book_response = get_order_book(AUTH_TOKEN)

    orders = order_book_response.get("orders", {})
    if not orders or orders == "null":
        return [], []

    order_list = orders.get("order", [])
    if isinstance(order_list, dict):
        order_list = [order_list]

    # Filter orders that are open or pending
    orders_to_cancel = [
        order for order in order_list
        if order.get("status") in ("pending", "open", "partially_filled")
    ]

    logger.info(f"Orders to cancel: {len(orders_to_cancel)}")

    canceled_orders = []
    failed_cancellations = []

    for order in orders_to_cancel:
        orderid = str(order["id"])
        cancel_response, status_code = cancel_order(orderid, AUTH_TOKEN)
        if status_code == 200:
            canceled_orders.append(orderid)
        else:
            failed_cancellations.append(orderid)

    return canceled_orders, failed_cancellations
