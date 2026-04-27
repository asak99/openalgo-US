import json
import os
import threading
import time

from broker.alpaca.mapping.transform_data import (
    _parse_auth,
    map_product_type,
    reverse_map_product_type,
    transform_data,
    transform_modify_order_data,
)
from database.auth_db import get_auth_token
from database.token_db import get_br_symbol, get_oa_symbol
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

LIVE_BASE_URL = "https://api.alpaca.markets"
PAPER_BASE_URL = "https://paper-api.alpaca.markets"


def _get_base_url(auth):
    """Get the correct base URL based on auth token."""
    _, _, _, url_flag = _parse_auth(auth)
    return PAPER_BASE_URL if url_flag == "paper" else LIVE_BASE_URL


def _get_headers(auth):
    """Build Alpaca API headers."""
    api_key, api_secret, _, _ = _parse_auth(auth)
    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _get_account_id(auth):
    """Extract account_id from combined auth token."""
    _, _, account_id, _ = _parse_auth(auth)
    return account_id


def get_api_response(endpoint, auth, method="GET", payload=None):
    """Make an API request to Alpaca's API."""
    client = get_httpx_client()
    headers = _get_headers(auth)
    base_url = _get_base_url(auth)
    url = f"{base_url}{endpoint}"

    try:
        if method.upper() == "GET":
            response = client.get(url, headers=headers)
        elif method.upper() == "POST":
            response = client.post(url, headers=headers, json=payload)
        elif method.upper() == "PATCH":
            response = client.patch(url, headers=headers, json=payload)
        elif method.upper() == "DELETE":
            response = client.delete(url, headers=headers)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

        response.raise_for_status()
        if response.text:
            return response.json()
        return {}

    except Exception as e:
        error_msg = str(e)
        try:
            if hasattr(e, "response") and e.response is not None:
                error_detail = e.response.json()
                error_msg = error_detail.get("message", error_msg)
        except Exception:
            pass
        logger.exception(f"Alpaca API request failed: {error_msg}")
        raise


def get_order_book(auth):
    """Get all orders."""
    orders = get_api_response("/v2/orders?status=all&limit=500&nested=true", auth)
    # Wrap in expected format
    return {"orders": {"order": orders if isinstance(orders, list) else [orders] if orders else []}}


def get_trade_book(auth):
    """Get filled orders (trades)."""
    orders = get_api_response("/v2/orders?status=closed&limit=500", auth)
    return {"orders": {"order": orders if isinstance(orders, list) else [orders] if orders else []}}


def get_positions(auth):
    """Get all open positions."""
    positions = get_api_response("/v2/positions", auth)
    if isinstance(positions, list):
        return {"positions": {"position": positions}}
    return {"positions": {"position": []}}


def get_holdings(auth):
    """For US markets, holdings = positions."""
    return get_positions(auth)


# --- Per-Symbol Smart Order Lock ---
_symbol_locks = {}
_symbol_locks_lock = threading.Lock()
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

    positions = positions_data.get("positions", {})
    position_list = positions.get("position", [])

    if isinstance(position_list, dict):
        position_list = [position_list]

    for position in position_list:
        if position.get("symbol") == tradingsymbol:
            net_qty = str(position.get("qty", position.get("quantity", 0)))
            logger.info(f"Net Quantity {net_qty}")
            break

    return net_qty


