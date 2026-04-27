# database/master_contract_db.py
# Downloads and stores US stock and options symbols from Alpaca

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


def download_master_contract(auth_token=None):
    """
    Download and store US equity symbols from Alpaca Assets API.
    
    Alpaca provides a comprehensive assets endpoint that lists all tradable symbols.
    """
    try:
        if not auth_token:
            login_username = os.getenv("LOGIN_USERNAME")
            auth_token = get_auth_token(login_username)

        if not auth_token:
            logger.error("No auth token available for master contract download")
            return None

        parts = auth_token.split(":")
        api_key = parts[0]
        api_secret = parts[1] if len(parts) > 1 else ""
        url_flag = parts[3] if len(parts) > 3 else "paper"

        base_url = "https://paper-api.alpaca.markets" if url_flag == "paper" else "https://api.alpaca.markets"

        client = get_httpx_client()
        headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
            "Accept": "application/json",
        }

        all_symbols_data = []

        # Fetch all active, tradable US equities
        response = client.get(
            f"{base_url}/v2/assets?status=active&asset_class=us_equity",
            headers=headers,
        )

        if response.status_code != 200:
            logger.error(f"Failed to fetch assets: {response.status_code}")
            return None

        assets = response.json()
        logger.info(f"Fetched {len(assets)} assets from Alpaca")

        total = len(assets)
        for idx, asset in enumerate(assets):
            if not asset.get("tradable", False):
                continue

            symbol = asset.get("symbol", "")
            if not symbol:
                continue

            exchange = _map_alpaca_exchange(asset.get("exchange", ""))

            sym_data = {
                "symbol": symbol,
                "brsymbol": symbol,
                "name": asset.get("name", symbol),
                "exchange": exchange,
                "brexchange": asset.get("exchange", ""),
                "token": f"{symbol}_{exchange}",
                "expiry": "",
                "strike": -1,
                "lotsize": 1,
                "instrumenttype": "EQ",
                "tick_size": 0.01,
            }
            all_symbols_data.append(sym_data)

            # Emit progress every 500 symbols
            if idx % 500 == 0:
                progress = min(90, int(idx / total * 90))
                socketio.emit("master_contract_progress", {
                    "broker": "alpaca",
                    "progress": progress,
                    "message": f"Processing {len(all_symbols_data)} symbols...",
                })

        if not all_symbols_data:
            logger.error("No symbol data fetched from Alpaca")
            return None

        df = pd.DataFrame(all_symbols_data)

        # Add index symbols
        index_symbols = _get_index_symbols()
        if index_symbols:
            df = pd.concat([df, pd.DataFrame(index_symbols)], ignore_index=True)

        logger.info(f"Total symbols to insert: {len(df)}")

        delete_symtoken_table()
        copy_from_dataframe(df)

        socketio.emit("master_contract_progress", {
            "broker": "alpaca",
            "progress": 100,
            "message": f"Completed. {len(df)} symbols loaded.",
        })

        return df

    except Exception as e:
        logger.exception(f"Error downloading master contract: {e}")
        return None


def _map_alpaca_exchange(exch_code):
    """Map Alpaca exchange code to OpenAlgo exchange."""
    mapping = {
        "NASDAQ": "NASDAQ",
        "NYSE": "NYSE",
        "AMEX": "AMEX",
        "ARCA": "NYSE",
        "BATS": "NYSE",
        "IEX": "NYSE",
        "OTC": "NYSE",
    }
    return mapping.get(exch_code, "NYSE")


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
