# n8n PO Reconciliation: vendor email to IDoc, with a human-in-the-loop gate

An n8n workflow template for the purchase-order reconciliation pattern:

1. A non-EDI vendor response (order confirmation, invoice, shipping notice) arrives by email, as text or a PDF attachment.
2. An LLM extracts the fields against a fixed JSON schema.
3. The PO on record is fetched and nine weighted business rules score the extraction.
4. Score at or above the threshold with no hard failure posts automatically. Anything else goes to a reviewer who approves or rejects from an email.
5. Validated data becomes an ORDERS05 IDoc queued for SAP and a landing row for Snowflake. Every attempt is logged.

Built and verified on n8n 2.35.5 (self-hosted). All external systems are swapped for lab stand-ins so the whole thing runs on a laptop: Postgres plays SAP and Snowflake, a webhook plays the Outlook mailbox, Mailpit catches the mail, and any OpenAI-compatible endpoint serves the model. The swap points are listed below.

## Repo layout

| Path | What |
|---|---|
| `workflows/po-reconciliation-email-to-idoc.json` | The workflow. Import it into n8n. 23 nodes plus canvas notes. |
| `data/schema.sql` | Schema `om_poc`: vendors, POs, PO lines, reconciliations, IDoc outbox, Snowflake mirror. Seeds four POs. |
| `data/fixtures/` | Three vendor emails that exercise the three paths, plus the PDF invoice one of them attaches. |
| `scripts/send-fixture.py` | POSTs a fixture to the webhook, base64-encoding the PDF. Python standard library only. |
| `docs/design-notes.md` | Rules, scoring, IDoc mapping, open questions for production. |

## Quick start

Prerequisites: n8n 2.x, Postgres, an SMTP sink (Mailpit works), and an OpenAI-compatible chat endpoint. The lab used LM Studio with `qwen/qwen3-coder-30b`; any model that returns well-formed JSON for the Information Extractor works.

1. Create a database and load the schema.

   ```
   createdb om_poc
   psql om_poc -f data/schema.sql
   ```

2. Create three credentials in n8n. The workflow references them by these names, with placeholder ids, so the import will ask you to pick them.

   | Credential name in the workflow | Type | Used by |
   |---|---|---|
   | `Lab Postgres om_poc` | Postgres | all database nodes |
   | `LM Studio (lab)` | OpenAI API (set Base URL to your endpoint) | `LmStudioExtractorModel` |
   | `Lab Mailpit SMTP` | SMTP | reviewer, AP and ops emails |

3. Import `workflows/po-reconciliation-email-to-idoc.json` (Workflow menu, Import from File). Open each credential-bearing node once and select the credential.

4. Change the model id on `LmStudioExtractorModel` if yours differs, then activate the workflow.

5. Send the fixtures.

   ```
   python3 scripts/send-fixture.py data/fixtures/f1-northwind-confirmation-clean.json
   python3 scripts/send-fixture.py data/fixtures/f2-apex-invoice-price-variance.json
   python3 scripts/send-fixture.py data/fixtures/f3-meridian-unknown-po.json
   ```

   Expected outcomes:

   | Fixture | Path | Why |
   |---|---|---|
   | F1 clean confirmation | auto, posted | every rule passes, score about 0.98 |
   | F2 PDF invoice | review | unit price drift 5% on one line (R7) and total drift (R9) |
   | F3 unknown PO | review, hard fail | PO not on record (R1) and vendor cannot be matched (R2) |

   For F2 and F3 the reviewer email lands in Mailpit at `http://localhost:8025` with Approve and Reject links. Clicking one resumes the waiting execution. AP gets an outcome email either way, and `om_poc.reconciliations` holds the full trail.

The webhook responds synchronously with the decision, score and failed rules, so the caller sees the result without opening n8n.

## How it is wired

The canvas is split into six zones, each framed by a sticky note.

