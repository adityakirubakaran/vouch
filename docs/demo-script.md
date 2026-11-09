# Demo script (about 5 minutes)

Two people: one drives the screen and narrates, the other plays Harbor Paper's Linda Park on a real phone call. Coastal Flour Mills always runs on the simulated line, so its callback shows on screen without a second phone.

## Setup

- `.env` has `ANTHROPIC_API_KEY`, `CALL_CHANNEL=twilio`, the three Twilio keys, `PUBLIC_BASE_URL`, `DEMO_REFERENCE_PHONE` (the second person's cell), `REAL_CALL_LIMIT=1`, and `PRESENTER_MODE=true`.
- `make reset`, so Vouch has no history from practice runs.
- Window 1: `make tunnel`, and check that its URL matches `PUBLIC_BASE_URL`. Window 2: `make start`, then open http://localhost:8000. Settings should show Twilio as configured.

Without Twilio, leave `CALL_CHANNEL=simulated`: every reference is played by Claude from `backend/vouch/fixtures/personas.json`, and Harbor's first call reaches voicemail before a retry connects.

**Presenter mode.** Vouch pauses before checking references, before calling, and before emailing the bank. A button at the top right names the next step; click it or press the right arrow key when the narration gets there. The script marks those moments **[Press →]**.

## 1. The problem (0:00 to 0:30)

Screen: the empty Applications page.

> Picture a supplier's finance team. A new business customer wants to buy on Net 30, and before anyone says yes, somebody has to vet them. That means retyping a PDF application, emailing their bank, and calling three trade references who almost never pick up. It takes days. And because nobody has time to dig in, fake references get right through.
>
> So we built Vouch. It's five AI agents working as a team. One reads the application, one checks every reference for fraud, one calls them, one emails the bank, and one writes the credit memo. Let's run one.

## 2. Reading the scan (0:30 to 1:00)

Screen: **New application**, drag in `samples/acme-bakery-supply-credit-application.pdf`. Reading takes 10 to 20 seconds. Open the Application tab to show the fields next to the scan. Vouch then pauses on "Check the references."

> Here's a credit application, scanned and filled out by hand, the way they actually show up. Claude is reading it right now, straight from the image. There's no OCR step. We make it answer in a strict format, so every field comes back as clean data, and if anything doesn't check out, it gets sent back to fix it.
>
> And there we go. Acme Bakery Supply, asking for thirty thousand dollars on Net 30, with their bank and three trade references.

## 3. Catching the fake reference (1:00 to 1:45)

Screen: the References tab, cards marked "Not checked yet."

> Before Vouch calls anyone, it checks whether these references are even real.

**[Press →]** The cards fill in: Harbor and Coastal at 99, Bayview Packaging at 1 and flagged, with its reasons open.

> This is our fraud model. For every reference, it pulls almost thirty signals from seven places: state business records, when the company's website was registered, the company's own site, who the phone carrier says owns the number, who Google lists at that number, IPQualityScore's fraud score for it, and everything Vouch has seen before. Each signal pushes the score up or down by a set amount. It's the same kind of model that's behind credit scores, and it lands on a number from one to ninety-nine.
>
> So look at Bayview. It scores a one. Caller ID and Google both say its phone number belongs to Acme, the applicant itself. Bayview was registered twenty-three days ago, at Acme's address, and its only officer is Acme's owner. That's a fake reference, and Vouch won't call it.
>
> Harbor Paper is the opposite. Ninety-nine. The number's listed to Harbor, they've been registered for fifteen years, and the fraud score is six out of a hundred. And every one of these reasons is right here, with where it came from.

Keep it accurate: the demo companies are fictional, so their records come from sandbox data and the cards say "(sandbox)." The weights are set by hand today, so call it a logistic scorecard, not a trained model.

## 4. The live call (1:45 to 3:00)

Screen: Calls. Harbor Paper's line says Real phone.

> So let's call the two references that checked out.

**[Press →]** The phone rings a few seconds later. Let the call play.

> Vouch is dialing Harbor Paper right now, on a real phone line. Listen to how it opens: it says it's an AI and that the call is recorded. And before it asks a single question, it makes sure it's talking to the right person.

Linda's lines (the memo's numbers depend on them; the agent words its questions its own way, so answer the topic it asks about):

