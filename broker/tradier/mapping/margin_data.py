# Mapping OpenAlgo API Request https://openalgo.in/docs
# Mapping Tradier Margin Data

from database.token_db import get_br_symbol
from utils.logging import get_logger

logger = get_logger(__name__)


def transform_margin_positions(positions):
    """
    Transform OpenAlgo margin position format to Tradier format.

    Args:
        positions: List of positions in OpenAlgo format

    Returns:
        List of positions in Tradier order preview format
    """
    transformed_positions = []
    skipped_positions = []

    for position in positions:
        try:
            symbol = position["symbol"]
            exchange = position["exchange"]

            br_symbol = get_br_symbol(symbol, exchange)

            if not br_symbol or str(br_symbol).lower() == "none":
                logger.warning(f"Symbol not found for: {symbol} on exchange: {exchange}")
                skipped_positions.append(f"{symbol} ({exchange})")
                continue

            br_symbol_str = str(br_symbol).strip()
            if not br_symbol_str:
                skipped_positions.append(f"{symbol} ({exchange}) - invalid symbol")
                continue

            # Determine if this is an option
            is_option = exchange == "US_OPTIONS" or len(br_symbol_str) > 10

            action = position["action"].upper()

            transformed_position = {
                "class": "option" if is_option else "equity",
                "symbol": br_symbol_str if not is_option else br_symbol_str[:6].strip(),
                "side": _map_side(action, is_option),
                "quantity": int(position["quantity"]),
                "type": map_order_type(position.get("pricetype", "MARKET")),
                "price": float(position.get("price", 0)),
            }

            if is_option:
                transformed_position["option_symbol"] = br_symbol_str

            transformed_positions.append(transformed_position)
            logger.debug(f"Transformed position: {symbol} -> {br_symbol_str}")

        except Exception as e:
            logger.error(f"Error transforming position: {position}, Error: {e}")
            skipped_positions.append(f"{position.get('symbol', 'unknown')} - Error: {str(e)}")
            continue

    if skipped_positions:
        logger.warning(f"Skipped {len(skipped_positions)} positions: {', '.join(skipped_positions)}")

    if transformed_positions:
        logger.info(f"Transformed {len(transformed_positions)} positions for margin calculation")

    return transformed_positions


def _map_side(action, is_option=False):
    """Map action to Tradier side."""
    if is_option:
        return "buy_to_open" if action == "BUY" else "sell_to_close"
    return action.lower()


def map_product_type(product):
    """Maps OpenAlgo product type to Tradier duration."""
    mapping = {"CNC": "gtc", "NRML": "day", "MIS": "day"}
    return mapping.get(product, "day")


def map_order_type(pricetype):
    """Maps OpenAlgo price type to Tradier order type."""
    mapping = {"MARKET": "market", "LIMIT": "limit", "SL": "stop_limit", "SL-M": "stop"}
    return mapping.get(pricetype, "market")


def parse_margin_response(response_data):
    """
    Parse Tradier margin response to OpenAlgo standard format.

    Since Tradier uses order preview for margin calculation,
    we aggregate the preview results.
    """
    try:
        if not response_data or not isinstance(response_data, dict):
            return {"status": "error", "message": "Invalid response from broker"}

        if response_data.get("status") != "success":
            error_message = response_data.get("message", "Failed to calculate margin")
            return {"status": "error", "message": error_message}

        total_margin = float(response_data.get("total_margin", 0))
        orders = response_data.get("orders", [])

        # For Tradier, margin is simpler - no SPAN/exposure breakdown
        response = {
            "status": "success",
            "data": {
                "total_margin_required": total_margin,
                "span_margin": total_margin,  # No separate SPAN for US
                "exposure_margin": 0,
            },
        }

        logger.info(f"Tradier margin result: total={total_margin:.2f}")
        return response

    except Exception as e:
        logger.error(f"Error parsing Tradier margin response: {e}")
        return {"status": "error", "message": f"Failed to parse margin response: {str(e)}"}
