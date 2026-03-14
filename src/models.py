"""
SQLAlchemy ORM models for the Automated Sales Outreach platform.
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean,
    DateTime, Text, ForeignKey,
)
from sqlalchemy.orm import relationship
from src.database import Base


class CampaignDB(Base):
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=False)
    template = Column(String, default="problem_agitate_solve")
    daily_limit = Column(Integer, default=500)
    status = Column(String, default="active")  # active | paused | completed
    created_at = Column(DateTime, default=datetime.utcnow)

    emails = relationship("EmailLogDB", back_populates="campaign")
    prospects = relationship("ProspectDB", back_populates="campaign")


class ProspectDB(Base):
    __tablename__ = "prospects"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, index=True, nullable=False)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    company = Column(String, nullable=False)
    title = Column(String, nullable=False)
    linkedin_url = Column(String, nullable=True)
    campaign_id = Column(String, ForeignKey("campaigns.campaign_id"), nullable=True)
    sequence_step = Column(Integer, default=0)   # 0=pending, 1–5=touch number
    last_contacted = Column(DateTime, nullable=True)
    replied = Column(Boolean, default=False)
    unsubscribed = Column(Boolean, default=False)
    meeting_booked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    campaign = relationship("CampaignDB", back_populates="prospects")


class EmailLogDB(Base):
    __tablename__ = "email_logs"

    id = Column(Integer, primary_key=True, index=True)
    campaign_id = Column(String, ForeignKey("campaigns.campaign_id"), nullable=True)
    prospect_email = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    step = Column(Integer, default=1)
    status = Column(String, default="pending")   # pending | sent | opened | replied | bounced
    sent_at = Column(DateTime, nullable=True)
    opened_at = Column(DateTime, nullable=True)
    replied_at = Column(DateTime, nullable=True)

    campaign = relationship("CampaignDB", back_populates="emails")


class UnsubscribeDB(Base):
    __tablename__ = "unsubscribes"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    reason = Column(String, nullable=True)
    unsubscribed_at = Column(DateTime, default=datetime.utcnow)


class SubscriptionDB(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String, unique=True, index=True, nullable=False)
    stripe_customer_id = Column(String, nullable=True)
    stripe_subscription_id = Column(String, nullable=True)
    plan = Column(String, default="starter")       # starter | pro | agency
    daily_limit = Column(Integer, default=500)
    status = Column(String, default="active")      # active | canceled | past_due
    monthly_revenue = Column(Float, default=299.0)
    created_at = Column(DateTime, default=datetime.utcnow)
