import { ArrowUpRight } from "lucide-react";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { AppConfig } from "../api/types";
import { CHANNEL_LABEL } from "../lib/format";

interface Policy {
  seller?: string;
  terms_offered?: string;
  verification?: { reference_min_age_days?: number; domain_min_age_days?: number; flag_on_warnings?: number };
  reference_score?: { flag_below?: number; genuine_at?: number; high_fraud_score?: number };
  applicant?: { min_years_in_business?: number; require_signed_authorization?: boolean };
  limits?: {
    min_completed_references?: number;
    fraction_of_avg_high_credit?: number;
    slow_pay_grace_days?: number;
    slow_pay_fraction?: number;
    round_down_to?: number;
    cap_at_requested?: boolean;
    senior_review_above?: number;
  };
  bank?: { max_nsf_last_12mo?: number };
}

function Integration({
  title,
  state,
  ok,
  children,
}: {
  title: string;
  state: string;
  ok: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="integration">
      <div className="row gap-12">
        <h3>{title}</h3>
        <span className="spacer" />
        <span className={`pill ${ok ? "ink" : "outline"}`}>{state}</span>
      </div>
      <div className="integration-body">{children}</div>
    </div>
  );
}

function Env({ children }: { children: React.ReactNode }) {
  return <code className="env">{children}</code>;
}

const pct = (v?: number) => (v === undefined ? "" : `${Math.round(v * 100)}%`);
const usd = (v?: number) => (v === undefined ? "" : `$${v.toLocaleString("en-US")}`);

