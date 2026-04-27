# database/master_contract_db.py
# Downloads and stores US stock and options symbols from Tradier

import os
import pandas as pd
import numpy as np
import json

from utils.httpx_client import get_httpx_client
from sqlalchemy import create_engine, Column, Integer, String, Float, Sequence, Index
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from database.auth_db import get_auth_token
from extensions import socketio
from utils.logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_engine(DATABASE_URL)
db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


class SymToken(Base):
    __tablename__ = "symtoken"
    id = Column(Integer, Sequence("symtoken_id_seq"), primary_key=True)
    symbol = Column(String, nullable=False, index=True)
    brsymbol = Column(String, nullable=False, index=True)
    name = Column(String)
    exchange = Column(String, index=True)
    brexchange = Column(String, index=True)
    token = Column(String, index=True)
    expiry = Column(String)
    strike = Column(Float)
    lotsize = Column(Integer)
    instrumenttype = Column(String)
    tick_size = Column(Float)

    __table_args__ = (Index("idx_symbol_exchange", "symbol", "exchange"),)


def init_db():
    logger.info("Initializing Master Contract DB")
    Base.metadata.create_all(bind=engine)


def delete_symtoken_table():
    logger.info("Deleting Symtoken Table")
    SymToken.query.delete()
    db_session.commit()


def copy_from_dataframe(df):
    logger.info("Performing Bulk Insert")
    data_dict = df.to_dict(orient="records")

    existing_tokens = {result.token for result in db_session.query(SymToken.token).all()}
    filtered_data_dict = [row for row in data_dict if row["token"] not in existing_tokens]

    try:
        if filtered_data_dict:
            db_session.bulk_insert_mappings(SymToken, filtered_data_dict)
            db_session.commit()
            logger.info(f"Bulk insert completed with {len(filtered_data_dict)} new records.")
        else:
            logger.info("No new records to insert.")
    except Exception as e:
        logger.error(f"Error during bulk insert: {e}")
        db_session.rollback()


# --- Common US Stock Lists ---
# Top US stocks/ETFs to include in the symbol database
COMMON_US_SYMBOLS = [
    # Major indices ETFs
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO",
    # Leveraged ETFs
    "TQQQ", "SQQQ", "SPXL", "SPXS", "UVXY", "SVXY",
    # Mega cap tech
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA",
    # Semiconductors
    "AMD", "INTC", "AVGO", "QCOM", "MU", "MRVL", "ARM", "SMCI",
    # Software & Cloud
    "CRM", "ORCL", "ADBE", "NOW", "SNOW", "PLTR", "NET", "DDOG",
    # Financials
    "JPM", "BAC", "GS", "MS", "C", "WFC", "BRK.B", "V", "MA",
    # Healthcare
    "UNH", "JNJ", "PFE", "ABBV", "MRK", "LLY", "BMY", "TMO",
    # Energy
    "XOM", "CVX", "COP", "SLB", "OXY", "DVN", "HAL",
    # Consumer
    "WMT", "COST", "HD", "TGT", "AMZN", "SBUX", "NKE", "MCD",
    # Industrial
    "CAT", "DE", "BA", "GE", "RTX", "LMT", "HON",
    # EV / Clean Energy
    "RIVN", "LCID", "NIO", "LI", "XPEV", "ENPH", "FSLR",
    # AI / Quantum / Speculative
    "IONQ", "RGTI", "QBTS", "OKLO", "SMR", "MSTR",
    # Crypto related
    "COIN", "MARA", "RIOT",
    # Sector ETFs
    "XLF", "XLE", "XLK", "XLV", "XLI", "XLU", "XLP", "XLY", "XLB", "XLRE",
    # VIX products
    "VXX", "VIXY",
    # Bond ETFs
    "TLT", "IEF", "SHY", "LQD", "HYG", "AGG", "BND",
    # International
    "EEM", "EFA", "FXI", "KWEB",
    # Gold / Commodities
    "GLD", "SLV", "USO", "GDX",
]


