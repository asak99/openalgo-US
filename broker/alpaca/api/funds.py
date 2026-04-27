# api/funds.py - Alpaca account balance and margin data

import os

from broker.alpaca.api.order_api import _parse_auth, _get_headers, _get_base_url
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)


def get_margin_data(auth_token):
    """Fetch margin/balance data from Alpaca's API."""
    api_key, api_secret, _, url_flag = _parse_auth(auth_token)
    base_url = "https://paper-api.alpaca.markets" if url_flag == "paper" else "https://api.alpaca.markets"

    client = get_httpx_client()
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
        "Accept": "application/json",
    }

    try:
        response = client.get(f"{base_url}/v2/account", headers=headers)
        response.raise_for_status()
        account = response.json()
    except Exception as e:
        logger.error(f"Error fetching account data: {e}")
        return {}

    try:
        # Alpaca account fields
        equity = float(account.get("equity", 0))
        cash = float(account.get("cash", 0))
        buying_power = float(account.get("buying_power", 0))
        portfolio_value = float(account.get("portfolio_value", 0))

        # Margin specific
        initial_margin = float(account.get("initial_margin", 0))
        maintenance_margin = float(account.get("maintenance_margin", 0))
        last_maintenance_margin = float(account.get("last_maintenance_margin", 0))

        # PnL
        # Alpaca doesn't directly provide daily PnL in account endpoint
        # We calculate from positions
        total_unrealised = 0
        total_realised = 0

        try:
            pos_response = client.get(f"{base_url}/v2/positions", headers=headers)
            pos_response.raise_for_status()
            positions = pos_response.json()

            for p in positions:
                unrealized = float(p.get("unrealized_pl", 0))
                total_unrealised += unrealized
        except Exception as e:
            logger.error(f"Error fetching positions for PnL: {e}")

        # Try to get today's PnL from portfolio history
        try:
            hist_response = client.get(
                f"{base_url}/v2/account/portfolio/history?period=1D&timeframe=1D",
                headers=headers,
            )
            if hist_response.status_code == 200:
                hist_data = hist_response.json()
                profit_loss = hist_data.get("profit_loss", [0])
                if profit_loss:
                    total_realised = float(profit_loss[-1]) if profit_loss[-1] else 0
        except Exception as e:
            logger.error(f"Error fetching portfolio history: {e}")

        available_cash = buying_power
        used_margin = initial_margin
        collateral = equity - cash if equity > cash else 0

        processed_margin_data = {
            "availablecash": f"{available_cash:.2f}",
            "collateral": f"{collateral:.2f}",
            "m2munrealized": f"{total_unrealised:.2f}",
            "m2mrealized": f"{total_realised:.2f}",
            "utiliseddebits": f"{used_margin:.2f}",
        }
        return processed_margin_data

    except KeyError as e:
        logger.error(f"Unexpected account data structure: {e}")
        return {}
    except Exception as e:
        logger.error(f"Error processing margin data: {e}")
        return {}
