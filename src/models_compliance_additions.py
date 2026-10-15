"""
Model additions for compliance gates.

Merge these classes into src/models.py (or import and create_all).
They use the same Base.
"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean
from src.database import Base


class ConsentDB(Base):
    __tablename__ = "consents"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    source = Column(String, nullable=False)  # form | double_opt_in | imported_with_proof | manual
    consented_at = Column(DateTime, default=datetime.utcnow)
    proof_url = Column(String, nullable=True)


class ReviewQueueDB(Base):
    __tablename__ = "review_queue"

    id = Column(Integer, primary_key=True, index=True)
    review_id = Column(String, unique=True, index=True, nullable=False)
    prospect_email = Column(String, index=True, nullable=False)
    campaign_id = Column(String, nullable=True)
    subject = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    step = Column(Integer, default=1)
    status = Column(String, default="pending")  # pending | approved | rejected
    reviewed_by = Column(String, nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class SuppressionDB(Base):
    __tablename__ = "suppressions"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    reason = Column(String, nullable=False)  # hard_bounce | complaint | manual
    created_at = Column(DateTime, default=datetime.utcnow)
    notes = Column(String, nullable=True)
