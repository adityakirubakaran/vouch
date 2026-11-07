// Mirrors backend/vouch/api/serialize.py. Keep the two in sync.

export type AppStatus =
  | "received"
  | "reading"
  | "verifying"
  | "checking_references"
  | "deciding"
  | "ready_for_review"
  | "approved"
  | "synced"
  | "declined"
  | "info_requested"
  | "failed";

export type DecisionCode = "approve" | "approve_reduced" | "request_info" | "manual_review" | "decline";

export type StepKind = "extract" | "verify" | "call" | "bank" | "memo" | "sync";
export type StepStatus = "pending" | "running" | "waiting" | "done" | "failed" | "skipped";

export type RefStatus =
  | "pending"
  | "verified"
  | "flagged"
  | "queued"
  | "calling"
  | "retry_scheduled"
  | "callback_scheduled"
  | "completed"
  | "declined"
  | "wrong_number"
  | "unreachable"
  | "emailed"
  | "no_reply";

export type CallChannel = "simulated" | "browser" | "twilio";
export type CallStatus = "queued" | "ringing" | "in_progress" | "ended";
export type CallOutcome =
  | "completed"
  | "callback_requested"
  | "declined"
  | "wrong_number"
  | "voicemail"
  | "no_answer"
  | "failed";

export interface Address {
  street: string | null;
  city: string | null;
  state: string | null;
  postal_code: string | null;
}

export interface CreditApplication {
  applicant: {
    legal_name: string;
    dba: string | null;
    phone: string | null;
    email: string | null;
    website: string | null;
    address: Address;
    entity_type: string | null;
    state_of_incorporation: string | null;
    ein: string | null;
    years_in_business: number | null;
    annual_revenue: number | null;
    principal_name: string | null;
    principal_title: string | null;
  };
  requested_limit: number | null;
  requested_terms: string | null;
  bank: {
    bank_name: string;
    contact_name: string | null;
    phone: string | null;
    email: string | null;
    account_last4: string | null;
  } | null;
  trade_references: {
    company: string;
    contact_name: string | null;
    phone: string | null;
    email: string | null;
    address: Address;
  }[];
  signature: {
    signed: boolean;
    signer_name: string | null;
    signer_title: string | null;
    date: string | null;
    authorizes_reference_checks: boolean;
  };
  issues: { field: string; issue: "illegible" | "missing" | "ambiguous"; note: string | null }[];
}

export interface Signal {
  rule: string;
  severity: "info" | "warn" | "critical";
  message: string;
}

export interface CheckRow {
  label: string; // "Registered" | "Phone number" | "Website age"
  value: string;
  ok: boolean | null; // null = not checked
}

export interface PartyChecks {
  registry: {
    found: boolean;
    legal_name: string | null;
    company_number: string | null;
    jurisdiction: string | null;
    status: string | null;
    incorporated_on: string | null;
    age_days: number | null;
    registered_address: string | null;
    officers: string[];
    source: string;
    source_url: string | null;
  };
  domain: { domain: string; found: boolean; created_on: string | null; age_days: number | null; source: string } | null;
  website: { url: string | null; checked: boolean; phones: string[]; source: string } | null;
  signals: Signal[];
  rows?: CheckRow[]; // references only
}

export interface ReferenceFindings {
  customer_since: string | null;
  terms: string | null;
  high_credit: number | null;
  current_balance: number | null;
  past_due_amount: number | null;
  avg_days_to_pay: number | null;
  pays_within_terms: boolean | null;
  would_extend_more_credit: boolean | null;
  comments: string | null;
}

export interface BankFindings {
  account_open_since: string | null;
  average_balance: string | null;
  nsf_or_overdrafts_last_12mo: number | null;
  has_loans_or_lines: boolean | null;
  loans_in_good_standing: boolean | null;
  relationship_rating: "satisfactory" | "unsatisfactory" | "unknown" | null;
  summary: string;
}

export interface TranscriptTurn {
  role: "agent" | "reference";
  text: string;
  ts: string;
  interrupted?: boolean;
}

export interface CallResult {
  outcome: CallOutcome;
  findings: ReferenceFindings;
  callback_at: string | null;
  alternate_contact: string | null;
  summary: string;
}

export interface CallOut {
  id: string;
  attempt: number;
  channel: CallChannel;
  dialed_number: string | null;
  status: CallStatus;
  outcome: CallOutcome | null;
  transcript: TranscriptTurn[];
  live_findings: ReferenceFindings | null;
  result: CallResult | null;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  ring_deadline: string | null;
}

