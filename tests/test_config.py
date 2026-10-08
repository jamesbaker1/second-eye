"""Configuration that has to be wrong loudly rather than quietly.

Two settings decide things a lawyer sees. Firm membership decides whether a
colleague is an outside party, which decides whether an internal draft comes
back as a review or as an incident report. Agent effort goes straight to the
model API, so a typo means every review fails.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from secondeye.config import Settings, settings


@pytest.fixture(autouse=True)
def _clear():
    settings.cache_clear()
    yield
    settings.cache_clear()


# --- who is inside the firm -----------------------------------------------

def test_a_colleague_on_the_apex_domain_is_internal_when_the_agent_is_on_a_subdomain():
    cfg = Settings(mail_agent_address="review@legal.firm.com", firm_domains="firm.com")
    assert cfg.is_internal("colleague@firm.com")
    assert cfg.is_internal("associate@mail.firm.com")
    assert not cfg.is_internal("counsel@acme.com")


def test_a_subdomain_agent_address_without_firm_domains_refuses_to_start():
    """The deployment PLAN phase 4 prescribes, misconfigured.

    review@legal.firm.com with FIRM_DOMAINS blank made every colleague an
    outside party, so an ordinary internal circulation came back as the red
    "this has already left the firm" incident report. Better to fail at boot,
    where one sentence fixes it, than to be alarming and wrong on every
    internal draft.
    """
    with pytest.raises(ValidationError) as e:
        Settings(mail_agent_address="review@legal.firm.com", firm_domains="")
    assert "FIRM_DOMAINS=firm.com" in str(e.value)


def test_an_apex_agent_address_needs_no_firm_domains():
    cfg = Settings(mail_agent_address="review@firm.com", firm_domains="")
    assert cfg.is_internal("colleague@firm.com")


def test_a_country_code_apex_domain_is_not_mistaken_for_a_subdomain():
    """firm.co.uk is an apex domain, not review@legal.firm.com."""
    cfg = Settings(mail_agent_address="review@firm.co.uk", firm_domains="")
    assert cfg.is_internal("colleague@firm.co.uk")


def test_an_allowed_sender_domain_is_not_treated_as_inside_the_firm():
    """Co-counsel may use the agent without becoming part of the firm.

    Folding the allowlist into firm membership emptied external_recipients for
    those domains, which silently disabled the wrong-recipient blocker for the
    documents most likely to need it.
    """
    cfg = Settings(
        mail_agent_address="review@firm.com",
        firm_domains="firm.com",
        allowed_senders="cocounsel.com",
    )
    assert not cfg.is_internal("partner@cocounsel.com")


# --- values that reach the model API --------------------------------------

def test_an_invalid_agent_effort_is_refused_at_startup_not_per_email():
    """AGENT_EFFORT is passed through to output_config verbatim.

    As a bare str, "higher" survived startup and then 400'd on every single
    review, which reaches the lawyer as "something went wrong on my side".
    """
    with pytest.raises(ValidationError):
        Settings(mail_agent_address="review@firm.com", agent_effort="higher")

    assert Settings(mail_agent_address="review@firm.com", agent_effort="max").agent_effort == "max"