| Zone | Nodes | Notes |
|---|---|---|
| 1 Intake | `WatchOutlookInbox` (disabled), `ReceiveVendorEmailFixture`, `NormalizeEmail` | Both triggers feed one Code node that produces a single email envelope with any PDF in `$binary.attachment`. |
| 2 Decipher | `HasPdfAttachment`, `ExtractPdfText`, `BuildDocumentText`, `ExtractPoFieldsWithAi` | Extract from File pulls PDF text. The Information Extractor runs a manual JSON schema. No agent loop, no tools. |
| 3 Validate | `LookupPurchaseOrder`, `ApplyValidationRules`, `LogReconciliation`, `RespondWithDecision` | PO lookup has Always Output Data on so a missing PO still reaches the rules. |
| 4 HITL gate | `MeetsAutoThreshold`, `RequestHumanReview`, `WaitForReviewerDecision`, `ReviewerApproved` | Wait node resumes on a webhook. Decision comes from the `decision` query parameter. |
| 5 Convert and post | `BuildIdocXml`, `QueueIdocForSap`, `WriteSnowflakeLandingRow`, `MarkReconciliationPosted`, `MarkReconciliationRejected`, `NotifyApOutcome` | Message type follows document type: ORDRSP, INVOIC or DESADV. |
| 6 Failures | `NotifyOpsFailure` | Every fallible node routes its error output here. Nothing is posted on a failure. |

## Tuning

Four constants at the top of `ApplyValidationRules`:

| Constant | Default | Meaning |
|---|---|---|
| `AUTO_THRESHOLD` | 0.90 | score at or above this posts without a human |
| `PRICE_TOLERANCE_PCT` | 2.0 | per-line unit price drift allowed |
| `TOTAL_TOLERANCE_PCT` | 1.0 | document total versus PO total |
| `AI_WEIGHT` | 0.30 | share of the score taken from the model's self-reported confidence |

Score = 0.7 × weighted rule pass ratio + 0.3 × model confidence. A failed hard rule (R1 PO exists, R2 vendor matches) always forces review regardless of score.

## Swap points for production

| Node | Lab stand-in | Production |
|---|---|---|
| `WatchOutlookInbox` | disabled; `ReceiveVendorEmailFixture` webhook plays the mailbox | Microsoft Outlook trigger on the AP mailbox with an OAuth2 credential, plus a Get Attachment step before `NormalizeEmail`. The Code node already accepts the Outlook message shape. |
| `LmStudioExtractorModel` | LM Studio, local | the approved provider: Azure OpenAI, Bedrock, OpenRouter, or a hosted OpenAI-compatible endpoint. Swap the chat model node, keep the Information Extractor. |
| `LookupPurchaseOrder` | Postgres `purchase_orders` + `po_lines` | SAP OData or RFC read of EKKO/EKPO. Keep the output shape (`po_number`, `vendor_id`, `vendor_name`, `email_domain`, `currency`, `order_date`, `total_amount`, `lines[]`) and nothing downstream changes. |
| `QueueIdocForSap` | Postgres `idoc_outbox` | HTTP Request to the SAP PI/PO or CPI IDoc endpoint, or a file drop. The XML is already built. |
| `WriteSnowflakeLandingRow` | Postgres `reconciled_po_responses` | Snowflake node, same column list. |
| `RequestHumanReview`, `NotifyApOutcome`, `NotifyOpsFailure` | Mailpit SMTP | corporate SMTP, or Teams if reviewers prefer it. The resume link mechanism is the same. |

## Things that bit during the build

- `$execution.resumeUrl` on n8n 2.35 already carries `?signature=...`. Appending a second `?` breaks the signature check and the resume webhook answers 401. The review email joins its parameters with `&` after checking for `?`.
- The Information Extractor with a manual schema works against models without function calling, as long as the model returns clean JSON. Numbers came back as numbers and dates as ISO strings from qwen3-coder-30b.
- Postgres Execute Query with Query Parameters set from an expression returning an array binds `$1..$n` cleanly, including JSON strings cast with `::jsonb` in the SQL.
- The Wait node holds the execution until the reviewer clicks. Executions that are never resolved stay in Waiting. Decide on a timeout before production.

## Open questions before production

- SAP integration path for IDocs: PI/PO, CPI, or file drop.
- Snowflake write: direct node write, or Snowpipe through a stage.
- Approved LLM provider and data residency for vendor PDFs.
- Who the AP reviewers are and which channel they want (email, Teams).
- The auto-post threshold and the price and total tolerances. 0.90, 2% and 1% are placeholders.
- Retention and audit expectations for `reconciliations` and the IDoc outbox.

## License

MIT. Fixture vendors, POs and amounts are synthetic.