export interface StepOut {
  id: string;
  kind: StepKind;
  reference_id: string | null;
  status: StepStatus;
  attempts: number;
  call_attempts: number | null;
  wait_reason: string | null;
  next_run_at: string | null; // set when a retry is scheduled
  timeout_at: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface ReferenceOut {
  id: string;
  kind: "trade" | "bank";
  position: number;
  company: string;
  contact_name: string | null;
  phone: string | null; // E.164
  phone_display: string | null;
  email: string | null;
  status: RefStatus;
  flagged: boolean;
  checks: PartyChecks | null;
  findings: (ReferenceFindings & Partial<BankFindings>) | null;
  outcome: string | null;
  summary: string | null;
  calls: CallOut[];
  step: StepOut | null;
}

export interface EvidenceOut {
  code: string; // e.g. "call-ref-1", "bank", "reg-applicant", "fraud-ref-3", "app"
  kind: "call" | "bank" | "registry" | "domain" | "fraud" | "application";
  label: string; // short chip label: "Call 1", "Bank email", "State records", "Fraud check"
  detail: string;
  source_id: string | null; // call id, email id, or reference id
}

export interface EmailOut {
  id: string;
  reference_id: string | null;
  direction: "outbound" | "inbound";
  to_addr: string | null;
  from_addr: string | null;
  subject: string;
  body: string;
  delivery: "outbox" | "smtp" | "simulated" | "webhook";
  created_at: string;
}

export interface MemoPoint {
  text: string;
  evidence_ids: string[]; // EvidenceOut.code values
}

export interface DecisionDetail {
  decision: DecisionCode;
  reason: string;
  recommended_limit: number | null;
  requested_limit: number | null;
  terms: string;
  math: string[]; // how the limit was computed, one line per step
  missing: string[]; // what to ask the applicant for (request_info)
  verified_references: number;
  flagged_references: number;
  facts: { id: string; text: string }[];
  risks: { id: string; text: string }[];
  narrative: { headline: string; points: MemoPoint[]; risks: MemoPoint[] };
  narrative_source: "anthropic" | "fake" | "template";
  generated_at: string;
}

export interface Review {
  action: "approved" | "declined" | "info_requested";
  limit?: number;
  terms?: string;
  note?: string | null;
  reasons?: string[];
  notice?: string; // adverse action notice draft (declined)
  email_id?: string;
  reviewer: string;
  at: string;
}

export interface SyncResult {
  ok: boolean;
  provider: "ledger" | "quickbooks";
  customer_id?: string;
  url?: string | null;
  error?: string;
  synced_at: string;
}

export interface ApplicationSummary {
  id: string;
  created_at: string;
  updated_at: string;
  status: AppStatus;
  applicant_name: string | null;
  requested_limit: number | null;
  requested_terms: string | null;
  decision: DecisionCode | null;
  recommended_limit: number | null;
  approved_limit: number | null;
  source_filename: string;
  sample_id: string | null;
  reference_count: number;
  flagged_references: number;
  completed_references: number;
}

export interface ApplicationDetail extends ApplicationSummary {
  extracted: CreditApplication | null;
  applicant_checks: PartyChecks | null;
  decision_detail: DecisionDetail | null;
  review: Review | null;
  sync: SyncResult | null;
  error: string | null;
  references: ReferenceOut[]; // trade references, in form order
  bank: ReferenceOut | null;
  steps: StepOut[];
  evidence: EvidenceOut[];
  emails: EmailOut[];
  document_url: string;
  llm_mode: "anthropic" | "fake";
}

export interface ActivityEvent {
  id?: number;
  ts: string;
  type: string;
  message: string;
  data: Record<string, unknown>;
  application_id?: string | null;
}

// Live events from GET /api/stream?application_id=...
// Notable types: application.status, step.updated, call.created, call.ringing,
// call.updated, call.token {call_id, token}, call.transcript {call_id, role, text},
// call.findings {call_id, findings}, call.ended, email.sent, email.received,
// verification.done, memo.ready, application.approved, application.synced.
export type LiveEvent = ActivityEvent;

export interface RingingCall {
  id: string;
  application_id: string;
  reference_company: string | null;
  contact_name: string | null;
  applicant: string | null;
  seller: string;
  dialed_number: string | null;
  ring_deadline: string | null;
}

export interface SampleInfo {
  id: string;
  title: string;
  description: string;
  available: boolean;
  url: string;
}

export interface AppConfig {
  seller_name: string;
  llm: { mode: "anthropic" | "fake"; model_smart: string; model_fast: string };
  calls: {
    default_channel: CallChannel;
    configured_channel: CallChannel;
    twilio_configured: boolean;
    public_base_url: string | null;
    demo_reference_phone: boolean;
    max_attempts: number;
    retry_seconds: number;
  };
  email: { mode: "outbox" | "smtp"; simulate_bank_reply_seconds: number };
  registry: { mode: "sandbox" | "opencorporates"; opencorporates: boolean; live_domain_checks: boolean };
  accounting: { mode: "ledger" | "quickbooks"; connected: boolean; realm_id?: string | null; environment?: string };
}
