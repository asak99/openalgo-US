import os

from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)


def authenticate_broker(auth_token):
    """
    Authenticate with Tradier using an API access token.

    For individual developers, the access token is obtained from the Tradier
    API Settings dashboard. For OAuth apps, the auth_token would be the
    authorization code exchanged for an access token.

    Args:
        auth_token (str): Tradier API access token (Bearer token)

    Returns:
        tuple: (access_token, error_message) - access_token is None on failure
    """
    try:
        BROKER_API_KEY = os.getenv("BROKER_API_KEY")  # Not used for Tradier direct token auth
        BROKER_API_SECRET = os.getenv("BROKER_API_SECRET")  # Tradier access token

        # If auth_token is provided (from callback), use it directly
        # Otherwise fall back to env var
        access_token = auth_token if auth_token else BROKER_API_SECRET

        if not access_token:
            return None, "No access token provided. Set BROKER_API_SECRET to your Tradier access token."

        # Validate the token by calling the user profile endpoint
        client = get_httpx_client()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        response = client.get("https://api.tradier.com/v1/user/profile", headers=headers)

        if response.status_code == 200:
            data = response.json()
            profile = data.get("profile", {})
            account = profile.get("account", {})

            # Handle both single account and multiple accounts
            if isinstance(account, list):
                account_id = account[0].get("account_number", "")
            else:
                account_id = account.get("account_number", "")

            # Store account_id in the token for later use (token:account_id format)
            combined_token = f"{access_token}:{account_id}"
            logger.info(f"Tradier authentication successful for account: {account_id}")
            return combined_token, None
        else:
            error_data = response.json() if response.text else {}
            error_msg = error_data.get("fault", {}).get("faultstring", f"HTTP {response.status_code}")
            return None, f"Tradier authentication failed: {error_msg}"

    except Exception as e:
        logger.exception(f"Tradier authentication error: {e}")
        return None, f"An exception occurred: {str(e)}"
