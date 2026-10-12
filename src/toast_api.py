# -*- coding: utf-8 -*-
import time
import requests
import logging
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from src.utils import get_env
from src.database_setup import APIToken  # Your established model

logger = logging.getLogger("ToastAPI")


class ToastAPI:
    def __init__(self, engine=None):
        self.base_url = get_env("TOAST_API_HOST")
        self.client_id = get_env("TOAST_CLIENT_ID")
        self.client_secret = get_env("TOAST_CLIENT_SECRET")

        # Injected engine from main()
        self.engine = engine
        self.token = None
        self.token_expiry = 0

        # Initial check
        if self.engine:
            self._load_token_from_db()

    def _load_token_from_db(self):
        """Uses the injected engine to fetch token metadata."""
        if not self.engine:
            return

        with Session(self.engine) as session:
            try:
                # Query using your APIToken model
                token_record = session.query(APIToken).filter_by(service_name='toast').first()
                if token_record:
                    self.token = token_record.access_token
                    self.token_expiry = token_record.expires_at
                    logger.info("Retrieved cached token from Postgres.")
            except Exception as e:
                logger.warning(f"Failed to read token from DB: {e}")

    def _save_token_to_db(self, created_at):
        """Restores your important Atomic Upsert logic using the injected engine."""
        if not self.engine:
            return

        with Session(self.engine) as session:
            try:
                # Prepare the Postgres-specific UPSERT (The logic you pointed out)
                stmt = insert(APIToken).values(
                    service_name='toast',
                    access_token=self.token,
                    client_id=self.client_id,
                    expires_at=self.token_expiry,
                    created_at=created_at
                )

                # Maintain the "Update if exists" logic
                stmt = stmt.on_conflict_do_update(
                    index_elements=['service_name'],
                    set_={
                        'access_token': stmt.excluded.access_token,
                        'expires_at': stmt.excluded.expires_at,
                        'created_at': stmt.excluded.created_at
                    }
                )

                session.execute(stmt)
                session.commit()
                logger.info("Token successfully synced to shared database.")
            except Exception as e:
                session.rollback()
                logger.error(f"Failed to save token to database: {e}")

    def _authenticate(self):
        """Fetch a fresh access token from the Toast Auth API."""
        logger.info("Requesting fresh Toast credentials...")

        url = f"{self.base_url}/authentication/v1/authentication/login"
        payload = {
            "clientId": self.client_id,
            "clientSecret": self.client_secret,
            "userAccessType": "TOAST_MACHINE_CLIENT"
        }

        response = requests.post(url, json=payload)
        response.raise_for_status()

        data = response.json()
        token_data = data.get("token", {})
        self.token = token_data.get("accessToken")
        expires_in = token_data.get("expiresIn", 86400)

        now = time.time()
        self.token_expiry = now + expires_in - 59  # 1-hour safety buffer

        self._save_token_to_db(created_at=now)
        return self.token

    def get_token(self):
        # Double-check DB before authenticating to see if another store-run already did it
        if not self.token or time.time() >= self.token_expiry:
            self._load_token_from_db()

        if not self.token or time.time() >= self.token_expiry:
            return self._authenticate()

        return self.token

    def get_headers(self):
        return {
            "Authorization": f"Bearer {self.get_token()}",
            "Content-Type": "application/json"
        }