| When the agent asks about | Linda says |
|---|---|
| Having two minutes | "This is Linda in accounts receivable. Sure, go ahead." |
| Whether you handle Acme's account and have the details | "Yes, I handle their account. Let me pull it up, one second. Okay, I have it in front of me." |
| How long, and what terms | "They've bought from us on credit since 2022, on Net 30." |
| High credit and balance | "High credit is about forty thousand. Right now they owe around twelve thousand four hundred." |
| Past due and days to pay | "Nothing is past due. They pay in about 28 days on average." |
| Extending more credit | "Yes, we'd happily extend more. Good customer, they order every week." |
| Its read-back | "That's right." |

After the call:

> So what just happened under the hood? Twilio turns Linda's voice into text and streams it to Claude Haiku, which streams its answer straight back as speech. It stops talking the moment she cuts in. And that credit file on the right filled itself in while she was talking. That's a second model listening along. Once the call ends, Claude re-reads the whole transcript and only keeps numbers Linda actually said.
>
> And if nobody had picked up? Vouch recognizes the voicemail, leaves a quick message, and tries again later.

## 5. The follow-up (3:00 to 3:30)

Screen: Coastal Flour Mills (simulated line). Its first call ran while Linda was on the phone: Raj asked for a callback, and the panel reads "Calling back tomorrow at 10:00 AM."

> Meanwhile, Vouch called Coastal Flour Mills too. Raj was signing for a delivery and asked us to call back tomorrow morning. So Vouch said no problem, hung up, and booked it for ten a.m. tomorrow, in Raj's own time zone. And if someone misses two calls in a row, it tries again the next business morning.
>
> All of this runs on a workflow engine we built to handle waiting on people. Every step is saved, so even if the server restarts tonight, that callback still happens tomorrow, and the memo waits for it. We can't wait until tomorrow, though, so I'll pull it forward.

Click **Call now**. Raj answers on the simulated line: customer since 2019, Net 30, high credit $20,000, nothing past due, about 31 days to pay.

## 6. The bank (3:30 to 3:50)

> Last piece: the bank.

**[Press →]** The Bank tab opens. The request appears right away and the simulated reply about 8 seconds later, both tagged "Outbox, not sent."

> Vouch emails them a reference request. And when the bank writes back, Claude reads the reply and pulls out what matters: the account's been open since 2014, balances are in the mid five figures, no overdrafts in a year, and there's a hundred-and-fifty-thousand-dollar credit line in good standing.

## 7. The memo and approval (3:50 to 4:40)

Screen: the decision card: approve $25,000 on Net 30, below the $30,000 requested. Open "How the limit was set," then click a source chip to jump to Linda's transcript.

> With everything in, here's the memo. Vouch recommends twenty-five thousand on Net 30, a little less than the thirty they asked for.
>
> Here's the part we care about most: the AI didn't pick that number. Our credit policy did, and it's just code. The two good references extend thirty thousand on average. Policy takes eighty-five percent of that. They pay on time, so there's no cut. Round it down, and you get twenty-five thousand.
>
> Claude writes the explanation, but every sentence has to point to its source, and we check every one of those links. If it cites something that doesn't exist, it gets thrown out. So I can click any of these and land right on the moment in the call where it came from.

Click **Approve $25,000 and sync.**

> One click, and Acme's a customer in QuickBooks, on Net 30, with the approved limit right there in their record.

(In the demo this writes to the local ledger; connect a QuickBooks sandbox in Settings to show the real customer.)

## 8. Close (4:40 to 5:00)

Screen: the Applications page, Acme marked synced.

> So that's Vouch. Days of retyping, emailing and phone tag, down to a few minutes and one click. A fraud model that caught the fake reference before anyone picked up the phone, and a credit policy you can explain to anyone who asks.
>
> And here's where it goes next. With the customer's permission, payment data a company like Ramp already has could answer these reference checks instantly, and the phone calls just become the backup.
