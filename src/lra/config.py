from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Multi-label public suffixes, used for one decision only: whether the agent
# address sits on a subdomain of the firm's own domain. Getting this list wrong
# costs the operator one explicit FIRM_DOMAINS entry. It never decides whether
# an address is inside the firm, so a missing suffix can never silently
# misclassify a recipient.
_PUBLIC_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "com.au", "net.au", "org.au",
    "co.nz", "co.za", "co.jp", "or.jp", "com.br", "com.sg", "com.hk", "com.cn",
    "co.in", "com.mx", "co.il", "com.tr", "co.kr",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mail_provider: str = "console"
    # The one name the product goes by in a lawyer's inbox: the From display
    # name on every reply, and the name on the contact card we attach. It was
    # "Legal Review" here while the landing page said "Second Eye", so a
    # lawyer who signed up on one met a stranger in the other.
    mail_agent_name: str = "Second Eye"
    mail_agent_address: str = "review@example.com"
    # Other addresses that deliver to the agent (a firm's "legal@" forwarding
    # rule, say), comma separated. A message to one of these is addressed to
    # us; without the list it looks like a BCC and, with nothing attached, is
    # answered with silence.
    mail_agent_aliases: str = ""
    # The firm's own mail domains, comma separated. This is NOT derivable from
    # the agent address: the deployment plan puts the agent on a subdomain so
    # the firm's sending reputation stays insulated, which means every colleague
    # would otherwise look like an outside party.
    firm_domains: str = ""

    agentmail_api_key: str = ""
    agentmail_inbox_id: str = ""
    agentmail_webhook_secret: str = ""

    postmark_server_token: str = ""
    postmark_inbound_secret: str = ""

    anthropic_api_key: str = ""
    # Only for an API key that is not scoped to a workspace. The API refuses
    # such a key unless every request names the workspace to bill and log
    # against; a key created inside a workspace needs nothing here.
    anthropic_workspace_id: str = ""
    # Claude Fable 5.1, the most capable model available (Jim's call,
    # 2026-09-28). Thinking is always on and cannot be configured, so nothing
    # here sends a thinking block; a direct Messages API call names
    # refusal_fallback() so a classifier's decline is answered by Opus instead
    # of ending the request. The agents take their model from agents/*.yaml.
    review_model: str = "claude-fable-5-1"
    # No triage_model. There was one, pointing at a cheap model, and nothing
    # read it: every call went to review_model. A setting that advertises
    # two-tier routing the code does not do is worse than no setting, because
    # an operator tuning cost sets it and watches the bill not move. It comes
    # back when the triage path does (DECISIONS, open question 18).
    # For a firm whose Anthropic organisation is zero-data-retention. Fable
    # 5.1 is a Covered Model: it requires 30-day retention and answers a ZDR
    # organisation with a 400 on every request. True puts every model call and
    # every agent (`lra agents apply`) on ZDR_MODEL instead, the best current
    # model available under ZDR, whatever REVIEW_MODEL and agents/*.yaml say.
    # It covers the model only: a Managed Agents session's container, files
    # and memory stores are retained regardless (docs/sandbox.md).
    zero_retention: bool = False

    # --- Document management system ---
    dms_provider: str = "none"          # imanage | netdocuments | sharepoint | none
    dms_client_id: str = ""
    dms_client_secret: str = ""
    dms_token_url: str = ""
    dms_authorize_url: str = ""
    public_base_url: str = "http://localhost:8000"
    # The document system is served by cloudflare/dms-mcp, bound to one
    # matter per session (dms_mcp.py; docs/migration.md, phases 4 and 5).
    # The MCP server's endpoint, e.g. https://legal-review-agent-dms-mcp.<account>.workers.dev/mcp.
    # Every vault credential is keyed on it, so changing it means reconnecting.
    dms_mcp_url: str = ""
    # Shared with the MCP Worker (its DMS_MCP_SIGNING_KEY secret): signs each
    # session's (lawyer, matter, expiry) binding.
    dms_mcp_signing_key: str = ""
    # Where the binding travels. "query": on the session's MCP URL, with the
    # lawyer's own vault supplying the token. "capability": a vault made for
    # the one session holds a signed bearer carrying the token, for when the
    # vault turns out not to keep a query string; it needs our token table.
    dms_mcp_binding: Literal["query", "capability"] = "query"
    # How long a session's binding is good for.
    dms_mcp_binding_ttl_seconds: int = 21_600

    # --- Agent loop ---
    # A Literal, not a str: the value goes onto the agent definition as
    # model.effort, so a typo used to survive startup and then fail every
    # single review, which reaches the lawyer as "something went wrong on my
    # side". Now a bad value refuses to boot.
    agent_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    # Characters of document text sent to the agent in one review. Beyond this
    # the review is scoped and the reply says so, rather than quietly reviewing
    # the first third of a credit agreement and sounding confident about it.
    max_review_characters: int = 240_000
    # Wall-clock budget for a review session. DECISIONS 16 allows the lawyer to
    # wait five minutes, and the Workflow's wait for the session ends at this
    # budget plus the grace below. Past the budget the session is interrupted
    # and the model is
    # told to report with what it has, and the reply says the review was cut
    # short. The dollar budget below bounds spend; this bounds time.
    agent_time_budget_seconds: int = 300
    # How long, after being told to report now, the model gets to do so before
    # the session is interrupted for good and whatever was reported stands.
    agent_report_grace_seconds: int = 90

    # --- Anthropic Managed Agents (docs/agentic.md, docs/sandbox.md) ---
    # The review and the instruction agent are persisted agent definitions in
    # Anthropic's workspace, applied from agents/*.yaml by `lra agents apply`,
    # which prints these three ids. Every review is a session referencing them.
    # With any of the three empty, nothing model-backed can run.
    managed_review_agent_id: str = ""
    managed_associate_agent_id: str = ""
    managed_environment_id: str = ""
    # The playbook agent (agents/playbook.agent.yaml): reads the firm's
    # precedents and positions memos into position files. Optional; without it
    # playbook intake by email says it is not set up.
    managed_playbook_agent_id: str = ""
    # The comments agent (agents/comments.agent.yaml): turns the comments and
    # the other side's markup in a document (src/lra/comments.py). Optional;
    # without it "turn the comments" says it is not set up.
    managed_comments_agent_id: str = ""
    # The blackline agent (agents/blackline.agent.yaml): the Word comparison,
    # its PDF with a change summary, and a baseline named in words ("against
    # what we sent on Tuesday"). Optional; without it (or its skill) a
    # blackline request is an ordinary comparison, as before.
    managed_blackline_agent_id: str = ""
    # Before phase 5 (docs/migration.md) the clientless reviewer had an id of
    # its own. Read only by `lra agents apply`, which adopts that agent as
    # MANAGED_REVIEW_AGENT_ID and says to delete this line.
    managed_review_detached_agent_id: str = ""
    # The closing agent (agents/closing.agent.yaml): signature packets, signed
    # pages, the executed set and the closing checklist. Optional; without it
    # a closing email is told it is not set up.
    managed_closing_agent_id: str = ""
    # Hard cap on what one session may spend, in US cents at list price. The
    # platform pauses the session at the cap; we treat that like the time
    # budget and report what was found. 0 means no cap.
    managed_session_budget_cents: int = 800
    # Personal and matter memory live in Anthropic memory stores, one per
    # lawyer and one per matter, created on first use and recorded in our
    # database. The firm-wide store is one for the whole deployment; created by
    # `lra agents apply` and put here.
    managed_firm_memory_store_id: str = ""
    # This product's own skill, as uploaded by `lra skills sync`, which prints
    # the id to put here. Empty means the agents get Anthropic's skills only.
    sandbox_skill_id: str = ""
    # The firm's playbook, `skills/lra-playbook`, uploaded by
    # `lra skills sync lra-playbook`. Empty means no playbook is consulted.
    sandbox_playbook_skill_id: str = ""
    # The key-terms skill, `skills/lra-key-terms`, uploaded by
    # `lra skills sync lra-key-terms`. Empty means it is not attached.
    sandbox_key_terms_skill_id: str = ""
    # The closing tools, `skills/lra-closing`, uploaded by
    # `lra skills sync lra-closing`. Attached to the closing agent only.
    sandbox_closing_skill_id: str = ""
    # The comments skill, `skills/lra-comments`, uploaded by
    # `lra skills sync lra-comments`. Attached to the comments agent only.
    sandbox_comments_skill_id: str = ""
    # The negotiation ledger skill, `skills/lra-negotiation`, uploaded by
    # `lra skills sync lra-negotiation`. Empty means a new version from the
    # other side gets an ordinary review: nothing is read against our points.
    sandbox_negotiation_skill_id: str = ""
    # The blackline skill, `skills/lra-blackline`, uploaded by
    # `lra skills sync lra-blackline`. Attached to the blackline agent only.
    sandbox_blackline_skill_id: str = ""
    # A search query written from a privileged document discloses part of it,
    # so this is a decision, not a default. Applied to the agent definitions by
    # `lra agents apply`.
    web_search_enabled: bool = False
    # "us" or "global". Empty leaves routing to the API's default. Applied to
    # the agent definitions by `lra agents apply`.
    inference_geo: str = ""

    # --- Archive ---
    # Off by default. Storing privileged client correspondence needs a written
    # policy and the firm's sign-off before it is switched on. See docs/archive.md.
    archive_enabled: bool = False
    archive_blobs: bool = True          # keep attachment bytes, not just their text
    # Days archived mail is kept. 7, like everything else about a document
    # (THREAD_RETENTION_DAYS), so switching the archive on does not quietly
    # mean keeping mail for ever; a firm that wants longer says so here.
    # 0 keeps it indefinitely.
    archive_retention_days: int = 7

    allowed_senders: str = ""
    # Who a colleague should ask to be added, named in the refusal they get.
    # "Not on the list yet" with no next step is where a second user is lost.
    allowlist_contact: str = ""
    # Who may change the firm's playbook by email, comma separated addresses
    # or domains. A playbook is firm-wide: every later review measures every
    # lawyer's document against it, so it is not everyone's to rewrite. Empty
    # means the allowlist contact, or failing that anyone on FIRM_DOMAINS.
    playbook_admins: str = ""
    # The name on every tracked change and comment we write. A redline is
    # forwarded on as often as not, and "Legal Review Agent" on each revision
    # told the other side who, or what, marked up the draft. Firms set their
    # own (initials, a team name); the default says nothing about us.
    redline_author: str = "Reviewer"
    # One-tap answer links write to review+t<token>@. Off until the mail
    # domain accepts plus addresses (Cloudflare: subaddressing in Email
    # Routing), or every tapped reply is dropped without a word.
    one_tap_links: bool = True
    # Who a reply may go to (DECISIONS 31, policy.py). "sender_only": the
    # lawyer who sent the message, and nobody else, whatever any composer or
    # model decided; enforced where every message leaves. "model": the
    # recipients stand as composed (DECISIONS 30). A Literal, so a typo
    # refuses to boot rather than quietly meaning one or the other.
    reply_policy: Literal["sender_only", "model"] = "sender_only"
    # Who decides what an email asks for (triage.py, docs/migration.md phase
    # 3). "rules": the phrase lists and rules, as before. "shadow": the model
    # triages every email too, the rules still decide, and both plans go in
    # the audit log. "model": the model's plan is carried out. The allowlist,
    # the no-AI list and REPLY_POLICY hold in every mode.
    triage: Literal["rules", "shadow", "model"] = "shadow"
    # Clients whose guidelines exclude AI, comma separated: matter ids, client
    # names, or the client's mail domains (policy.py). A match gets no model
    # call at all, only the deterministic tools. Playbook admins add and lift
    # further entries by email ("no AI for Acme", "AI ok for Acme").
    no_ai_matters: str = ""
    # The legend on everything we send (policy.privileged): the last line of
    # the email, small and grey, and an X-Privileged header for the firm's
    # mail rules. Not the first line, which is the verdict a phone previews.
    # Empty sends none.
    privilege_notice: str = "Privileged & Confidential — Attorney Work Product"
    # The kill switch. True: the edge accepts and holds every message (no
    # bounce), the application reviews nothing and sends nothing, and what was
    # held is released when it is false again. See policy.paused.
    service_paused: bool = False
    max_attachment_mb: int = 25
    # Hours before a job row is deleted. 0 or less means keep it indefinitely;
    # it does not mean "store nothing", which is not achievable while webhook
    # retries have to be deduplicated against something. Enforced on the
    # inbound path, see store.claim().
    retention_hours: int = 24

    # `sqlite:///path` for a local file, or `d1://` for Cloudflare D1 reached
    # through the edge Worker (see d1.py). With `d1://`, documents go to R2.
    database_url: str = "sqlite:///./lra.sqlite3"
    # The Cloudflare Worker in front of this service, and the one secret that
    # opens its internal endpoints. Only used on Cloudflare. See edge.py.
    edge_url: str = ""
    edge_secret: str = ""
    # 32 random bytes, base64. Documents and OAuth tokens are encrypted with it
    # before they are stored. `lra keygen` makes one. The previous key is only
    # ever used to read, so a key can be rotated without losing what it sealed.
    data_key: str = ""
    data_key_previous: str = ""
    # Days anything about a document is kept after the conversation's last
    # activity (Jim's call, 2026-10-04: nothing kept after 7 days). It covers
    # the conversation and its document, every earlier version, the
    # negotiation ledger, a closing and its signed pages, notes the agent
    # proposed that nobody confirmed, and, at Anthropic, each job's review
    # sessions and uploads. Swept daily (retention.py, from the Worker's
    # cron). A firm may shorten it; 0 keeps everything indefinitely.
    thread_retention_days: int = 7
    # Whether undos, "B is fine" dismissals and the sent version read back
    # are recorded and turned into suppressions without the lawyer asking.
    # Off: nothing is learned unless a lawyer asks ("remember that", "stop
    # flagging X"), and no record of what was kept or dropped is stored.
    learn_from_outcomes: bool = False
    # Cloudflare carries 5 MiB per message, or 25 to an address verified in
    # the account. Raise this only if every lawyer's address is verified.
    cloudflare_max_message_mb: int = 5
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _firm_domains_must_be_set_on_a_subdomain(self) -> "Settings":
        """Refuse to start rather than guess which domain is the firm.

        PLAN phase 4 puts the agent on a subdomain (review@legal.firm.com) so
        the firm's sending reputation stays insulated. With FIRM_DOMAINS left
        blank, every colleague on firm.com was an outside party: an internal
        circulation came back as the red incident report saying privileged
        material had already left the building. That fires on essentially every
        internal draft, and a check that is alarming and wrong is the one that
        gets a filter rule in Outlook.

        We do not derive the parent domain instead, because the same address
        shape occurs on shared mail hosts (review@firm.somemailhost.com), where
        deriving it would make every other tenant look like a colleague and
        quietly disable the wrong-recipient blocker. A wrong guess there is
        silent; this failure is one sentence at boot.
        """
        if self.firm_domains.strip():
            return self
        agent_domain = self.mail_agent_address.lower().split("@")[-1].strip()
        parent = agent_domain.split(".", 1)[1] if "." in agent_domain else ""
        if "." in parent and parent not in _PUBLIC_SUFFIXES:
            raise ValueError(
                f"MAIL_AGENT_ADDRESS is on the subdomain {agent_domain!r} and "
                "FIRM_DOMAINS is empty, so every colleague would be treated as "
                "an outside party and every internal draft would come back as "
                f"an incident report. Set FIRM_DOMAINS={parent} (comma separated "
                "if the firm has more than one mail domain)."
            )
        return self

    @property
    def internal_domains(self) -> set[str]:
        """Every domain that counts as inside the firm."""
        agent_domain = self.mail_agent_address.lower().split("@")[-1]
        domains = {d.strip().lower() for d in self.firm_domains.split(",") if d.strip()}
        domains.add(agent_domain)
        # A subdomain of a firm domain is still the firm.
        domains.update(
            d for d in {agent_domain} if any(d.endswith("." + f) for f in domains)
        )
        # Deliberately NOT the allowlist. A firm adds co-counsel's domain so
        # those lawyers can use the agent; treating that domain as inside the
        # firm silently disabled the wrong-recipient blocker for every document
        # sent to them, which is one of the checks the product is sold on.
        return domains

    @property
    def effective_model(self) -> str:
        """The model every direct call names: REVIEW_MODEL, or ZDR_MODEL when
        ZERO_RETENTION is on."""
        return ZDR_MODEL if self.zero_retention else self.review_model

    def is_internal(self, address: str) -> bool:
        domain = address.lower().split("@")[-1]
        return any(domain == d or domain.endswith("." + d) for d in self.internal_domains)

    @property
    def allowlist(self) -> list[str]:
        return [s.strip().lower() for s in self.allowed_senders.split(",") if s.strip()]


