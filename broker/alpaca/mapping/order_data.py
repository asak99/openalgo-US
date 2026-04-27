import json

from database.token_db import get_oa_symbol, get_symbol
from utils.logging import get_logger

logger = get_logger(__name__)


def map_order_data(order_data):
    """Process Alpaca order data into OpenAlgo format."""
    orders = order_data.get("orders", {})
    if not orders:
        logger.info("No order data available.")
        return []

    order_list = orders.get("order", [])
    if isinstance(order_list, dict):
        order_list = [order_list]

    for order in order_list:
        symbol = order.get("symbol", "")
        if symbol:
            exchange = _infer_exchange(order)
            mapped = get_oa_symbol(brsymbol=symbol, exchange=exchange)
            if mapped:
                order["symbol"] = mapped

    return order_list


def calculate_order_statistics(order_data):
    """Calculate order statistics."""
    total_buy_orders = total_sell_orders = 0
    total_completed_orders = total_open_orders = total_rejected_orders = 0

    if order_data:
        for order in order_data:
            side = order.get("side", "").lower()
            if side == "buy":
                total_buy_orders += 1
            elif side == "sell":
                total_sell_orders += 1

            status = order.get("status", "").lower()
            if status == "filled":
                total_completed_orders += 1
            elif status in ("new", "accepted", "pending_new", "partially_filled"):
                total_open_orders += 1
            elif status == "rejected":
                total_rejected_orders += 1

    return {
        "total_buy_orders": total_buy_orders,
        "total_sell_orders": total_sell_orders,
        "total_completed_orders": total_completed_orders,
        "total_open_orders": total_open_orders,
        "total_rejected_orders": total_rejected_orders,
    }


def transform_order_data(orders):
    """Transform Alpaca order data to OpenAlgo standard format."""
    if isinstance(orders, dict):
        orders = [orders]

    transformed_orders = []

    for order in orders:
        if not isinstance(order, dict):
            continue

        # Map status
        status = order.get("status", "").lower()
        status_map = {
            "filled": "complete",
            "rejected": "rejected",
            "new": "open",
            "accepted": "open",
            "pending_new": "open",
            "partially_filled": "open",
            "expired": "cancelled",
            "canceled": "cancelled",
            "cancelled": "cancelled",
            "done_for_day": "cancelled",
            "replaced": "cancelled",
        }
        order_status = status_map.get(status, status)

        # Map type to pricetype
        order_type = order.get("type", "market").lower()
        pricetype_map = {
            "market": "MARKET",
            "limit": "LIMIT",
            "stop": "SL-M",
            "stop_limit": "SL",
        }
        pricetype = pricetype_map.get(order_type, "MARKET")

        # Map time_in_force to product
        tif = order.get("time_in_force", "day").lower()
        product_map = {"day": "MIS", "gtc": "CNC", "ioc": "MIS", "fok": "MIS"}
        product = product_map.get(tif, "MIS")

        transformed_order = {
            "symbol": order.get("symbol", ""),
            "exchange": _infer_exchange(order),
            "action": order.get("side", "").upper(),
            "quantity": int(float(order.get("qty", order.get("quantity", 0)) or 0)),
            "price": float(order.get("limit_price", 0) or 0),
            "trigger_price": float(order.get("stop_price", 0) or 0),
            "pricetype": pricetype,
            "product": product,
            "orderid": order.get("id", order.get("client_order_id", "")),
            "order_status": order_status,
            "timestamp": order.get("created_at", order.get("submitted_at", "")),
        }

        transformed_orders.append(transformed_order)

    return transformed_orders


def map_trade_data(trade_data):
    return map_order_data(trade_data)


def transform_tradebook_data(tradebook_data):
    """Transform tradebook data."""
    transformed_data = []
    for trade in tradebook_data:
        if trade.get("status", "").lower() != "filled":
            continue

        avg_price = float(trade.get("filled_avg_price", 0) or 0)
        quantity = int(float(trade.get("filled_qty", trade.get("qty", 0)) or 0))

        transformed_trade = {
            "symbol": trade.get("symbol", ""),
            "exchange": _infer_exchange(trade),
            "product": "MIS" if trade.get("time_in_force") == "day" else "CNC",
            "action": trade.get("side", "").upper(),
            "quantity": quantity,
            "average_price": avg_price,
            "trade_value": quantity * avg_price,
            "orderid": trade.get("id", ""),
            "timestamp": trade.get("filled_at", trade.get("created_at", "")),
        }
        transformed_data.append(transformed_trade)

    return transformed_data


