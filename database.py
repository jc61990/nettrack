import os
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Set DATABASE_URL in your environment, e.g.:
# postgresql://nettrack:password@localhost:5432/nettrack
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://nettrack:changeme@localhost:5432/nettrack",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