@lru_cache
def settings() -> Settings:
    return Settings()


# Claude Fable 5.1 runs safety classifiers aimed at biology and cyber work that
# can occasionally decline a benign request; the decline is an HTTP 200 with
# stop_reason "refusal". A contract is not either, but a false positive on one
# would otherwise end the call. The server-side fallback reruns a declined
# request on this model in the same round trip. Opus 5 is one of the two
# targets the API accepts.
FALLBACK_MODEL = "claude-opus-5"
FALLBACK_BETA = "server-side-fallback-2026-06-01"

# The model ZERO_RETENTION puts everything on. Claude Fable 5.1 (and Fable 5)
# require 30-day retention; Claude Opus 5 is available under zero data
# retention, and is the most capable model documented as such. Claude Opus 5.5
# may be too, but its launch documentation says nothing about ZDR, so it is not
# assumed. Opus 5 runs no refusal fallback (refusal_fallback returns nothing
# for it), and a decline there ends the call as it did before Fable.
ZDR_MODEL = "claude-opus-5"


def refusal_fallback(model: str) -> dict:
    """Keyword arguments for a `client.beta.messages` call on `model`, opting
    into the server-side fallback where the model has one. Empty for any
    other model, so the call is unchanged there."""
    if model.startswith(("claude-fable-5-1", "claude-mythos-5-1")):
        return {"betas": [FALLBACK_BETA], "fallbacks": [{"model": FALLBACK_MODEL}]}
    return {}


def anthropic_client():
    """The one way this codebase builds a client, so that credentials and the
    workspace header are decided in one place rather than five."""
    import anthropic

    from lra import policy

    # A no-AI client's work (policy.py) never reaches the API, however the
    # call was arrived at.
    policy.require_ai_allowed()
    cfg = settings()
    kwargs: dict = {"api_key": cfg.anthropic_api_key or None}
    workspace = cfg.anthropic_workspace_id.strip()
    if workspace:
        kwargs["default_headers"] = {"anthropic-workspace-id": workspace}
    return anthropic.Anthropic(**kwargs)
