# Demo script (3 to 5 minutes)

Two people: one drives the review screen, one plays the reference on the phone. Before recording, run `make reset` and start Vouch.

For a real phone call, use `CALL_CHANNEL=twilio` with `DEMO_REFERENCE_PHONE` set to the second person's phone and `REAL_CALL_LIMIT=1`, so Harbor Paper rings the phone and Coastal Flour Mills runs simulated. Each reference's call panel also has a **Line** switch. Without Twilio, open **Phone** in a second browser window and pick **Browser phone** on the review screen.

## 1. The problem (0:00 to 0:30)

On the Applications page:

> When a new business customer asks for Net 30, someone in finance retypes their application, emails their bank, and calls three references who rarely pick up. Vouch does all of that.

## 2. Read the scan (0:30 to 1:00)

New application, then upload `samples/acme-bakery-supply-credit-application.pdf` (or run the sample). Open the Application section to show the extracted fields beside the scan.

> This is a scanned, hand-filled form. Claude reads it directly, into a typed schema, including the three trade references and the bank.

## 3. Catch the fake reference (1:00 to 1:45)

References section: Harbor and Coastal score 99, Bayview Packaging scores 1 and is flagged. Its evidence list is already open.

> Every reference gets an authenticity score. For Bayview, caller ID and Google both list its phone number to Acme, the applicant. It was registered 23 days ago, at Acme's address, and its only officer is Acme's owner. Vouch won't call it.

Open Harbor's evidence list.

> Harbor scores 99 for the opposite reasons. The carrier's caller ID says Harbor Paper, Google lists the number at Harbor's address, and IPQualityScore's fraud model rates it 6 out of 100. Each line names its source, and the weights live in the credit policy file, so we can tell an applicant exactly why a reference didn't count.

## 4. The live call (1:45 to 3:00)

Harbor Paper rings the phone. (On the simulated line, its first attempt reaches voicemail: the panel shows the greeting, the short message Vouch left, and the retry countdown. Click **Call now** if you don't want to wait.) If the real call goes to voicemail, Vouch leaves the same message instead of starting the interview. The second person answers as Linda Park:

- "This is Linda in accounts receivable. Sure, go ahead."
- When asked for numbers: "Let me pull up the account, one second." Then: customer since 2022, Net 30.
- High credit about forty thousand, owes around twelve thousand four hundred.
- Nothing past due, pays in about 28 days.
- Yes, would extend more.

> It says up front that it's an AI on a recorded line. The fields on the right fill in while she talks.

## 4b. The follow-up (3:00 to 3:20)

Scroll to Coastal Flour Mills. Raj was busy and asked to be called back tomorrow morning. The panel reads "Calling back tomorrow at 10:00 AM PST."

> References rarely pick up the first time. Vouch hangs up politely, books the callback for the time Raj asked for, in his time zone, and the memo waits for it. If nobody answers twice, it tries again the next business morning. For the demo, I'll pull tomorrow forward.

Click **Call now**. Raj answers this time and gives his numbers.

## 5. The bank (3:00 to 3:20)

Bank section: the request email and the reply, read into account age, balances, and NSF history.

## 6. The memo and approval (3:20 to 4:30)

Decision card: $25,000 on Net 30, below the $30,000 requested.

> Every number comes from the credit policy file, and every sentence links to its source. Here's how the limit was set: average high credit from two verified references, times 85 percent, rounded down.

Click a source chip to jump to the transcript. Approve, then show the customer created in QuickBooks with Net 30 terms and the limit in the notes.

## 7. Close (4:30 to 5:00)

> Next: with the applicant's consent, Ramp's own payment data could answer a reference check instantly, so the calls become the fallback.

## Questions to be ready for

- **Why not let Claude decide the limit?** Credit decisions need repeatable, explainable reasons. Policy computes; Claude explains and cites.
- **What if a reference won't talk to an AI?** The agent offers to take a written request and records the email; that reference is marked declined and the memo says so.
- **Recording consent?** Disclosed in the first sentence of every call.
- **What happens if the server restarts mid-call?** The workflow state is in SQLite; the call is marked failed and retried, everything else resumes.
- **Cost per application?** A handful of Sonnet calls for reading and writing plus Haiku for the calls: well under a dollar, plus Twilio minutes.
