import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "pipeline"))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import POSTGRES_DSN

engine = create_engine(POSTGRES_DSN, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