def place_order_api(data, auth):
    """Place an order via Alpaca API."""
    newdata = transform_data(data)

    payload = {
        "symbol": newdata["symbol"],
        "qty": newdata["qty"],
        "side": newdata["side"],
        "type": newdata["type"],
        "time_in_force": newdata["time_in_force"],
    }

    # Add price fields
    if "limit_price" in newdata:
        payload["limit_price"] = newdata["limit_price"]
    if "stop_price" in newdata:
        payload["stop_price"] = newdata["stop_price"]

    logger.info(f"Alpaca order payload: {payload}")

    client = get_httpx_client()
    headers = _get_headers(auth)
    base_url = _get_base_url(auth)

    response = client.post(f"{base_url}/v2/orders", headers=headers, json=payload)

    logger.info(f"Alpaca raw response: status={response.status_code}, body={response.text}")

    response_data = response.json() if response.text else {}
    logger.info(f"Response from place_order_api: {response_data}")

    if response.status_code in (200, 201):
        orderid = response_data.get("id", response_data.get("client_order_id", ""))
    else:
        orderid = None
        error_msg = response_data.get("message", "Order placement failed")
        logger.error(f"Alpaca order error: {error_msg}")

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
            return res, response_data, orderid

        symbol_lock = _get_symbol_lock(symbol, exchange, product)

        with symbol_lock:
            position_size = int(data.get("position_size", "0"))
            current_position = int(
                get_open_position(symbol, exchange, map_product_type(product), AUTH_TOKEN)
            )

            logger.info(f"position_size: {position_size}, Open Position: {current_position}")

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

    try:
        # Alpaca has a dedicated endpoint for closing all positions
        client = get_httpx_client()
        headers = _get_headers(auth)
        base_url = _get_base_url(auth)

        response = client.delete(f"{base_url}/v2/positions", headers=headers)

        if response.status_code in (200, 207):
            return {"status": "success", "message": "All Open Positions SquaredOff"}, 200
        else:
            # Fall back to manual close
            positions_data = get_positions(AUTH_TOKEN)
            positions = positions_data.get("positions", {})
            position_list = positions.get("position", [])

            if isinstance(position_list, dict):
                position_list = [position_list]

            if not position_list:
                return {"message": "No Open Positions Found"}, 200

            for position in position_list:
                qty = int(position.get("qty", position.get("quantity", 0)))
                if qty == 0:
                    continue

                action = "SELL" if qty > 0 else "BUY"
                quantity = abs(qty)

                place_order_payload = {
                    "apikey": current_api_key,
                    "strategy": "Squareoff",
                    "symbol": position.get("symbol", ""),
                    "action": action,
                    "exchange": "NYSE",
                    "pricetype": "MARKET",
                    "product": "MIS",
                    "quantity": str(quantity),
                }

                place_order_api(place_order_payload, AUTH_TOKEN)

            return {"status": "success", "message": "All Open Positions SquaredOff"}, 200

    except Exception as e:
        logger.exception(f"Error closing all positions: {e}")
        return {"status": "error", "message": str(e)}, 500


def cancel_order(orderid, auth):
    """Cancel an existing order."""
    try:
        client = get_httpx_client()
        headers = _get_headers(auth)
        base_url = _get_base_url(auth)

        response = client.delete(f"{base_url}/v2/orders/{orderid}", headers=headers)

        if response.status_code in (200, 204):
            return {"status": "success", "orderid": orderid}, 200
        else:
            error_data = response.json() if response.text else {}
            return {
                "status": "error",
                "message": error_data.get("message", "Failed to cancel order"),
            }, response.status_code

    except Exception as e:
        logger.exception(f"Error canceling order {orderid}: {e}")
        return {"status": "error", "message": f"Failed to cancel order: {str(e)}"}, 500


def modify_order(data, auth):
    """Modify an existing order using PATCH."""
    newdata = transform_modify_order_data(data)

    payload = {}
    if newdata.get("qty"):
        payload["qty"] = newdata["qty"]
    if newdata.get("limit_price"):
        payload["limit_price"] = newdata["limit_price"]
    if newdata.get("stop_price"):
        payload["stop_price"] = newdata["stop_price"]
    if newdata.get("time_in_force"):
        payload["time_in_force"] = newdata["time_in_force"]

    logger.info(f"Modify order payload: {payload}")

    client = get_httpx_client()
    headers = _get_headers(auth)
    base_url = _get_base_url(auth)

    response = client.patch(
        f"{base_url}/v2/orders/{data['orderid']}",
        headers=headers,
        json=payload,
    )

    response_data = response.json() if response.text else {}
    logger.info(f"Modify order response: {response_data}")

    response.status = response.status_code

    if response.status_code == 200:
        return {"status": "success", "orderid": response_data.get("id", data["orderid"])}, 200
    else:
        return {
            "status": "error",
            "message": response_data.get("message", "Failed to modify order"),
        }, response.status_code


def cancel_all_orders_api(data, auth):
    """Cancel all open orders."""
    try:
        client = get_httpx_client()
        headers = _get_headers(auth)
        base_url = _get_base_url(auth)

        # Alpaca has a dedicated cancel all endpoint
        response = client.delete(f"{base_url}/v2/orders", headers=headers)

        if response.status_code in (200, 207):
            canceled = response.json() if response.text else []
            canceled_ids = [str(o.get("id", "")) for o in canceled] if isinstance(canceled, list) else []
            return canceled_ids, []
        else:
            return [], []

    except Exception as e:
        logger.exception(f"Error canceling all orders: {e}")
        return [], []