def map_position_data(position_data):
    """Process Alpaca position data."""
    positions = position_data.get("positions", {})
    if not positions:
        logger.info("No position data available.")
        return []

    position_list = positions.get("position", [])
    if isinstance(position_list, dict):
        position_list = [position_list]

    for position in position_list:
        symbol = position.get("symbol", "")
        if symbol:
            exchange = _infer_exchange_from_symbol(symbol)
            mapped = get_oa_symbol(brsymbol=symbol, exchange=exchange)
            if mapped:
                position["symbol"] = mapped

    return position_list


def transform_positions_data(positions_data):
    """Transform position data to OpenAlgo format."""
    transformed_data = []

    for position in positions_data:
        qty = int(float(position.get("qty", position.get("quantity", 0)) or 0))
        avg_price = float(position.get("avg_entry_price", position.get("average_price", 0)) or 0)
        current_price = float(position.get("current_price", 0) or 0)
        unrealized_pl = float(position.get("unrealized_pl", 0) or 0)

        transformed_position = {
            "symbol": position.get("symbol", ""),
            "exchange": _infer_exchange_from_symbol(position.get("symbol", "")),
            "product": "CNC",
            "quantity": str(qty),
            "pnl": round(unrealized_pl, 2),
            "average_price": f"{avg_price:.2f}",
            "ltp": round(current_price, 2),
        }
        transformed_data.append(transformed_position)

    return transformed_data


def transform_holdings_data(holdings_data):
    """Transform holdings data."""
    transformed_data = []
    for holding in holdings_data:
        qty = int(float(holding.get("qty", 0) or 0))
        avg_price = float(holding.get("avg_entry_price", 0) or 0)
        current_price = float(holding.get("current_price", avg_price) or avg_price)
        unrealized_pl = float(holding.get("unrealized_pl", 0) or 0)
        pnlpercent = float(holding.get("unrealized_plpc", 0) or 0) * 100

        transformed_position = {
            "symbol": holding.get("symbol", ""),
            "exchange": _infer_exchange_from_symbol(holding.get("symbol", "")),
            "quantity": qty,
            "product": "CNC",
            "average_price": avg_price,
            "pnl": round(unrealized_pl, 2),
            "pnlpercent": round(pnlpercent, 2),
        }
        transformed_data.append(transformed_position)

    return transformed_data


def map_portfolio_data(portfolio_data):
    return map_position_data(portfolio_data)


def calculate_portfolio_statistics(holdings_data):
    """Calculate portfolio statistics."""
    totalholdingvalue = 0
    totalinvvalue = 0
    totalprofitandloss = 0

    for item in holdings_data:
        qty = int(float(item.get("qty", 0) or 0))
        avg = float(item.get("avg_entry_price", 0) or 0)
        current = float(item.get("current_price", avg) or avg)

        totalholdingvalue += current * abs(qty)
        totalinvvalue += avg * abs(qty)
        totalprofitandloss += float(item.get("unrealized_pl", 0) or 0)

    totalpnlpercentage = (totalprofitandloss / totalinvvalue * 100) if totalinvvalue else 0

    return {
        "totalholdingvalue": totalholdingvalue,
        "totalinvvalue": totalinvvalue,
        "totalprofitandloss": totalprofitandloss,
        "totalpnlpercentage": totalpnlpercentage,
    }


def _infer_exchange(order):
    """Infer exchange from order data."""
    asset_class = order.get("asset_class", "us_equity")
    if asset_class == "us_option":
        return "US_OPTIONS"
    return "NYSE"


def _infer_exchange_from_symbol(symbol):
    """Infer exchange from symbol format."""
    if not symbol:
        return "NYSE"
    if len(symbol) > 10 and any(c.isdigit() for c in symbol[-8:]):
        return "US_OPTIONS"
    return "NYSE"
