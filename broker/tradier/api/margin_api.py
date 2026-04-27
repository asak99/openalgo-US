import json

from broker.tradier.mapping.margin_data import parse_margin_response, transform_margin_positions
from broker.tradier.api.order_api import _parse_auth, _get_headers, _get_account_id, BASE_URL
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)


def calculate_margin_api(positions, auth):
    """
    Calculate margin requirement for positions using Tradier API.

    Tradier doesn't have a direct basket margin endpoint like Indian brokers.
    We use the order preview feature to estimate margin impact.

    Args:
        positions: List of positions in OpenAlgo format
        auth: Authentication token

    Returns:
        Tuple of (response, response_data)
    """
    access_token, account_id = _parse_auth(auth)

    # Transform positions to Tradier format
    transformed_positions = transform_margin_positions(positions)

    if not transformed_positions:
        error_response = {
            "status": "error",
            "message": "No valid positions to calculate margin. Check if symbols are valid.",
        }

        class MockResponse:
            status_code = 400
            status = 400

        return MockResponse(), error_response

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    client = get_httpx_client()

    total_margin = 0
    order_margins = []

    # Preview each position as an order to get margin impact
    for pos in transformed_positions:
        import urllib.parse

        payload = {
            "class": pos.get("class", "equity"),
            "symbol": pos["symbol"],
            "side": pos["side"],
            "quantity": str(pos["quantity"]),
            "type": pos.get("type", "market"),
            "duration": "day",
            "preview": "true",
        }

        if pos.get("class") == "option":
            payload["option_symbol"] = pos.get("option_symbol", pos["symbol"])

        if pos.get("price"):
            payload["price"] = str(pos["price"])

        try:
            encoded = urllib.parse.urlencode(payload)
            response = client.post(
                f"{BASE_URL}/accounts/{account_id}/orders",
                headers=headers,
                content=encoded,
            )

            if response.status_code == 200:
                data = response.json()
                order_preview = data.get("order", {})
                margin_change = float(order_preview.get("margin_change", 0))
                order_cost = float(order_preview.get("order_cost", 0))

                order_margins.append({
                    "symbol": pos["symbol"],
                    "margin_change": margin_change,
                    "order_cost": order_cost,
                })
                total_margin += abs(margin_change)

                logger.info(
                    f"Margin preview for {pos['symbol']}: "
                    f"margin_change={margin_change}, order_cost={order_cost}"
                )
            else:
                logger.warning(
                    f"Margin preview failed for {pos['symbol']}: {response.text}"
                )

        except Exception as e:
            logger.error(f"Error previewing order for {pos['symbol']}: {e}")

    # Parse and standardize response
    raw_response = {
        "status": "success",
        "total_margin": total_margin,
        "orders": order_margins,
    }

    standardized_response = parse_margin_response(raw_response)

    class SuccessResponse:
        status_code = 200
        status = 200

    return SuccessResponse(), standardized_response
