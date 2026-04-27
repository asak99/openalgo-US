import json

from database.token_db import get_oa_symbol, get_symbol
from utils.logging import get_logger

logger = get_logger(__name__)


def map_order_data(order_data):
    """
    Process Tradier order data into OpenAlgo format.

    Tradier returns orders in:
    {"orders": {"order": [...]}}  or {"orders": "null"}
    """
    orders = order_data.get("orders", {})
    if not orders or orders == "null":
        logger.info("No order data available.")
        return []

    order_list = orders.get("order", [])
    if isinstance(order_list, dict):
        order_list = [order_list]

    for order in order_list:
        symbol = order.get("symbol", "")
        # For US stocks, symbol mapping is typically 1:1
        # but we still go through the mapping layer for consistency
        if symbol:
            mapped = get_oa_symbol(brsymbol=symbol, exchange=_infer_exchange(order))
            if mapped:
                order["symbol"] = mapped

    return order_list


def calculate_order_statistics(order_data):
    """Calculate order statistics from order data."""
    total_buy_orders = total_sell_orders = 0
    total_completed_orders = total_open_orders = total_rejected_orders = 0

    if order_data:
        for order in order_data:
            side = order.get("side", "").lower()
            if side in ("buy", "buy_to_open", "buy_to_close"):
                total_buy_orders += 1
            elif side in ("sell", "sell_to_open", "sell_to_close"):
                total_sell_orders += 1

            status = order.get("status", "").lower()
            if status == "filled":
                total_completed_orders += 1
            elif status in ("pending", "open", "partially_filled"):
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
    """Transform Tradier order data to OpenAlgo standard format."""
    if isinstance(orders, dict):
        orders = [orders]

    transformed_orders = []

    for order in orders:
        if not isinstance(order, dict):
            logger.warning(f"Expected a dict, found {type(order)}. Skipping.")
            continue

        # Map Tradier status to OpenAlgo status
        status = order.get("status", "").lower()
        status_map = {
            "filled": "complete",
            "rejected": "rejected",
            "pending": "open",
            "open": "open",
            "partially_filled": "open",
            "expired": "cancelled",
            "canceled": "cancelled",
        }
        order_status = status_map.get(status, status)

        # Map side to action
        side = order.get("side", "").lower()
        action_map = {
            "buy": "BUY",
            "sell": "SELL",
            "buy_to_open": "BUY",
            "buy_to_close": "BUY",
            "sell_to_open": "SELL",
            "sell_to_close": "SELL",
        }
        action = action_map.get(side, side.upper())

        # Map order type to pricetype
        order_type = order.get("type", "market").lower()
        pricetype_map = {
            "market": "MARKET",
            "limit": "LIMIT",
            "stop": "SL-M",
            "stop_limit": "SL",
        }
        pricetype = pricetype_map.get(order_type, "MARKET")

        # Map duration to product
        duration = order.get("duration", "day").lower()
        product_map = {
            "day": "MIS",
            "gtc": "CNC",
            "pre": "MIS",
            "post": "MIS",
        }
        product = product_map.get(duration, "MIS")

        transformed_order = {
            "symbol": order.get("symbol", ""),
            "exchange": _infer_exchange(order),
            "action": action,
            "quantity": order.get("quantity", 0),
            "price": order.get("price", 0.0) or 0.0,
            "trigger_price": order.get("stop_price", 0.0) or 0.0,
            "pricetype": pricetype,
            "product": product,
            "orderid": str(order.get("id", "")),
            "order_status": order_status,
            "timestamp": order.get("create_date", ""),
        }

        transformed_orders.append(transformed_order)

    return transformed_orders


def map_trade_data(trade_data):
    """Map trade data - Tradier uses order data for trades."""
    return map_order_data(trade_data)


def transform_tradebook_data(tradebook_data):
    """Transform tradebook data to OpenAlgo format."""
    transformed_data = []
    for trade in tradebook_data:
        # Filter only filled orders for tradebook
        if trade.get("status", "").lower() != "filled":
            continue

        side = trade.get("side", "").lower()
        action_map = {
            "buy": "BUY",
            "sell": "SELL",
            "buy_to_open": "BUY",
            "buy_to_close": "BUY",
            "sell_to_open": "SELL",
            "sell_to_close": "SELL",
        }

        avg_price = float(trade.get("avg_fill_price", 0) or 0)
        quantity = int(trade.get("quantity", 0) or 0)

        transformed_trade = {
            "symbol": trade.get("symbol", ""),
            "exchange": _infer_exchange(trade),
            "product": "MIS" if trade.get("duration") == "day" else "CNC",
            "action": action_map.get(side, side.upper()),
            "quantity": quantity,
            "average_price": avg_price,
            "trade_value": quantity * avg_price,
            "orderid": str(trade.get("id", "")),
            "timestamp": trade.get("create_date", ""),
        }
        transformed_data.append(transformed_trade)

    return transformed_data


