# api/funds.py - Tradier account balance and margin data

import os

from broker.tradier.api.order_api import _parse_auth, _get_headers, _get_account_id, BASE_URL
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)


def get_margin_data(auth_token):
    """Fetch margin/balance data from Tradier's API using the provided auth token."""
    access_token, account_id = _parse_auth(auth_token)

    client = get_httpx_client()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }

    try:
        # Get account balances
        response = client.get(
            f"{BASE_URL}/accounts/{account_id}/balances",
            headers=headers,
        )
        response.raise_for_status()
        balance_data = response.json()
    except Exception as e:
        logger.error(f"Error fetching balance data: {e}")
        return {}

    balances = balance_data.get("balances", {})
    if not balances:
        logger.error("No balance data returned from Tradier")
        return {}

    try:
        # Determine account type and extract relevant fields
        # Tradier has different balance structures for margin vs cash accounts
        account_type = balances.get("account_type", "cash")

        if account_type == "margin":
            margin_info = balances.get("margin", {})
            total_equity = float(balances.get("total_equity", 0))
            total_cash = float(balances.get("total_cash", 0))
            market_value = float(balances.get("market_value", 0))

            # Margin-specific fields
            stock_buying_power = float(margin_info.get("stock_buying_power", 0))
            option_buying_power = float(margin_info.get("option_buying_power", 0))
            maintenance_margin = float(margin_info.get("maintenance_requirement", 0)) if margin_info else 0

            available_cash = stock_buying_power
            used_margin = maintenance_margin
            collateral = total_equity - total_cash
        elif account_type == "pdt":
            pdt_info = balances.get("pdt", {})
            total_equity = float(balances.get("total_equity", 0))
            total_cash = float(balances.get("total_cash", 0))

            stock_buying_power = float(pdt_info.get("stock_buying_power", 0))
            option_buying_power = float(pdt_info.get("option_buying_power", 0))

            available_cash = stock_buying_power
            used_margin = float(pdt_info.get("maintenance_requirement", 0)) if pdt_info else 0
            collateral = total_equity - total_cash
        else:
            # Cash account
            cash_info = balances.get("cash", {})
            total_equity = float(balances.get("total_equity", 0))
            total_cash = float(balances.get("total_cash", 0))
            available_cash = float(cash_info.get("cash_available", total_cash))
            used_margin = 0
            collateral = 0

        # Fetch PnL from positions
        total_realised = 0
        total_unrealised = 0
        try:
            pos_response = client.get(
                f"{BASE_URL}/accounts/{account_id}/positions",
                headers=headers,
            )
            pos_response.raise_for_status()
            position_data = pos_response.json()

            positions = position_data.get("positions", {})
            if positions and positions != "null":
                position_list = positions.get("position", [])
                if isinstance(position_list, dict):
                    position_list = [position_list]

                for p in position_list:
                    cost_basis = float(p.get("cost_basis", 0))
                    quantity = int(p.get("quantity", 0))

                    # Get current market value
                    # Tradier positions include cost_basis but not always current value
                    # We can calculate unrealized PnL if we have quotes
                    # For now use the gain/loss endpoint
                    pass

            # Try gainloss endpoint for realized PnL
            gl_response = client.get(
                f"{BASE_URL}/accounts/{account_id}/gainloss",
                headers=headers,
            )
            if gl_response.status_code == 200:
                gl_data = gl_response.json()
                gainloss = gl_data.get("gainloss", {})
                if gainloss and gainloss != "null":
                    closed_positions = gainloss.get("closed_position", [])
                    if isinstance(closed_positions, dict):
                        closed_positions = [closed_positions]
                    for cp in closed_positions:
                        total_realised += float(cp.get("gain_loss", 0))

        except Exception as e:
            logger.error(f"Error fetching positions for PnL: {e}")

        # Calculate unrealized from positions if available
        open_pnl = float(balances.get("open_pl", 0)) if "open_pl" in balances else total_unrealised
        close_pnl = float(balances.get("close_pl", 0)) if "close_pl" in balances else total_realised

        processed_margin_data = {
            "availablecash": f"{available_cash:.2f}",
            "collateral": f"{collateral:.2f}",
            "m2munrealized": f"{open_pnl:.2f}",
            "m2mrealized": f"{close_pnl:.2f}",
            "utiliseddebits": f"{used_margin:.2f}",
        }
        return processed_margin_data

    except KeyError as e:
        logger.error(f"Unexpected balance data structure: {e}")
        return {}
    except Exception as e:
        logger.error(f"Error processing margin data: {e}")
        return {}
