# n8n PO Reconciliation: vendor email to IDoc, with a human-in-the-loop gate

An n8n workflow template for the purchase-order reconciliation pattern:

1. A non-EDI vendor response (order confirmation, invoice, shipping notice) arrives by email, as text or a PDF attachment.
2. An LLM extracts the fields against a fixed JSON schema.
3. The PO on record is fetched and nine weighted business rules, read from a data table, score the extraction.
4. Score at or above the threshold with no hard failure posts automatically. Anything else goes to a reviewer who approves or rejects from an email.
5. Validated data becomes an ORDERS05 IDoc queued for SAP and a landing row for Snowflake. Every attempt is logged.

Built from n8n's own nodes only: no Code nodes, no community nodes. Every rule, mapping and shape change is an Edit Fields, Switch, Split Out, Aggregate, Data Table, Convert to File or XML node you can read on the canvas. Built and verified on n8n 2.35.5 (self-hosted). All external systems are swapped for lab stand-ins so the whole thing runs on a laptop: Postgres plays SAP and Snowflake, a webhook plays the Outlook mailbox, Mailpit catches the mail, and any OpenAI-compatible endpoint serves the model. The swap points are listed below.

## Repo layout

| Path | What |
|---|---|
| `workflows/po-reconciliation-email-to-idoc.json` | The workflow. Import it into n8n. 34 nodes plus canvas notes. |
| `data/schema.sql` | Schema `om_poc`: vendors, POs, PO lines, reconciliations, IDoc outbox, Snowflake mirror. Seeds four POs. |
| `data/validation-rules.json` | The rule registry and the two settings that the data table `om_poc_validation_rules` is seeded from. |
| `data/fixtures/` | Three vendor emails that exercise the three paths, plus the PDF invoice one of them attaches. |
| `scripts/seed-validation-rules.py` | Creates the data table through the public API and upserts the rules. Python standard library only. |
| `scripts/send-fixture.py` | POSTs a fixture to the webhook, base64-encoding the PDF. Python standard library only. |
| `docs/design-notes.md` | Rules, scoring, IDoc mapping, open questions for production. |

## Quick start

Prerequisites: n8n 2.x with the public API enabled, Postgres, an SMTP sink (Mailpit works), and an OpenAI-compatible chat endpoint. The lab used LM Studio with `qwen/qwen3-coder-30b`; any model that returns well-formed JSON for the Information Extractor works.

1. Create a database and load the schema.

   ```
   createdb om_poc
   psql om_poc -f data/schema.sql
   ```

2. Create the rules data table. The script creates `om_poc_validation_rules` in your personal project (or `--project "<team project>"`) and loads the eleven rows.

   ```
   N8N_API_URL=http://localhost:5678 N8N_API_KEY=<your key> python3 scripts/seed-validation-rules.py
   ```

3. Create three credentials in n8n, in the same project as the table. The workflow references them by these names, with placeholder ids, so the import will ask you to pick them.

   | Credential name in the workflow | Type | Used by |
   |---|---|---|
   | `Lab Postgres om_poc` | Postgres | all database nodes |
   | `LM Studio (lab)` | OpenAI API (set Base URL to your endpoint) | `LmStudioExtractorModel` |
   | `Lab Mailpit SMTP` | SMTP | reviewer, AP and ops emails |

4. Import `workflows/po-reconciliation-email-to-idoc.json` (Workflow menu, Import from File). Open each credential-bearing node once and select the credential. Open `GetValidationRules` once and confirm it resolves the table (it binds by name; pick it from the list if not).

5. Change the model id on `LmStudioExtractorModel` if yours differs, then activate the workflow.

6. Send the fixtures.

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

The canvas is split into seven zones, each framed by a sticky note.

