import os
import logging
from dotenv import load_dotenv
from pathlib import Path
from sqlalchemy import text
# Load environment variables
load_dotenv()


def get_env(var_name: str):
    value = os.getenv(var_name)
    if value is None:
        raise EnvironmentError(f"Missing required environment variable: {var_name}")
    return value

# Logging setup
def setup_logger(name="app", log_file="logs/app.log"):
    os.makedirs("logs", exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger
# -*- coding: utf-8 -*-

def get_project_root():
    """
    Returns the root folder of the project.
    Works whether running from script or interactive environment.
    """
    try:
        # If running as a script
        return Path(__file__).parent.parent
    except NameError:
        # Fallback for interactive environments (e.g., Jupyter, IPython)
        return Path.cwd()


def load_locations(engine):
    """
    Pulls the active store list from the Postgres database.
    Explicitly converts GUIDs to strings to ensure compatibility with API headers.
    """
    query = text("""
        SELECT location_name, store_guid 
        FROM locations 
    """)

    try:
        with engine.connect() as conn:
            result = conn.execute(query)

            # We cast str(row.store_guid) here to prevent the 'uuid.UUID' header error
            locations = [
                {
                    "location_name": row.location_name,
                    "store_guid": str(row.store_guid)
                }
                for row in result
            ]

        if not locations:
            logging.warning("Database query successful, but no active locations found.")

        return locations

    except Exception as e:
        logging.error(f"Error loading locations from DB: {e}")
        return []