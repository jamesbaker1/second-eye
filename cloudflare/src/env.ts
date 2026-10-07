import type { ReviewContainer } from "./index";
import type { FlowParams } from "./review-flow";

export interface Env {
  DB: D1Database;
  DOCS: R2Bucket;
  EMAIL: { send(message: unknown): Promise<{ messageId: string }> };
  REVIEW: DurableObjectNamespace<ReviewContainer>;
  REVIEW_FLOW: Workflow<FlowParams>;
  EDGE_SECRET: string;
  EDGE_URL: string;
  DATA_KEY: string;
  DATA_KEY_PREVIOUS?: string;
  MAIL_AGENT_ADDRESS?: string;
  MAIL_AGENT_NAME?: string;
  ONE_TAP_LINKS?: string;
  TRIAGE?: string;
  ALLOWED_SENDERS?: string;
  MAX_EMAILS_PER_DAY?: string;
  DMS_PROVIDER?: string;
  /** "true": the kill switch. Mail is held, nothing is sent (mail.ts). */
  SERVICE_PAUSED?: string;
  /** "sender_only" (the default) or "model" (outbox.ts, DECISIONS 31). */
  REPLY_POLICY?: string;
  /** The whsec_ secret from Console -> Manage -> Webhooks. */
  ANTHROPIC_WEBHOOK_SIGNING_KEY?: string;
  /** Opens signed /internal/* from outside while set (operator.ts). Never
   *  forwarded to the container. Unset, the path does not exist. */
  OPERATOR_SECRET?: string;
  /** Firm domains with no DMARC record, admitted on an aligned DKIM pass (auth.ts). */
  DKIM_ONLY_DOMAINS?: string;
  /** "allowlist" (default) or "open": what the container reaches (index.ts, egress()). */
  CONTAINER_EGRESS?: string;
  /** Hosts beyond api.anthropic.com the container may reach in "allowlist" mode. */
  EGRESS_ALLOWED_HOSTS?: string;
  DMS_TOKEN_URL?: string;
  [name: string]: unknown;
}

/** A sealed message waiting in R2 for its ReviewFlow instance. */
export interface Pointer {
  key: string;
  receivedAt: string;
}
