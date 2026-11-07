"""The credit check workflow: which steps exist, what each one does, and what
runs next.

    extract ──► verify ──► call(ref 1) ─┐
       │                   call(ref 2) ─┼──► memo ──► (human approves) ──► sync
       └──────► bank (email, wait) ─────┘

verify decides which references get called: flagged references are excluded.
Calls and the bank request can wait minutes or days (no answer, callback
requested, bank has not replied); the engine persists those waits.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from sqlalchemy import select

from .agents import bank as bank_agent
from .agents import decision as decision_agent
from .agents import verification as ver
from .agents.extraction import extract_application, media_type_for
from .db import Application, Call, Email, Evidence, Reference, Step, utcnow
from .engine import Done, Retry, Skip, StepContext, StepFailed, Wait
from .fixtures_data import bank_reply_for
from .policy import load_policy
from .schemas import BankFindings, CallResult, CreditApplication, ReferenceFindings
from .util import format_phone, money, normalize_phone

if TYPE_CHECKING:  # pragma: no cover
    from .services import Services


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


log = logging.getLogger("vouch.workflow")

MAX_CALL_SECONDS = 600


class Workflow:
    def __init__(self, services: "Services") -> None:
        self.s = services
        services.workflow = self
        self._side_tasks: set[asyncio.Task] = set()

    def install(self) -> None:
        e = self.s.engine
        e.register("extract", self.extract)
        e.register("verify", self.verify)
        e.register("call", self.call)
        e.register("bank", self.bank)
        e.register("memo", self.memo)
        e.register("sync", self.sync)
        e.after_step = self.after_step

    # ================================================================ intake

    def create_application(self, filename: str, data: bytes, sample_id: Optional[str] = None) -> str:
        media = media_type_for(filename)
        if media is None:
            raise ValueError("Upload a PDF, PNG, or JPG credit application.")
        with self.s.db.session() as s:
            app = Application(source_filename=filename, source_path="", sample_id=sample_id, status="received")
            s.add(app)
            s.flush()
            path = self.s.settings.uploads_dir / f"{app.id}{Path(filename).suffix.lower()}"
            path.write_bytes(data)
            app.source_path = str(path)
            self.s.engine.add_step(s, app.id, "extract", state={"max_attempts": 2})
            app_id = app.id
        self.s.bus.publish("application.created", f"Received {filename}", application_id=app_id, data={"filename": filename})
        return app_id

    # ============================================================ helpers

    def _status(self, app_id: str, status: str, message: str) -> None:
        with self.s.db.session() as s:
            app = s.get(Application, app_id)
            if app is None:
                return
            app.status = status
        self.s.bus.publish("application.status", message, application_id=app_id, data={"status": status})

    @staticmethod
    def _evidence(s, app_id: str, code: str, kind: str, label: str, detail: str, source_id: Optional[str] = None) -> str:
        ev_id = f"{app_id}.{code}"
        row = s.get(Evidence, ev_id)
        if row is None:
            s.add(Evidence(id=ev_id, application_id=app_id, kind=kind, label=label, detail=detail, source_id=source_id))
        else:
            row.kind, row.label, row.detail, row.source_id = kind, label, detail, source_id
        return code

    def _application(self, app_id: str) -> tuple[Application, CreditApplication]:
        with self.s.db.session() as s:
            app = s.get(Application, app_id)
            if app is None or app.extracted is None:
                raise StepFailed("application has not been read yet")
            return app, CreditApplication.model_validate(app.extracted)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    # ============================================================== steps

    async def extract(self, ctx: StepContext) -> Done:
        self._status(ctx.application_id, "reading", "Reading the application")
        with self.s.db.session() as s:
            app = s.get(Application, ctx.application_id)
            assert app is not None
            path, filename = Path(app.source_path), app.source_filename
        media = media_type_for(filename) or "application/pdf"
        application = await extract_application(self.s.llm, self.s.settings.seller_name, path.read_bytes(), media)

        with self.s.db.session() as s:
            app = s.get(Application, ctx.application_id)
            assert app is not None
            app.extracted = application.model_dump()
            app.applicant_name = application.applicant.dba or application.applicant.legal_name
            app.requested_limit = application.requested_limit
            app.requested_terms = application.requested_terms
            for old in list(app.references):  # idempotent if re-run
                s.delete(old)
            s.flush()
            for i, ref in enumerate(application.trade_references):
                s.add(Reference(
                    application_id=app.id, position=i, kind="trade", company=ref.company,
                    contact_name=ref.contact_name, phone=normalize_phone(ref.phone), email=ref.email,
                    details=ref.model_dump(), status="pending",
                ))
            if application.bank:
                b = application.bank
                s.add(Reference(
                    application_id=app.id, position=len(application.trade_references), kind="bank", company=b.bank_name,
                    contact_name=b.contact_name, phone=normalize_phone(b.phone), email=b.email,
                    details=b.model_dump(), status="pending",
                ))
            self._evidence(
                s, app.id, "app", "application", "Application",
                f"Signed application from {application.applicant.legal_name} requesting "
                f"{money(application.requested_limit)} on {application.requested_terms or 'unstated terms'}.",
            )
        n = len(application.trade_references)
        self.s.bus.publish(
            "application.read",
            f"Read the application: {application.applicant.legal_name}, {n} trade references"
            + (f", {len(application.issues)} fields to double-check" if application.issues else ""),
            application_id=ctx.application_id,
        )
        return Done({"trade_references": n, "issues": len(application.issues)})

    async def verify(self, ctx: StepContext) -> Done:
        self._status(ctx.application_id, "verifying", "Checking public records")
        policy = load_policy()
        _, application = self._application(ctx.application_id)
        with self.s.db.session() as s:
            refs = s.scalars(select(Reference).where(Reference.application_id == ctx.application_id, Reference.kind == "trade").order_by(Reference.position)).all()
            ref_rows = [(r.id, r.company, r.details or {}) for r in refs]

        a = application.applicant
        applicant_party = ver.PartyInput("applicant", a.legal_name, a.phone, a.email, a.website, a.address.street, a.address.postal_code, a.address.state)
        ref_parties = [
            ver.PartyInput(rid, company, d.get("phone"), d.get("email"), None, (d.get("address") or {}).get("street"),
                           (d.get("address") or {}).get("postal_code"), (d.get("address") or {}).get("state"))
            for rid, company, d in ref_rows
        ]
        checks = await asyncio.gather(*[
            ver.check_party(p, self.s.registry, self.s.domains, self.s.websites) for p in [applicant_party, *ref_parties]
        ])
        applicant_checks, ref_checks = checks[0], checks[1:]
        app_signals = ver.applicant_signals(application, applicant_checks, policy)
        people = ver.applicant_people(application, applicant_checks)

        flagged_names = []
        with self.s.db.session() as s:
            app = s.get(Application, ctx.application_id)
            assert app is not None
            app.applicant_checks = {**applicant_checks, "signals": [x.to_dict() for x in app_signals]}
            reg = applicant_checks["registry"]
            self._evidence(
                s, app.id, "reg-applicant", "registry", "State records",
                f"{reg.get('legal_name') or a.legal_name}: "
                + (f"registered {reg.get('incorporated_on')}, status {reg.get('status')}" if reg.get("found") else "no registration found")
                + f" ({reg.get('source')}).",
            )
            for party, chk in zip(ref_parties, ref_checks):
                others = [p for p in ref_parties if p.key != party.key]
                sigs = ver.reference_signals(party, chk, applicant_party, applicant_checks, people, others, policy)
                flagged = ver.is_flagged(sigs, policy)
                ref = s.get(Reference, party.key)
                assert ref is not None
                ref.checks = {**chk, "signals": [x.to_dict() for x in sigs], "rows": ver.display_rows(party, chk, sigs)}
                ref.flagged = flagged
                ref.status = "flagged" if flagged else "verified"
                n = ref.position + 1
                r = chk["registry"]
                self._evidence(
                    s, app.id, f"reg-ref-{n}", "registry", f"{ref.company} records",
                    (f"Registered {r.get('incorporated_on')}, status {r.get('status')}" if r.get("found") else "No registration found")
                    + f" ({r.get('source')}).",
                    source_id=ref.id,
                )
                if flagged:
                    flagged_names.append(ref.company)
                    self._evidence(
                        s, app.id, f"fraud-ref-{n}", "fraud", "Fraud check",
                        f"{ref.company}: " + " ".join(x.message for x in sigs if x.severity in ("critical", "warn")),
                        source_id=ref.id,
                    )
        msg = (f"Flagged {', '.join(flagged_names)}" if flagged_names else "All references check out") + f"; {len(ref_parties) - len(flagged_names)} to call"
        self.s.bus.publish("verification.done", msg, application_id=ctx.application_id,
                           data={"flagged": flagged_names, "applicant_signals": [x.to_dict() for x in app_signals]})
        return Done({"flagged": flagged_names})

    async def call(self, ctx: StepContext):
        st = self.s.settings
        state = ctx.state
        active = state.get("active_call")
        with self.s.db.session() as s:
            ref = s.get(Reference, ctx.reference_id)
            assert ref is not None
            company = ref.company
            app_row = s.get(Application, ctx.application_id)
            app_channel = app_row.call_channel if app_row is not None else None
            active_call = s.get(Call, active) if active else None
            active_status = active_call.status if active_call else None

        if active and active_status == "ended":
            state["active_call"] = None
            with self.s.db.session() as s:
                call = s.get(Call, active)
                assert call is not None
                result = CallResult.model_validate(call.result)
            return self._after_call(ctx, result, active)

        if active and ctx.resumed_by_timeout:
            await self.s.calls.hangup(active)
            self.s.calls.finish(active, CallResult(outcome="no_answer", summary="The call timed out."))
            state["active_call"] = None
            with self.s.db.session() as s:
                call = s.get(Call, active)
                result = CallResult.model_validate(call.result) if call and call.result else CallResult(outcome="no_answer", summary="Timed out.")
            return self._after_call(ctx, result, active)

        if active:  # still ringing or talking (e.g. after a restart, or "call now" while busy)
            return Wait(f"On a call with {company}", timeout_seconds=st.call_ring_timeout_seconds + MAX_CALL_SECONDS)

        attempts = state.get("call_attempts", 0)
        if attempts >= st.call_max_attempts:
            self._set_ref(ctx.reference_id, status="unreachable")
            return Done({"outcome": "unreachable"})
        # The application row is the source of truth for the channel; step state can
        # be stale if the reviewer switched channels while this handler was running.
        channel = self.s.calls.resolve_channel(state.pop("channel", None) or app_channel or state.get("default_channel"))
        attempts += 1
        state["call_attempts"] = attempts
        call_id = self.s.calls.create_call(ctx.reference_id, attempts, channel)  # type: ignore[arg-type]
        state["active_call"] = call_id
        await self.s.calls.place(call_id)
        with self.s.db.session() as s:
            placed = s.get(Call, call_id)
            ended = placed is not None and placed.status == "ended"
            result = CallResult.model_validate(placed.result) if ended and placed.result else None
        if ended:  # failed while dialing (bad number, Twilio error, busy signal)
            state["active_call"] = None
            return self._after_call(ctx, result or CallResult(outcome="failed", summary="The call could not be placed."), call_id)
        return Wait(f"Calling {company} (attempt {attempts})", timeout_seconds=st.call_ring_timeout_seconds + MAX_CALL_SECONDS)

    def _after_call(self, ctx: StepContext, result: CallResult, call_id: str):
        st = self.s.settings
        attempts = ctx.state.get("call_attempts", 1)
        with self.s.db.session() as s:
            ref = s.get(Reference, ctx.reference_id)
            assert ref is not None
            ref.outcome = result.outcome
            ref.summary = result.summary
            company, n = ref.company, ref.position + 1
            if result.outcome == "completed":
                ref.findings = result.findings.model_dump()
                ref.status = "completed"
                f = result.findings
                detail = "; ".join(x for x in [
                    f"customer since {f.customer_since}" if f.customer_since else "",
                    f"terms {f.terms}" if f.terms else "",
                    f"high credit {money(f.high_credit)}" if f.high_credit is not None else "",
                    f"balance {money(f.current_balance)}" if f.current_balance is not None else "",
                    f"past due {money(f.past_due_amount)}" if f.past_due_amount is not None else "",
                    f"pays in about {f.avg_days_to_pay:.0f} days" if f.avg_days_to_pay is not None else "",
                ] if x)
                self._evidence(s, ref.application_id, f"call-ref-{n}", "call", f"Call {n}", f"{company}: {detail}.", source_id=call_id)
                return Done({"outcome": "completed"})
            if result.outcome in ("declined", "wrong_number"):
                ref.status = result.outcome
                if result.alternate_contact:
                    ref.summary = f"{result.summary} Written requests go to {result.alternate_contact}."
                self._evidence(s, ref.application_id, f"call-ref-{n}", "call", f"Call {n}", f"{company}: {ref.summary}", source_id=call_id)
                return Done({"outcome": result.outcome})
            # no answer, voicemail, failed, callback requested: try again if allowed
            if attempts >= st.call_max_attempts:
                ref.status = "unreachable"
                self._evidence(s, ref.application_id, f"call-ref-{n}", "call", f"Call {n}", f"{company}: not reached after {attempts} attempts.", source_id=call_id)
                return Done({"outcome": "unreachable"})
            delay = st.call_retry_seconds
            if result.outcome == "callback_requested":
                ref.status = "callback_scheduled"
                why = f"Calling back ({result.callback_at or 'as requested'})"
                if st.honor_callback_times:
                    delay = max(delay, 3600)
            else:
                ref.status = "retry_scheduled"
                why = {"no_answer": "No answer", "voicemail": "Left a voicemail", "failed": "Call failed"}.get(result.outcome, result.outcome) + ", trying again"
        return Retry(after_seconds=delay, reason=why)

    async def bank(self, ctx: StepContext):
        with self.s.db.session() as s:
            ref = s.scalars(select(Reference).where(Reference.application_id == ctx.application_id, Reference.kind == "bank")).first()
            if ref is None:
                return Skip("No bank listed on the application")
            ref_id, bank_name, bank_email = ref.id, ref.company, ref.email
        _, application = self._application(ctx.application_id)
        state = ctx.state

        reply = self._latest_bank_reply(ctx.application_id, state.get("sent_at"))
        if state.get("sent_email") and reply is not None:
            email_id, body = reply
            findings = await bank_agent.read_reply(self.s.llm, application.applicant.legal_name, body)
            with self.s.db.session() as s:
                ref = s.get(Reference, ref_id)
                assert ref is not None
                ref.findings = findings.model_dump()
                ref.status = "completed"
                ref.outcome = "completed"
                ref.summary = findings.summary
                self._evidence(s, ctx.application_id, "bank", "bank", "Bank email", f"{bank_name}: {findings.summary}", source_id=email_id)
            return Done({"outcome": "replied"})

        if state.get("sent_email") and ctx.resumed_by_timeout:
            self._set_ref(ref_id, status="no_reply", outcome="no_reply", summary="The bank did not reply in time.")
            return Done({"outcome": "no_reply"})

        if not state.get("sent_email"):
            subject, body = bank_agent.compose_request(self.s.settings.seller_name, self.s.settings.seller_contact_email, application)
            email_id = await self.s.mailer.send(ctx.application_id, ref_id, bank_email, subject, body)
            state["sent_email"] = email_id
            state["sent_at"] = utcnow().isoformat()
            self._set_ref(ref_id, status="emailed")
            delay = self.s.settings.simulate_bank_reply_seconds
            canned = bank_reply_for(bank_name)
            if delay > 0 and canned and self.s.settings.email_mode == "outbox":
                self._spawn(self._simulated_bank_reply(ctx.application_id, ref_id, bank_email, canned, delay))
        return Wait(f"Waiting for {bank_name} to reply", timeout_seconds=self.s.settings.bank_reply_timeout_seconds)

    async def _simulated_bank_reply(self, app_id: str, ref_id: str, from_addr: Optional[str], body: str, delay: float) -> None:
        await asyncio.sleep(delay)
        self.simulate_bank_reply(app_id, body=body, from_addr=from_addr)

    def simulate_bank_reply(self, app_id: str, body: Optional[str] = None, from_addr: Optional[str] = None) -> str:
        with self.s.db.session() as s:
            ref = s.scalars(select(Reference).where(Reference.application_id == app_id, Reference.kind == "bank")).first()
            if ref is None:
                raise ValueError("No bank on this application")
            bank_name, ref_id, from_addr = ref.company, ref.id, from_addr or ref.email
        text = body or bank_reply_for(bank_name) or (
            f"Hello,\n\nThe account has been open with {bank_name} for several years. Average balances are in the low five "
            "figures and there have been no NSF items in the last 12 months. Relationship is satisfactory.\n\nBusiness Banking"
        )
        return self.s.mailer.receive(app_id, from_addr, f"RE: Bank reference request", text, delivery="simulated", reference_id=ref_id)

    def _latest_bank_reply(self, app_id: str, sent_at: Optional[str]) -> Optional[tuple[str, str]]:
        """The newest inbound email that arrived after our request went out."""
        since = parse_iso(sent_at)
        with self.s.db.session() as s:
            q = select(Email).where(Email.application_id == app_id, Email.direction == "inbound").order_by(Email.created_at.desc())
            for email in s.scalars(q).all():
                if since is None or email.created_at is None or email.created_at >= since - timedelta(seconds=1):
                    return email.id, email.body
            return None

    async def memo(self, ctx: StepContext) -> Done:
        self._status(ctx.application_id, "deciding", "Writing the credit memo")
        policy = load_policy()
        app, application = self._application(ctx.application_id)
        with self.s.db.session() as s:
            app_row = s.get(Application, ctx.application_id)
            assert app_row is not None
            app_signals = (app_row.applicant_checks or {}).get("signals", [])
            refs = s.scalars(select(Reference).where(Reference.application_id == ctx.application_id).order_by(Reference.position)).all()
            evidence = {e.id.split(".", 1)[1] for e in s.scalars(select(Evidence).where(Evidence.application_id == ctx.application_id)).all()}
            ref_inputs = []
            bank_findings: Optional[BankFindings] = None
            for r in refs:
                n = r.position + 1
                if r.kind == "bank":
                    if r.findings:
                        bank_findings = BankFindings.model_validate(r.findings)
                    continue
                ids = [c for c in (f"call-ref-{n}", f"fraud-ref-{n}", f"reg-ref-{n}") if c in evidence]
                ref_inputs.append(decision_agent.RefInput(
                    id=r.id, company=r.company, flagged=r.flagged, outcome=r.outcome if not r.flagged else "excluded",
                    findings=ReferenceFindings.model_validate(r.findings) if r.findings else None,
                    signals=(r.checks or {}).get("signals", []), evidence_ids=ids,
                ))
        result = decision_agent.evaluate(
            policy, application, app_signals, ref_inputs, bank_findings,
            bank_evidence="bank" if "bank" in evidence else None,
            app_evidence="app",
            applicant_registry_evidence="reg-applicant" if "reg-applicant" in evidence else None,
        )
        applicant = application.applicant.dba or application.applicant.legal_name
        try:
            narrative = await decision_agent.write_narrative(self.s.llm, self.s.settings.seller_name, applicant, result)
            narrative_source = self.s.llm.mode
        except Exception as exc:  # noqa: BLE001
            log.warning("memo narrative failed, using template: %s", exc)
            narrative = decision_agent.template_narrative(applicant, result)
            narrative_source = "template"
        with self.s.db.session() as s:
            app_row = s.get(Application, ctx.application_id)
            assert app_row is not None
            app_row.decision = {**result, "narrative": narrative.model_dump(), "narrative_source": narrative_source, "generated_at": utcnow().isoformat()}
            app_row.status = "ready_for_review"
        label = {"approve": "Recommend approval", "approve_reduced": "Recommend a reduced limit", "request_info": "Need more information",
                 "manual_review": "Needs a credit manager", "decline": "Recommend decline"}[result["decision"]]
        lim = f": {money(result['recommended_limit'])} on {result['terms']}" if result.get("recommended_limit") else ""
        self.s.bus.publish("memo.ready", f"{label}{lim}", application_id=ctx.application_id, data={"decision": result["decision"]})
        self.s.bus.publish("application.status", "Ready for review", application_id=ctx.application_id, data={"status": "ready_for_review"})
        return Done({"decision": result["decision"]})

    async def sync(self, ctx: StepContext) -> Done:
        with self.s.db.session() as s:
            app = s.get(Application, ctx.application_id)
            assert app is not None and app.review is not None and app.extracted is not None
            a = app.extracted["applicant"]
            review = dict(app.review)
            customer = {
                "display_name": a.get("dba") or a["legal_name"],
                "legal_name": a["legal_name"],
                "email": a.get("email"),
                "phone": format_phone(normalize_phone(a.get("phone"))),
                "address": a.get("address"),
                "credit_limit": review["limit"],
                "terms": review["terms"],
                "memo_url": f"{(self.s.settings.public_base_url or 'http://localhost:8000').rstrip('/')}/applications/{app.id}",
                "application_id": app.id,
            }
        try:
            result = await self.s.accounting.sync_customer(customer)
        except Exception as exc:  # noqa: BLE001
            log.warning("accounting sync failed: %s", exc)
            result = {"ok": False, "provider": self.s.accounting.mode, "error": str(exc)}
        with self.s.db.session() as s:
            app = s.get(Application, ctx.application_id)
            assert app is not None
            app.sync = {**result, "synced_at": utcnow().isoformat()}
            if result.get("ok"):
                app.status = "synced"
        where = "QuickBooks" if result.get("provider") == "quickbooks" else "the ledger"
        if result.get("ok"):
            self.s.bus.publish("application.synced", f"Added {customer['display_name']} to {where} ({result.get('customer_id')})",
                               application_id=ctx.application_id, data={"status": "synced", **{k: v for k, v in result.items() if k != 'payload'}})
        else:
            self.s.bus.publish("application.sync_failed", f"Could not sync to {where}: {result.get('error')}", application_id=ctx.application_id)
        return Done({"ok": bool(result.get("ok"))})

    # ======================================================== graph edges

    def after_step(self, s, step: Step) -> None:
        if step.status == "failed":
            app = s.get(Application, step.application_id)
            if app is not None and step.kind in ("extract", "verify", "memo"):
                app.status = "failed"
                app.error = step.error
            return
        if step.kind == "call":
            # A reference reached after the memo was written (e.g. "Call now" on a
            # finished reference) re-opens the decision so it reflects the new answers.
            memo = s.scalars(select(Step).where(Step.application_id == step.application_id, Step.kind == "memo")).first()
            app = s.get(Application, step.application_id)
            if memo is not None and memo.status == "done" and app is not None and app.status == "ready_for_review":
                memo.status = "pending"
                memo.next_run_at = None
                app.status = "deciding"
            return
        if step.kind == "extract":
            verify = self.s.engine.add_step(s, step.application_id, "verify", depends_on=[step.id], state={"requires": [step.id]})
            bank = self.s.engine.add_step(s, step.application_id, "bank", depends_on=[step.id], state={"requires": [step.id], "max_attempts": 2})
            step_state = dict(step.state or {})
            step_state["children"] = [verify.id, bank.id]
            step.state = step_state
        elif step.kind == "verify":
            refs = s.scalars(select(Reference).where(Reference.application_id == step.application_id, Reference.kind == "trade").order_by(Reference.position)).all()
            deps = []
            app = s.get(Application, step.application_id)
            channel = app.call_channel if app is not None else None
            for ref in refs:
                if ref.flagged:
                    continue
                call_state: dict = {"call_attempts": 0, "max_attempts": 5}
                if channel:
                    call_state["default_channel"] = channel
                call = self.s.engine.add_step(s, step.application_id, "call", reference_id=ref.id, depends_on=[step.id], state=call_state)
                ref.status = "queued"
                deps.append(call.id)
            bank = s.scalars(select(Step).where(Step.application_id == step.application_id, Step.kind == "bank")).first()
            if bank is not None:
                deps.append(bank.id)
            self.s.engine.add_step(s, step.application_id, "memo", depends_on=[step.id, *deps], state={"requires": [step.id], "max_attempts": 2})
            if app is not None:
                app.status = "checking_references"

    # ===================================================== human actions

    def approve(self, app_id: str, limit: Optional[float], terms: Optional[str], note: Optional[str], reviewer: str = "Credit analyst") -> None:
        with self.s.db.session() as s:
            app = s.get(Application, app_id)
            if app is None or app.decision is None:
                raise ValueError("The memo is not ready yet")
            final_limit = limit if limit is not None else app.decision.get("recommended_limit") or app.requested_limit
            if not final_limit:
                raise ValueError("Set a credit limit to approve")
            app.review = {"action": "approved", "limit": float(final_limit), "terms": terms or app.decision.get("terms") or "Net 30",
                          "note": note, "reviewer": reviewer, "at": utcnow().isoformat()}
            app.status = "approved"
            self.s.engine.add_step(s, app_id, "sync", state={"max_attempts": 1})
        self.s.bus.publish("application.approved", f"Approved {money(final_limit)} on {terms or 'Net 30'}", application_id=app_id, data={"status": "approved"})

    def retry_sync(self, app_id: str) -> None:
        with self.s.db.session() as s:
            app = s.get(Application, app_id)
            if app is None or app.review is None or app.review.get("action") != "approved":
                raise ValueError("Approve the application first")
            self.s.engine.add_step(s, app_id, "sync", state={"max_attempts": 1})

    def decline(self, app_id: str, reason: Optional[str], reviewer: str = "Credit analyst") -> None:
        with self.s.db.session() as s:
            app = s.get(Application, app_id)
            if app is None:
                raise ValueError("Unknown application")
            decision = app.decision or {}
            reasons = [reason] if reason else [decision.get("reason") or "Insufficient verified trade references"]
            applicant = app.applicant_name or "Applicant"
            notice = (
                f"Dear {applicant},\n\nThank you for applying for trade credit with {self.s.settings.seller_name}. "
                "We are unable to extend open account terms at this time.\n\n"
                "Principal reason(s) for our decision:\n" + "\n".join(f"- {r}" for r in reasons) + "\n\n"
                "We are happy to continue doing business on prepaid or credit card terms, and to reconsider with additional "
                "trade references or financial statements.\n\nThe federal Equal Credit Opportunity Act prohibits creditors from "
                "discriminating against credit applicants on the basis of race, color, religion, national origin, sex, marital "
                "status, or age (provided the applicant has the capacity to contract); because all or part of the applicant's "
                "income derives from any public assistance program; or because the applicant has in good faith exercised any "
                "right under the Consumer Credit Protection Act.\n\n"
                f"{self.s.settings.seller_name} Credit Team"
            )
            app.review = {"action": "declined", "reasons": reasons, "notice": notice, "reviewer": reviewer, "at": utcnow().isoformat()}
            app.status = "declined"
        self.s.bus.publish("application.declined", "Declined; adverse action notice drafted", application_id=app_id, data={"status": "declined"})

    async def request_info(self, app_id: str, message: Optional[str], reviewer: str = "Credit analyst") -> str:
        with self.s.db.session() as s:
            app = s.get(Application, app_id)
            if app is None:
                raise ValueError("Unknown application")
            missing = (app.decision or {}).get("missing") or []
            applicant = app.applicant_name or "there"
            to = ((app.extracted or {}).get("applicant") or {}).get("email")
        items = "\n".join(f"- {m}" for m in missing) or "- One more trade reference we can verify by phone"
        body = message or (
            f"Hi {applicant},\n\nThanks for your credit application with {self.s.settings.seller_name}. "
            f"To finish our review we need:\n{items}\n\nReply to this email and we will pick it up right away.\n\n"
            f"{self.s.settings.seller_name} Credit Team"
        )
        email_id = await self.s.mailer.send(app_id, None, to, f"{self.s.settings.seller_name} credit application: one more thing", body)
        with self.s.db.session() as s:
            app = s.get(Application, app_id)
            assert app is not None
            app.review = {"action": "info_requested", "email_id": email_id, "reviewer": reviewer, "at": utcnow().isoformat()}
            app.status = "info_requested"
        self.s.bus.publish("application.info_requested", "Asked the applicant for more information", application_id=app_id, data={"status": "info_requested"})
        return email_id

    def call_now(self, reference_id: str, channel: Optional[str]) -> bool:
        with self.s.db.session() as s:
            step = s.scalars(select(Step).where(Step.reference_id == reference_id, Step.kind == "call")).first()
            if step is None:
                return False
            step_id = step.id
            if step.status == "done" and channel:
                # Re-open a finished call step (e.g. try a different channel after a decline)
                ref = s.get(Reference, reference_id)
                state = dict(step.state or {})
                state["call_attempts"] = 0
                state["channel"] = channel
                step.state = state
                step.status = "pending"
                step.next_run_at = None
                if ref is not None:
                    ref.status = "queued"
                self.s.engine.wake()
                return True
        return self.s.engine.run_now(step_id, {"channel": channel} if channel else None)

    def set_default_channel(self, application_id: str, channel: str) -> None:
        """Where this application's calls go. Applies to calls not yet placed,
        including references that verification has not cleared yet."""
        with self.s.db.session() as s:
            app = s.get(Application, application_id)
            if app is None:
                raise ValueError("Unknown application")
            app.call_channel = channel
            for step in s.scalars(select(Step).where(Step.application_id == application_id, Step.kind == "call")).all():
                state = dict(step.state or {})
                state["default_channel"] = channel
                step.state = state
        self.s.bus.publish("application.call_channel", f"Calls now go to the {channel} phone line", application_id=application_id,
                           data={"call_channel": channel}, persist=False)

    def _set_ref(self, ref_id: Optional[str], **fields) -> None:
        if not ref_id:
            return
        with self.s.db.session() as s:
            ref = s.get(Reference, ref_id)
            if ref is None:
                return
            for k, v in fields.items():
                setattr(ref, k, v)
            app_id, company = ref.application_id, ref.company
        self.s.bus.publish("reference.updated", application_id=app_id, persist=False, data={"reference_id": ref_id, **{k: v for k, v in fields.items() if isinstance(v, (str, bool, int, float))}})