def map_position_data(position_data):
    """Process Tradier position data."""
    positions = position_data.get("positions", {})
    if not positions or positions == "null":
        logger.info("No position data available.")
        return []

    position_list = positions.get("position", [])
    if isinstance(position_list, dict):
        position_list = [position_list]

    for position in position_list:
        symbol = position.get("symbol", "")
        if symbol:
            mapped = get_oa_symbol(brsymbol=symbol, exchange=_infer_exchange_from_symbol(symbol))
            if mapped:
                position["symbol"] = mapped

    return position_list


def transform_positions_data(positions_data):
    """Transform position data to OpenAlgo format."""
    transformed_data = []

    for position in positions_data:
        quantity = int(position.get("quantity", 0))
        cost_basis = float(position.get("cost_basis", 0))
        avg_price = cost_basis / abs(quantity) if quantity != 0 else 0

        # Calculate PnL if we have current price info
        pnl = 0
        if "current_price" in position:
            current_price = float(position.get("current_price", 0))
            pnl = (current_price - avg_price) * quantity

        average_price_formatted = f"{avg_price:.2f}"

        transformed_position = {
            "symbol": position.get("symbol", ""),
            "exchange": _infer_exchange_from_symbol(position.get("symbol", "")),
            "product": "CNC",  # US positions are typically cash/margin
            "quantity": str(quantity),
            "pnl": round(pnl, 2),
            "average_price": average_price_formatted,
            "ltp": round(float(position.get("current_price", 0)), 2),
        }
        transformed_data.append(transformed_position)

    return transformed_data


def transform_holdings_data(holdings_data):
    """Transform holdings data to OpenAlgo format."""
    transformed_data = []
    for holding in holdings_data:
        quantity = int(holding.get("quantity", 0))
        cost_basis = float(holding.get("cost_basis", 0))
        avg_price = cost_basis / abs(quantity) if quantity != 0 else 0
        current_price = float(holding.get("current_price", avg_price))

        pnl = (current_price - avg_price) * quantity if quantity != 0 else 0
        pnlpercent = ((current_price - avg_price) / avg_price * 100) if avg_price != 0 else 0

        transformed_position = {
            "symbol": holding.get("symbol", ""),
            "exchange": _infer_exchange_from_symbol(holding.get("symbol", "")),
            "quantity": quantity,
            "product": "CNC",
            "average_price": avg_price,
            "pnl": round(pnl, 2),
            "pnlpercent": round(pnlpercent, 2),
        }
        transformed_data.append(transformed_position)

    return transformed_data


def map_portfolio_data(portfolio_data):
    """Process portfolio data from Tradier."""
    return map_position_data(portfolio_data)


def calculate_portfolio_statistics(holdings_data):
    """Calculate portfolio statistics."""
    totalholdingvalue = 0
    totalinvvalue = 0
    totalprofitandloss = 0

    for item in holdings_data:
        qty = int(item.get("quantity", 0))
        avg = float(item.get("cost_basis", 0)) / abs(qty) if qty != 0 else 0
        current = float(item.get("current_price", avg))

        totalholdingvalue += current * abs(qty)
        totalinvvalue += avg * abs(qty)
        totalprofitandloss += (current - avg) * qty

    totalpnlpercentage = (totalprofitandloss / totalinvvalue * 100) if totalinvvalue else 0

    return {
        "totalholdingvalue": totalholdingvalue,
        "totalinvvalue": totalinvvalue,
        "totalprofitandloss": totalprofitandloss,
        "totalpnlpercentage": totalpnlpercentage,
    }


def _infer_exchange(order):
    """Infer exchange from order data."""
    order_class = order.get("class", "equity")
    if order_class == "option":
        return "US_OPTIONS"
    # Default to NYSE for equities
    return "NYSE"


def _infer_exchange_from_symbol(symbol):
    """Infer exchange from symbol format."""
    if not symbol:
        return "NYSE"
    # OCC option symbols are typically 15+ chars
    if len(symbol) > 10 and any(c.isdigit() for c in symbol[-8:]):
        return "US_OPTIONS"
    return "NYSE"
