import json

from broker.alpaca.mapping.margin_data import parse_margin_response, transform_margin_positions
from broker.alpaca.api.order_api import _parse_auth, _get_headers, _get_base_url
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)


def calculate_margin_api(positions, auth):
    """
    Calculate margin requirement for positions.

    Alpaca doesn't have a dedicated margin calculation endpoint.
    We estimate from account buying power changes.
    """
    transformed_positions = transform_margin_positions(positions)

    if not transformed_positions:
        error_response = {
            "status": "error",
            "message": "No valid positions to calculate margin.",
        }

        class MockResponse:
            status_code = 400
            status = 400

        return MockResponse(), error_response

    api_key, api_secret, _, url_flag = _parse_auth(auth)
    base_url = "https://paper-api.alpaca.markets" if url_flag == "paper" else "https://api.alpaca.markets"

    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
        "Accept": "application/json",
    }

    client = get_httpx_client()

    # Get current account for buying power
    try:
        account_resp = client.get(f"{base_url}/v2/account", headers=headers)
        account_resp.raise_for_status()
        account = account_resp.json()

        buying_power = float(account.get("buying_power", 0))
        initial_margin = float(account.get("initial_margin", 0))

        # Estimate margin for positions based on Reg-T
        total_margin = 0
        for pos in transformed_positions:
            qty = int(pos.get("qty", 0))
            # For equities, Reg-T margin = 50% of position value
            # For options, margin varies
            estimated_price = float(pos.get("limit_price", 100))  # rough estimate
            position_value = qty * estimated_price

            if pos.get("type") == "option":
                # Options margin is complex; use position value as approximation
                total_margin += position_value
            else:
                # Reg-T: 50% initial margin for equities
                total_margin += position_value * 0.5

        raw_response = {
            "status": "success",
            "total_margin": total_margin,
            "buying_power": buying_power,
        }

        standardized = parse_margin_response(raw_response)

        class SuccessResponse:
            status_code = 200
            status = 200

        return SuccessResponse(), standardized

    except Exception as e:
        logger.error(f"Error calculating margin: {e}")
        error_response = {"status": "error", "message": f"Failed to calculate margin: {str(e)}"}

        class ErrorResponse:
            status_code = 500
            status = 500

        return ErrorResponse(), error_response
