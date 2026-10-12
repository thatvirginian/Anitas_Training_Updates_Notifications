# -*- coding: utf-8 -*-
import os
from sqlalchemy import create_engine, Column, String, Float, DateTime, func
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

# 1. Environment & Base Configuration
load_dotenv(override=True)
Base = declarative_base()

# 2. Connection URLs

# payroll_training — all training data + token storage
connection_url = URL.create(
    drivername="postgresql+psycopg2",
    username=os.getenv("PGUSER"),
    password=os.getenv("PGPASSWORD"),
    host=os.getenv("PGHOST"),
    port=os.getenv("PGPORT"),
    database=os.getenv("PGDATABASE"),              # payroll_training
    query={"sslmode": "require"},
)

# toast_order_data — read locations only
locations_url = URL.create(
    drivername="postgresql+psycopg2",
    username=os.getenv("PGUSER"),
    password=os.getenv("PGPASSWORD"),
    host=os.getenv("PGHOST"),
    port=os.getenv("PGPORT"),
    database=os.getenv("PGDATABASE_LOCATIONS"),    # toast_order_data
    query={"sslmode": "require"},
)

# 3. Engines

# Primary engine — payroll_training
engine = create_engine(
    connection_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20
)

# Locations engine — toast_order_data (read-only use)
locations_engine = create_engine(
    locations_url,
    pool_pre_ping=True,
    pool_size=2,        # small — only used once at startup to load locations
    max_overflow=2
)

# 4. Session Factories
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
LocationsSession = sessionmaker(autocommit=False, autoflush=False, bind=locations_engine)


# 5. Models

class APIToken(Base):
    """
    Cached Toast API bearer token for this service.
    One row: service_name = 'toast_training'.
    Stored in payroll_training.api_tokens.
    """
    __tablename__ = 'api_tokens'
    service_name = Column(String, primary_key=True)
    access_token = Column(String, nullable=False)
    client_id    = Column(String, nullable=False)
    expires_at   = Column(Float,  nullable=False)
    created_at   = Column(Float,  nullable=False)
    updated_at   = Column(DateTime, server_default=func.now(), onupdate=func.now())


# 6. Engine Getters

def get_engine():
    """Returns the primary SQLAlchemy engine (payroll_training)."""
    return engine

def get_locations_engine():
    """Returns the locations engine (toast_order_data). Read-only use."""
    return locations_engine