export default function Settings() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [params] = useSearchParams();
  useEffect(() => {
    api.config().then(setConfig).catch(() => undefined);
    api
      .policy()
      .then((p) => setPolicy(p as Policy))
      .catch(() => undefined);
  }, []);

  return (
    <main className="page settings">
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="sub">What Vouch is connected to. Change these in the .env file at the root of the project, then restart.</p>
        </div>
      </div>
      {params.get("quickbooks") === "connected" ? <p className="notice-ok">QuickBooks is connected.</p> : null}

      {config ? (
        <div className="integrations">
          <Integration
            title="Claude"
            ok={config.llm.mode === "anthropic"}
            state={config.llm.mode === "anthropic" ? "Live" : "Offline, scripted"}
          >
            <p>
              Reads applications with {config.llm.model_smart} and talks on calls with {config.llm.model_fast}.
            </p>
            {config.llm.mode !== "anthropic" ? (
              <p className="muted">
                Set <Env>ANTHROPIC_API_KEY</Env> to read your own files and hold real conversations. Without it, the bundled
                samples run on scripted responses.
              </p>
            ) : null}
          </Integration>

          <Integration
            title="Reference calls"
            ok={config.calls.twilio_configured}
            state={CHANNEL_LABEL[config.calls.default_channel]}
          >
            <p>
              New applications call references through the {CHANNEL_LABEL[config.calls.default_channel].toLowerCase()}, up to{" "}
              {config.calls.max_attempts} attempts, {Math.round(config.calls.retry_seconds)} seconds apart.
            </p>
            <p className="muted">
              Real calls need <Env>TWILIO_ACCOUNT_SID</Env>, <Env>TWILIO_AUTH_TOKEN</Env>, <Env>TWILIO_FROM_NUMBER</Env>, and a
              public <Env>PUBLIC_BASE_URL</Env> (for example from cloudflared). Set <Env>CALL_CHANNEL=twilio</Env> to make it the
              default, and <Env>DEMO_REFERENCE_PHONE</Env> to route every call to one phone during a demo.
            </p>
            <p className="muted">
              Twilio {config.calls.twilio_configured ? "is configured" : "is not configured"}
              {config.calls.public_base_url ? `, public URL ${config.calls.public_base_url}` : ", no public URL set"}.
            </p>
          </Integration>

          <Integration title="Bank email" ok={config.email.mode === "smtp"} state={config.email.mode === "smtp" ? "SMTP" : "Outbox"}>
            <p>
              {config.email.mode === "smtp"
                ? "Bank reference requests go out by email."
                : "Bank reference requests are kept in the app's outbox instead of being sent."}
            </p>
            <p className="muted">
              Set <Env>EMAIL_MODE=smtp</Env> with <Env>SMTP_HOST</Env>, <Env>SMTP_USERNAME</Env>, <Env>SMTP_PASSWORD</Env> to send
              them. Replies come in through <Env>POST /api/email/inbound</Env>.
              {config.email.simulate_bank_reply_seconds > 0
                ? ` In outbox mode the sample banks reply on their own after ${Math.round(config.email.simulate_bank_reply_seconds)} seconds.`
                : ""}
            </p>
          </Integration>

          <Integration
            title="Public records"
            ok={config.registry.opencorporates}
            state={config.registry.mode === "opencorporates" ? "OpenCorporates" : "Sandbox"}
          >
            <p>
              Business registrations come from {config.registry.mode === "opencorporates" ? "OpenCorporates" : "sandbox records for the sample companies"}.
              Domain ages {config.registry.live_domain_checks ? "come from live RDAP lookups" : "use sandbox data only"}.
            </p>
            <p className="muted">
              Set <Env>REGISTRY_MODE=opencorporates</Env> and <Env>OPENCORPORATES_API_TOKEN</Env> to check real companies.
            </p>
          </Integration>

          <Integration
            title="Phone checks"
            ok={config.phone_checks.ipqs || config.phone_checks.twilio || config.phone_checks.google}
            state={
              [config.phone_checks.ipqs && "IPQualityScore", config.phone_checks.twilio && "Twilio Lookup", config.phone_checks.google && "Google Places"]
                .filter(Boolean)
                .join(", ") || "Sandbox"
            }
          >
            <p>
              Each trade reference gets an authenticity score from 1 to 99. Part of it comes from who the reference's phone number
              really belongs to: IPQualityScore's fraud model, carrier caller ID and line type from Twilio Lookup, and which business
              Google lists the number for. The sample applications use built-in data for these.
            </p>
            <p className="muted">
              Add <Env>IPQS_API_KEY</Env> (1,000 free lookups), <Env>GOOGLE_MAPS_API_KEY</Env> with Places API (New) enabled, or Twilio
              credentials to check real numbers. Weights are in the credit policy below.
            </p>
          </Integration>

          <Integration
            title="Accounting"
            ok={config.accounting.mode === "quickbooks" && config.accounting.connected}
            state={
              config.accounting.mode === "quickbooks"
                ? config.accounting.connected
                  ? "QuickBooks connected"
                  : "QuickBooks not connected"
                : "Local ledger"
            }
          >
            <p>
              {config.accounting.mode === "quickbooks"
                ? "Approved customers are created in QuickBooks Online with their payment terms. The QuickBooks API cannot set a credit limit, so Vouch writes the approved limit into the customer notes."
                : "Approved customers are written to data/ledger.json."}
            </p>
            {config.accounting.mode === "quickbooks" && !config.accounting.connected ? (
              <a className="btn small" href="/api/integrations/quickbooks/connect">
                Connect QuickBooks <ArrowUpRight size={14} />
              </a>
            ) : null}
            {config.accounting.mode !== "quickbooks" ? (
              <p className="muted">
                Set <Env>ACCOUNTING_MODE=quickbooks</Env>, <Env>QUICKBOOKS_CLIENT_ID</Env>, and <Env>QUICKBOOKS_CLIENT_SECRET</Env> to
                use a QuickBooks sandbox company.
              </p>
            ) : null}
          </Integration>
        </div>
      ) : null}

      {policy ? (
        <section className="section">
          <div className="section-head">
            <div>
              <h2>Credit policy</h2>
              <p className="sub">
                The rules Vouch applies for {policy.seller}. They live in backend/vouch/policy/credit_policy.yaml.
              </p>
            </div>
          </div>
          <div className="policy-grid">
            <div className="card">
              <h3>Setting the limit</h3>
              <ol className="policy-list">
                <li>
                  Needs at least {policy.limits?.min_completed_references} trade references that answer the questions.
                </li>
                <li>Starts from {pct(policy.limits?.fraction_of_avg_high_credit)} of the average high credit they extend.</li>
                <li>
                  Cuts that to {pct(policy.limits?.slow_pay_fraction)} if the applicant pays them more than{" "}
                  {policy.limits?.slow_pay_grace_days} days late on average.
                </li>
                <li>
                  Rounds down to the nearest {usd(policy.limits?.round_down_to)}
                  {policy.limits?.cap_at_requested ? " and never goes above the amount requested." : "."}
                </li>
                <li>Sends anything above {usd(policy.limits?.senior_review_above)} to a senior credit manager.</li>
                <li>Offers {policy.terms_offered} terms.</li>
              </ol>
            </div>
            <div className="card">
              <h3>Flagging a reference</h3>
              <ol className="policy-list">
                <li>Its phone number is the applicant's own or another reference's, or caller ID or Google lists it to the applicant.</li>
                <li>It is listed at the applicant's address, or shares an officer with the applicant.</li>
                <li>It was registered less than {policy.verification?.reference_min_age_days} days ago, or is not active.</li>
                <li>
                  IPQualityScore rates its number {policy.reference_score?.high_fraud_score} or higher for fraud, or Vouch saw the number
                  for a different company before.
                </li>
                <li>
                  Otherwise, its authenticity score is under {policy.reference_score?.flag_below}. Softer findings like a new website
                  domain, a free email address, or a virtual phone number lower the score; caller ID, a business listing, or a long
                  registration raise it. Scores of {policy.reference_score?.genuine_at} and up read as likely genuine.
                </li>
                <li>Flagged references are never called and are left out of the limit.</li>
              </ol>
            </div>
            <div className="card">
              <h3>The applicant and the bank</h3>
              <ol className="policy-list">
                <li>
                  The application must be signed with authorization to contact references
                  {policy.applicant?.require_signed_authorization ? "" : " (not required)"}.
                </li>
                <li>Applicants in business under {policy.applicant?.min_years_in_business} years get a warning.</li>
                <li>
                  More than {policy.bank?.max_nsf_last_12mo} NSF items in a year, or an unsatisfactory rating, sends the file to a
                  credit manager.
                </li>
              </ol>
            </div>
          </div>
        </section>
      ) : null}
    </main>
  );
}
