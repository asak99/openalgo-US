import os

from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

# Alpaca endpoints
LIVE_BASE_URL = "https://api.alpaca.markets"
PAPER_BASE_URL = "https://paper-api.alpaca.markets"


def authenticate_broker(auth_token):
    """
    Authenticate with Alpaca using API Key ID and Secret Key.

    For Alpaca, BROKER_API_KEY = API Key ID, BROKER_API_SECRET = Secret Key.
    auth_token parameter is used as a flag: if "paper", use paper trading endpoint.

    Args:
        auth_token (str): "live" or "paper" to select endpoint, or the API key directly

    Returns:
        tuple: (access_token, error_message) - access_token is combined key on success
    """
    try:
        BROKER_API_KEY = os.getenv("BROKER_API_KEY")
        BROKER_API_SECRET = os.getenv("BROKER_API_SECRET")

        if not BROKER_API_KEY or not BROKER_API_SECRET:
            return None, "Missing BROKER_API_KEY or BROKER_API_SECRET for Alpaca"

        # Determine if paper or live trading
        use_paper = os.getenv("ALPACA_PAPER_TRADING", "true").lower() == "true"
        if auth_token and auth_token.lower() == "live":
            use_paper = False

        base_url = PAPER_BASE_URL if use_paper else LIVE_BASE_URL

        # Validate credentials by calling account endpoint
        client = get_httpx_client()
        headers = {
            "APCA-API-KEY-ID": BROKER_API_KEY,
            "APCA-API-SECRET-KEY": BROKER_API_SECRET,
            "Accept": "application/json",
        }

        response = client.get(f"{base_url}/v2/account", headers=headers)

        if response.status_code == 200:
            account_data = response.json()
            account_id = account_data.get("account_number", account_data.get("id", ""))
            account_status = account_data.get("status", "")

            if account_status not in ("ACTIVE", "APPROVED"):
                logger.warning(f"Alpaca account status: {account_status}")

            # Combined token format: api_key:api_secret:account_id:base_url_flag
            url_flag = "paper" if use_paper else "live"
            combined_token = f"{BROKER_API_KEY}:{BROKER_API_SECRET}:{account_id}:{url_flag}"
            logger.info(f"Alpaca authentication successful for account: {account_id} ({url_flag})")
            return combined_token, None
        elif response.status_code == 401:
            return None, "Alpaca authentication failed: Invalid API key or secret"
        elif response.status_code == 403:
            return None, "Alpaca authentication failed: Account not authorized"
        else:
            error_msg = f"HTTP {response.status_code}"
            try:
                error_data = response.json()
                error_msg = error_data.get("message", error_msg)
            except Exception:
                pass
            return None, f"Alpaca authentication failed: {error_msg}"

    except Exception as e:
        logger.exception(f"Alpaca authentication error: {e}")
        return None, f"An exception occurred: {str(e)}"