| Zone | Nodes | Notes |
|---|---|---|
| 1 Intake | `WatchOutlookInbox` (disabled), `ReceiveVendorEmailFixture`, `NormalizeOutlookEmail`, `NormalizeFixtureEmail`, `RouteAttachment` | One Edit Fields per trigger maps the source into a shared envelope. The Switch sends a base64 PDF to the decoder, a binary PDF straight to the extractor, plain text onward. Downstream nodes read the envelope from `$('RouteAttachment')`. |
| 2 Decipher | `DecodePdfAttachment`, `ExtractPdfText`, `BuildDocumentText`, `ExtractPoFieldsWithAi`, `LmStudioExtractorModel` | Convert to File turns the base64 into a file, Extract from File pulls the text, Edit Fields (JSON mode) joins it with the email body. The Information Extractor runs a manual JSON schema. No agent loop, no tools. |
| 3 Match lines | `LookupPurchaseOrder`, `ValidationInput`, `SplitOutExtractedLines`, `CheckLine`, `CollectLineChecks` | PO lookup has Always Output Data on so a missing PO still reaches the rules. `ValidationInput` holds the header facts. Split Out gives one item per extracted line, `CheckLine` finds its PO line and measures quantity and price drift, Aggregate folds the checks back into one item. |
| 4 Score | `GetValidationRules`, `CollectRules`, `EvaluateRules`, `ComputeDecision`, `LogReconciliation`, `RespondWithDecision` | The Data Table node reads the rule registry. `EvaluateRules` is one Edit Fields node with a `pass_<id>` and `detail_<id>` pair per rule. `ComputeDecision` joins the verdicts to the table rows: weighted score, model share, hard-rule override. |
| 5 HITL gate | `MeetsAutoThreshold`, `RequestHumanReview`, `WaitForReviewerDecision`, `ReviewerApproved` | Wait node resumes on a webhook. Decision comes from the `decision` query parameter. |
| 6 Convert and post | `PrepareIdocFields`, `BuildIdocTree`, `RenderIdocXml`, `QueueIdocForSap`, `WriteSnowflakeLandingRow`, `MarkReconciliationPosted`, `MarkReconciliationRejected`, `NotifyApOutcome` | The IDoc is laid out as a JSON tree (attributes under `attr`) and the XML node renders and escapes it. Message type follows document type: ORDRSP, INVOIC or DESADV. |
| 7 Failures | `NotifyOpsFailure` | Every fallible node routes its error output here. Nothing is posted on a failure. |

## Tuning

Everything that tunes the gate is a row in the data table `om_poc_validation_rules`. Edit the table in the n8n UI; the next execution picks it up. No workflow change.

| Column | Meaning |
|---|---|
| `rule_id` | `R1` to `R9`, or a setting name |
| `kind` | `rule` (scored) or `setting` (threshold only) |
| `weight` | share of the rules score |
| `hard` | a failed hard rule forces review at any score |
| `enabled` | `false` drops the rule from the score |
| `threshold` | R7 price drift in percent, R8 allowed difference in currency units, R9 total drift in percent; `AUTO_THRESHOLD` 0.90, `AI_WEIGHT` 0.30 |

Score = (1 − AI_WEIGHT) × weighted rule pass ratio + AI_WEIGHT × model confidence. A failed hard rule (R1 PO exists, R2 vendor matches) always forces review regardless of score. Adding a rule is a new row plus a matching `pass_<id>` / `detail_<id>` pair in `EvaluateRules`; a row without a pair is ignored.

## Swap points for production

| Node | Lab stand-in | Production |
|---|---|---|
| `WatchOutlookInbox` | disabled; `ReceiveVendorEmailFixture` webhook plays the mailbox | Microsoft Outlook trigger on the AP mailbox with an OAuth2 credential, plus a Get Attachment step before `NormalizeOutlookEmail` so the PDF arrives in `$binary.attachment`. |
| `LmStudioExtractorModel` | LM Studio, local | the approved provider: Anthropic, Azure OpenAI, Bedrock, OpenRouter, or a hosted OpenAI-compatible endpoint. Swap the chat model sub-node, keep the Information Extractor. |
| `LookupPurchaseOrder` | Postgres `purchase_orders` + `po_lines` | SAP OData or RFC read of EKKO/EKPO. Keep the output shape (`po_number`, `vendor_id`, `vendor_name`, `email_domain`, `currency`, `order_date`, `total_amount`, `lines[]`) and nothing downstream changes. |
| `QueueIdocForSap` | Postgres `idoc_outbox` | HTTP Request to the SAP PI/PO or CPI IDoc endpoint, or a file drop. The XML is already built. |
| `WriteSnowflakeLandingRow` | Postgres `reconciled_po_responses` | Snowflake node, same column list. |
| `RequestHumanReview`, `NotifyApOutcome`, `NotifyOpsFailure` | Mailpit SMTP | corporate SMTP, or Teams if reviewers prefer it. The resume link mechanism is the same. |

## Things that bit during the build

- `$execution.resumeUrl` on n8n 2.35 already carries `?signature=...`. Appending a second `?` breaks the signature check and the resume webhook answers 401. The review email joins its parameters with `&` after checking for `?`.
- An Edit Fields node in JSON mode takes one expression that returns the whole item. Inside it, never let two closing braces touch (`} }`, not `}}`), because `}}` ends the expression early.
- The Data Table node binds its table by id in the JSON n8n exports, and ids are per instance. This template binds by name instead; the seed script creates the table so the name exists before the import.
- Split Out on an empty list emits nothing, which would skip everything after it. `SplitOutExtractedLines` has Always Output Data on and `CheckLine` marks the placeholder item `empty`, which the rules filter out.
- The XML node serializes the whole input item, so the IDoc tree is built in its own Edit Fields node and the header fields (IDoc number, message type, the Snowflake row) sit in the node before it.
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
