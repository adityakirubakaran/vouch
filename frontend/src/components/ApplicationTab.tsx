import type { ApplicationDetail, Address } from "../api/types";
import { money } from "../lib/format";

function addr(a: Address | undefined | null): string {
  if (!a) return "";
  return [a.street, a.city, [a.state, a.postal_code].filter(Boolean).join(" ")].filter(Boolean).join(", ");
}

function Rows({ rows }: { rows: [string, string | number | null | undefined][] }) {
  return (
    <div className="kv">
      {rows.flatMap(([label, value]) => [
        <div key={`${label}-l`}>{label}</div>,
        <div key={`${label}-v`}>{value === null || value === undefined || value === "" ? <span className="faint">Blank</span> : value}</div>,
      ])}
    </div>
  );
}

export default function ApplicationTab({ app }: { app: ApplicationDetail }) {
  const x = app.extracted;
  const isImage = /\.(png|jpe?g|webp)$/i.test(app.source_filename);
  return (
    <div id="application" className="application-tab">
      <div className="stack gap-24">
        {!x ? (
          <p className="muted">{app.status === "failed" ? app.error : "Vouch is reading the application."}</p>
        ) : (
          <>
            {x.issues.length > 0 ? (
              <div className="card issues">
                <h3>Double-check these fields</h3>
                <ul>
                  {x.issues.map((i) => (
                    <li key={i.field}>
                      {i.field.replace(/\./g, " ")}: {i.issue}
                      {i.note ? `, ${i.note}` : ""}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            <section>
              <h3 className="sec-title">Applicant</h3>
              <Rows
                rows={[
                  ["Legal name", x.applicant.legal_name],
                  ["DBA", x.applicant.dba],
                  ["Entity", [x.applicant.entity_type, x.applicant.state_of_incorporation].filter(Boolean).join(", ")],
                  ["EIN", x.applicant.ein],
                  ["Address", addr(x.applicant.address)],
                  ["Phone", x.applicant.phone],
                  ["Email", x.applicant.email],
                  ["Website", x.applicant.website],
                  ["Years in business", x.applicant.years_in_business],
                  ["Annual revenue", x.applicant.annual_revenue ? money(x.applicant.annual_revenue) : null],
                  ["Principal", [x.applicant.principal_name, x.applicant.principal_title].filter(Boolean).join(", ")],
                ]}
              />
            </section>
            <section>
              <h3 className="sec-title">Request</h3>
              <Rows
                rows={[
                  ["Credit limit", x.requested_limit ? money(x.requested_limit) : null],
                  ["Terms", x.requested_terms],
                ]}
              />
            </section>
            {x.bank ? (
              <section>
                <h3 className="sec-title">Bank</h3>
                <Rows
                  rows={[
                    ["Bank", x.bank.bank_name],
                    ["Contact", x.bank.contact_name],
                    ["Phone", x.bank.phone],
                    ["Email", x.bank.email],
                    ["Account", x.bank.account_last4 ? `Ending ${x.bank.account_last4}` : null],
                  ]}
                />
              </section>
            ) : null}
            {x.trade_references.map((r, i) => (
              <section key={i}>
                <h3 className="sec-title">Trade reference {i + 1}</h3>
                <Rows
                  rows={[
                    ["Company", r.company],
                    ["Contact", r.contact_name],
                    ["Phone", r.phone],
                    ["Email", r.email],
                    ["Address", addr(r.address)],
                  ]}
                />
              </section>
            ))}
            <section>
              <h3 className="sec-title">Signature</h3>
              <Rows
                rows={[
                  ["Signed", x.signature.signed ? "Yes" : "No"],
                  ["Signer", [x.signature.signer_name, x.signature.signer_title].filter(Boolean).join(", ")],
                  ["Date", x.signature.date],
                  ["Authorizes reference checks", x.signature.authorizes_reference_checks ? "Yes" : "No"],
                ]}
              />
            </section>
          </>
        )}
      </div>
      <div className="doc-preview">
        <div className="row" style={{ marginBottom: 10 }}>
          <span className="muted small">{app.source_filename}</span>
          <span className="spacer" />
          <a className="link-btn" href={app.document_url} target="_blank" rel="noreferrer">
            Open
          </a>
        </div>
        {isImage ? (
          <img src={app.document_url} alt="Uploaded credit application" />
        ) : (
          <iframe title="Uploaded credit application" src={`${app.document_url}#view=FitH&toolbar=0`} />
        )}
      </div>
    </div>
  );
}