def download_master_contract(auth_token=None):
    """
    Download and store US equity and options symbols from Tradier.
    
    For US markets, we:
    1. Fetch equity quotes for common symbols to validate them
    2. Store them in the symtoken table with appropriate exchange mapping
    """
    try:
        if not auth_token:
            login_username = os.getenv("LOGIN_USERNAME")
            auth_token = get_auth_token(login_username)

        if not auth_token:
            logger.error("No auth token available for master contract download")
            return None

        access_token = auth_token.split(":")[0] if ":" in auth_token else auth_token

        client = get_httpx_client()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        all_symbols_data = []

        # Process symbols in batches
        batch_size = 50
        for i in range(0, len(COMMON_US_SYMBOLS), batch_size):
            batch = COMMON_US_SYMBOLS[i : i + batch_size]
            symbols_str = ",".join(batch)

            try:
                response = client.get(
                    f"https://api.tradier.com/v1/markets/quotes?symbols={symbols_str}&greeks=false",
                    headers=headers,
                )

                if response.status_code != 200:
                    logger.warning(f"Failed to fetch batch {i}: {response.status_code}")
                    continue

                data = response.json()
                quotes = data.get("quotes", {})
                quote_list = quotes.get("quote", [])

                if isinstance(quote_list, dict):
                    quote_list = [quote_list]

                # Also check for unmatched symbols
                unmatched = quotes.get("unmatched_symbols", {})
                unmatched_list = unmatched.get("symbol", []) if unmatched else []
                if isinstance(unmatched_list, str):
                    unmatched_list = [unmatched_list]

                for quote in quote_list:
                    symbol = quote.get("symbol", "")
                    if not symbol:
                        continue

                    # Determine exchange from Tradier response
                    exch = quote.get("exch", "")
                    exchange = _map_tradier_exchange(exch, quote.get("type", "stock"))
                    
                    sym_data = {
                        "symbol": symbol,
                        "brsymbol": symbol,
                        "name": quote.get("description", symbol),
                        "exchange": exchange,
                        "brexchange": exch,
                        "token": f"{symbol}_{exchange}",
                        "expiry": "",
                        "strike": -1,
                        "lotsize": 1,
                        "instrumenttype": _map_security_type(quote.get("type", "stock")),
                        "tick_size": 0.01,
                    }
                    all_symbols_data.append(sym_data)

                    # Emit progress
                    progress = min(100, int((i + batch_size) / len(COMMON_US_SYMBOLS) * 80))
                    socketio.emit("master_contract_progress", {
                        "broker": "tradier",
                        "progress": progress,
                        "message": f"Processing {len(all_symbols_data)} symbols...",
                    })

            except Exception as e:
                logger.error(f"Error fetching batch {i}: {e}")
                continue

        if not all_symbols_data:
            logger.error("No symbol data fetched from Tradier")
            return None

        df = pd.DataFrame(all_symbols_data)

        # Also add index symbols
        index_symbols = _get_index_symbols()
        if index_symbols:
            df = pd.concat([df, pd.DataFrame(index_symbols)], ignore_index=True)

        logger.info(f"Total symbols to insert: {len(df)}")

        # Store in database
        delete_symtoken_table()
        copy_from_dataframe(df)

        socketio.emit("master_contract_progress", {
            "broker": "tradier",
            "progress": 100,
            "message": f"Completed. {len(df)} symbols loaded.",
        })

        return df

    except Exception as e:
        logger.exception(f"Error downloading master contract: {e}")
        return None


def _map_tradier_exchange(exch_code, security_type="stock"):
    """Map Tradier exchange code to OpenAlgo exchange."""
    if security_type == "option":
        return "US_OPTIONS"

    mapping = {
        "Q": "NASDAQ",
        "N": "NYSE",
        "A": "AMEX",
        "P": "NYSE",  # NYSE Arca
        "Z": "NYSE",  # BATS
        "V": "NYSE",  # IEX
    }
    return mapping.get(exch_code, "NYSE")


def _map_security_type(sec_type):
    """Map Tradier security type to OpenAlgo instrument type."""
    mapping = {
        "stock": "EQ",
        "etf": "EQ",
        "option": "OPT",
        "index": "IDX",
    }
    return mapping.get(sec_type, "EQ")


def _get_index_symbols():
    """Return common US index symbols."""
    indices = [
        {"symbol": "SPX", "name": "S&P 500 Index", "exchange": "US_INDEX"},
        {"symbol": "NDX", "name": "NASDAQ 100 Index", "exchange": "US_INDEX"},
        {"symbol": "DJI", "name": "Dow Jones Industrial Average", "exchange": "US_INDEX"},
        {"symbol": "RUT", "name": "Russell 2000 Index", "exchange": "US_INDEX"},
        {"symbol": "VIX", "name": "CBOE Volatility Index", "exchange": "US_INDEX"},
    ]

    result = []
    for idx in indices:
        result.append({
            "symbol": idx["symbol"],
            "brsymbol": idx["symbol"],
            "name": idx["name"],
            "exchange": idx["exchange"],
            "brexchange": idx["exchange"],
            "token": f"{idx['symbol']}_{idx['exchange']}",
            "expiry": "",
            "strike": -1,
            "lotsize": 1,
            "instrumenttype": "IDX",
            "tick_size": 0.01,
        })
    return result


def search_symbols(query, exchange=None):
    """Search for symbols using Tradier's lookup API."""
    try:
        login_username = os.getenv("LOGIN_USERNAME")
        auth_token = get_auth_token(login_username)

        if not auth_token:
            return []

        access_token = auth_token.split(":")[0] if ":" in auth_token else auth_token

        client = get_httpx_client()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        response = client.get(
            f"https://api.tradier.com/v1/markets/lookup?q={query}&exchanges=Q,N,A&types=stock,etf",
            headers=headers,
        )

        if response.status_code != 200:
            return []

        data = response.json()
        securities = data.get("securities", {})
        security_list = securities.get("security", [])

        if isinstance(security_list, dict):
            security_list = [security_list]

        results = []
        for sec in security_list:
            results.append({
                "symbol": sec.get("symbol", ""),
                "name": sec.get("description", ""),
                "exchange": _map_tradier_exchange(sec.get("exchange", ""), sec.get("type", "stock")),
                "type": sec.get("type", "stock"),
            })

        return results

    except Exception as e:
        logger.error(f"Error searching symbols: {e}")
        return []
