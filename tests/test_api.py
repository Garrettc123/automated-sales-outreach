"""
Pytest test suite for the Automated Sales Outreach API.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #

SAMPLE_PROSPECT = {
    "email": "john.doe@acme.com",
    "first_name": "John",
    "last_name": "Doe",
    "company": "Acme Corp",
    "title": "VP of Sales",
    "linkedin_url": "https://linkedin.com/in/johndoe",
}

SAMPLE_CAMPAIGN = {
    "name": "Q2 Outreach",
    "prospects": [SAMPLE_PROSPECT],
    "template": "problem_agitate_solve",
    "daily_limit": 100,
    "product_description": "AI-powered outreach platform",
}


# --------------------------------------------------------------------------- #
# Root / health                                                                #
# --------------------------------------------------------------------------- #

def test_root(client):
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["service"] == "Automated Sales Outreach"
    assert "pricing" in data
    assert "starter" in data["pricing"]
    assert "pro" in data["pricing"]
    assert "agency" in data["pricing"]


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert "timestamp" in data
    assert "openai_configured" in data
    assert "sendgrid_configured" in data
    assert "stripe_configured" in data


# --------------------------------------------------------------------------- #
# Campaign creation                                                            #
# --------------------------------------------------------------------------- #

def test_create_campaign_v1(client):
    """POST /api/v1/campaigns should persist a campaign and return metadata."""
    with patch("src.main._process_campaign", new_callable=AsyncMock):
        resp = client.post("/api/v1/campaigns", json=SAMPLE_CAMPAIGN)
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Q2 Outreach"
    assert data["prospects"] == 1
    assert "campaign_id" in data
    assert data["status"] == "processing"


def test_create_campaign_alias(client):
    """POST /api/campaign (alias) should work identically."""
    with patch("src.main._process_campaign", new_callable=AsyncMock):
        resp = client.post("/api/campaign", json=SAMPLE_CAMPAIGN)
    assert resp.status_code == 200
    assert "campaign_id" in resp.json()


def test_get_campaign(client):
    """GET /api/v1/campaigns/{id} should return campaign details."""
    with patch("src.main._process_campaign", new_callable=AsyncMock):
        create_resp = client.post("/api/v1/campaigns", json=SAMPLE_CAMPAIGN)
    campaign_id = create_resp.json()["campaign_id"]

    resp = client.get(f"/api/v1/campaigns/{campaign_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["campaign_id"] == campaign_id
    assert data["prospects"] == 1


def test_get_campaign_not_found(client):
    resp = client.get("/api/v1/campaigns/nonexistent_id")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Email generation                                                             #
# --------------------------------------------------------------------------- #

def test_generate_email(client):
    """POST /api/v1/generate-email should return a structured email."""
    mock_email = {
        "subject": "Quick question about Acme Corp",
        "body": "Hi John, saw you recently raised Series B...",
        "personalization_score": 0.89,
    }
    with patch("src.main.research_prospect", new_callable=AsyncMock) as mock_research, \
         patch("src.main.generate_personalized_email", new_callable=AsyncMock, return_value=mock_email):
        mock_research.return_value = {
            "recent_news": "raised Series B",
            "pain_points": ["scaling"],
            "tech_stack": [],
        }
        resp = client.post("/api/v1/generate-email", json=SAMPLE_PROSPECT)

    assert resp.status_code == 200
    data = resp.json()
    assert data["prospect_email"] == "john.doe@acme.com"
    assert "subject" in data
    assert "body" in data
    assert 0.0 <= data["personalization_score"] <= 1.0


# --------------------------------------------------------------------------- #
# Stats                                                                        #
# --------------------------------------------------------------------------- #

def test_get_stats(client):
    resp = client.get("/api/v1/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "emails_sent_today" in data
    assert "reply_rate" in data
    assert "meetings_booked_today" in data
    assert "active_campaigns" in data
    assert "monthly_revenue" in data


# --------------------------------------------------------------------------- #
# Dashboard                                                                    #
# --------------------------------------------------------------------------- #

def test_dashboard(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Outreach Dashboard" in resp.text


# --------------------------------------------------------------------------- #
# Unsubscribe (CAN-SPAM / GDPR)                                               #
# --------------------------------------------------------------------------- #

def test_unsubscribe_get(client):
    """GET /unsubscribe should render HTML confirmation."""
    resp = client.get("/unsubscribe", params={"email": "unsub@example.com"})
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "unsubscribed" in resp.text.lower()


def test_unsubscribe_api(client):
    """POST /api/v1/unsubscribe should add email to suppression list."""
    resp = client.post(
        "/api/v1/unsubscribe",
        params={"email": "gdpr@example.com", "reason": "gdpr_request"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "unsubscribed"
    assert data["email"] == "gdpr@example.com"


def test_unsubscribe_idempotent(client):
    """Unsubscribing the same email twice should not raise an error."""
    email = "double@example.com"
    client.post("/api/v1/unsubscribe", params={"email": email})
    resp = client.post("/api/v1/unsubscribe", params={"email": email})
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Reply handling                                                               #
# --------------------------------------------------------------------------- #

def test_reply_webhook(client):
    resp = client.post(
        "/api/v1/reply-webhook",
        json={
            "from_email": "john.doe@acme.com",
            "subject": "Re: Quick question",
            "body": "Sounds interesting, let's chat.",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "reply recorded"
    assert data["prospect_email"] == "john.doe@acme.com"


# --------------------------------------------------------------------------- #
# Billing – plans                                                              #
# --------------------------------------------------------------------------- #

def test_billing_plans(client):
    resp = client.get("/api/v1/billing/plans")
    assert resp.status_code == 200
    data = resp.json()
    assert "starter" in data
    assert "pro" in data
    assert "agency" in data
    assert data["starter"]["monthly_price"] == 299.0
    assert data["pro"]["monthly_price"] == 599.0
    assert data["agency"]["monthly_price"] == 1499.0


def test_billing_checkout_unknown_plan(client):
    resp = client.post(
        "/api/v1/billing/checkout",
        json={
            "user_email": "test@example.com",
            "plan": "enterprise",
            "success_url": "https://example.com/success",
            "cancel_url": "https://example.com/cancel",
        },
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Process sequences trigger                                                    #
# --------------------------------------------------------------------------- #

def test_trigger_sequences(client):
    with patch("src.main._process_sequences", new_callable=AsyncMock):
        resp = client.post("/api/v1/process-sequences")
    assert resp.status_code == 200
    assert resp.json()["status"] == "sequence processing started"


# --------------------------------------------------------------------------- #
# AI service – unit tests (no real OpenAI calls)                              #
# --------------------------------------------------------------------------- #

def test_parse_email_content():
    """_parse_email_content should correctly extract subject and body."""
    from src.ai_service import _parse_email_content

    content = "Subject: Hello World\n\nHi there,\n\nThis is the body.\n\nBest,"
    subject, body = _parse_email_content(content, "John", "Acme", 1)
    assert subject == "Hello World"
    assert "Hi there" in body


def test_fallback_email():
    """_fallback_email should return a usable email dict."""
    from src.ai_service import _fallback_email

    result = _fallback_email("Jane", "Globex", {"recent_news": "expanded to Europe"}, step=1)
    assert result["subject"]
    assert "Jane" in result["body"]
    assert 0.0 <= result["personalization_score"] <= 1.0


def test_fallback_email_followup():
    """Follow-up steps should produce shorter emails."""
    from src.ai_service import _fallback_email

    result = _fallback_email("Jane", "Globex", {}, step=2)
    assert result["subject"]
    assert result["body"]
