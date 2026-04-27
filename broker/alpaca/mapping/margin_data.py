# Mapping OpenAlgo API - Alpaca Margin Data

from database.token_db import get_br_symbol
from utils.logging import get_logger

logger = get_logger(__name__)


def transform_margin_positions(positions):
    """Transform OpenAlgo positions to Alpaca format for margin estimation."""
    transformed = []
    skipped = []

    for position in positions:
        try:
            symbol = position["symbol"]
            exchange = position["exchange"]
            br_symbol = get_br_symbol(symbol, exchange)

            if not br_symbol or str(br_symbol).lower() == "none":
                skipped.append(f"{symbol} ({exchange})")
                continue

            is_option = exchange == "US_OPTIONS" or len(str(br_symbol)) > 10

            transformed.append({
                "symbol": str(br_symbol),
                "side": position["action"].lower(),
                "qty": int(position["quantity"]),
                "type": "option" if is_option else "equity",
                "limit_price": float(position.get("price", 0)),
            })

        except Exception as e:
            skipped.append(f"{position.get('symbol', 'unknown')} - {str(e)}")

    if skipped:
        logger.warning(f"Skipped {len(skipped)} positions: {', '.join(skipped)}")

    return transformed


def parse_margin_response(response_data):
    """Parse margin response to OpenAlgo format."""
    try:
        if not response_data or response_data.get("status") != "success":
            return {"status": "error", "message": response_data.get("message", "Failed")}

        total_margin = float(response_data.get("total_margin", 0))

        return {
            "status": "success",
            "data": {
                "total_margin_required": total_margin,
                "span_margin": total_margin,
                "exposure_margin": 0,
            },
        }

    except Exception as e:
        logger.error(f"Error parsing margin response: {e}")
        return {"status": "error", "message": str(e)}
