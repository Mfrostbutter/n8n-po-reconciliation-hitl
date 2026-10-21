# Design notes

## Native nodes only

Every step is a node n8n ships. The reasons are practical: a customer's security review clears n8n once and every community package separately; native nodes have official docs and a `typeVersion` history; a rule written as an If, Switch or Edit Fields is one an operator reads on the canvas. Where the first draft used Code nodes, the conversion went:

| Was | Now |
|---|---|
| `NormalizeEmail` (Code) | `NormalizeOutlookEmail` and `NormalizeFixtureEmail` (Edit Fields, one per trigger shape), `RouteAttachment` (Switch), `DecodePdfAttachment` (Convert to File, base64 to binary) |
| `BuildDocumentText` (Code) | `BuildDocumentText` (Edit Fields, JSON mode) |
| `ApplyValidationRules` (Code) | `ValidationInput` (Edit Fields), `SplitOutExtractedLines` (Split Out), `CheckLine` (Edit Fields), `CollectLineChecks` (Aggregate), `GetValidationRules` (Data Table), `CollectRules` (Aggregate), `EvaluateRules` (Edit Fields, one pass/detail pair per rule), `ComputeDecision` (Edit Fields, JSON mode) |
| `BuildIdocXml` (Code) | `PrepareIdocFields` (Edit Fields), `BuildIdocTree` (Edit Fields, JSON mode), `RenderIdocXml` (XML, JSON to XML) |

Edit Fields in JSON mode takes one expression that returns the whole item, so an arrow function inside `{{ }}` carries the few places that need intermediate variables. The output contract of `ComputeDecision` is the same object the Code node produced, so the log, the email and the IDoc did not change.

## Extraction schema

The Information Extractor runs a manual JSON schema. Required fields: `po_number`, `vendor_name`, `document_type`, `currency`, `total_amount`, `line_items`, `extraction_confidence`. Optional: `vendor_reference`, `document_date`, `notes`.

`document_type` is an enum: `order_confirmation`, `invoice`, `shipping_notice`, `other`. It drives the IDoc message type.

Each line item carries `line_no`, `sku`, `description`, `quantity`, `unit_price`, `line_total`, `promised_date`. `sku`, `quantity` and `unit_price` are required.

`extraction_confidence` is the model's own 0 to 1 estimate. It is 30% of the final score by default (`AI_WEIGHT` in the rules table). Treat it as a weak signal and tune it down if the model is overconfident.

## Validation rules

The registry is the data table `om_poc_validation_rules` (seed in `data/validation-rules.json`). Columns: `rule_id`, `name`, `kind` (`rule` or `setting`), `weight`, `hard`, `enabled`, `threshold`, `description`.

| Rule | Check | Weight | Hard | Threshold |
|---|---|---|---|---|
| R1 | PO exists in the system of record | 3 | yes | |
| R2 | Vendor matches the PO vendor (name overlap or sender domain) | 3 | yes | |
| R3 | Currency matches the PO | 2 | | |
| R4 | Line count matches the PO | 1 | | |
| R5 | Every extracted line maps to a PO line by SKU, falling back to line number | 2 | | |
| R6 | Quantities do not exceed the ordered quantity | 2 | | |
| R7 | Unit prices within the threshold of the PO, in percent | 3 | | 2.0 |
| R8 | Document total equals the sum of its lines, within the threshold in currency units | 1 | | 1.0 |
| R9 | Document total within the threshold of the PO total, in percent | 2 | | 1.0 |
| AUTO_THRESHOLD | setting: score at or above this posts without a human | | | 0.90 |
| AI_WEIGHT | setting: share of the score taken from model confidence | | | 0.30 |

Evaluation is split in three nodes so each one reads as a sentence. `ValidationInput` holds the header facts (extraction, PO on record, normalized keys, parsed totals). `CheckLine` runs once per extracted line and records the matching PO line, quantity over, and price drift in percent, with no thresholds applied. `EvaluateRules` has one `pass_<id>` boolean and one `detail_<id>` string per rule, each a short expression over those facts, reading its threshold from the table. `ComputeDecision` joins the verdicts to the enabled `rule` rows, computes the weighted pass ratio, blends in the model confidence, applies the hard-rule override and produces `decision` (`auto` or `review`) and `status`.

`rules_score` is the weighted pass ratio. `score = (1 - AI_WEIGHT) * rules_score + AI_WEIGHT * ai_confidence`. `decision` is `auto` when no hard rule failed and `score >= AUTO_THRESHOLD`, otherwise `review`. A table row without a matching pair in `EvaluateRules` is ignored; a rule with `enabled = false` drops out of the weighting.

Every rule result (pass, weight, hard, detail) is stored in `reconciliations.rule_results` and shown in the reviewer email.

## Reconciliation lifecycle

`reconciliations.status` moves through:

- `auto_approved` or `pending_review` on insert
- `posted` after the IDoc is queued and the landing row written
- `rejected` when the reviewer rejects

`reviewer_decision` is `auto`, `approve` or `reject`. `idoc_number` is set on post.

## IDoc mapping

`PrepareIdocFields` sets the IDoc number (UTC timestamp plus reconciliation id), the message type and the Snowflake landing row. `BuildIdocTree` lays the ORDERS05 document out as JSON with attributes under `attr`; `RenderIdocXml` (the XML node, root `ORDERS05`, attribute key `attr`) serializes and escapes it.

| Segment | Source |
|---|---|
| `EDI_DC40` | generated: `DOCNUM` from timestamp plus reconciliation id, `MESTYP` from document type (ORDRSP, INVOIC, DESADV), fixed sender and receiver ports |
| `E1EDK01` | currency, vendor reference |
| `E1EDK02` qualifier 001 | PO number and PO date from the system of record |
| `E1EDK02` qualifier 002 | vendor document number and date |
| `E1EDKA1` partner LF | vendor id and name from the system of record |
| `E1EDP01` per line | position from the matched PO line, quantity, unit price, net value |
| `E1EDP19` qualifier 002 | SKU and description |
| `E1EDP20` | promised quantity and date, when the vendor gave one |
| `E1EDS01` sum 002 | document total and currency |

Sender and receiver port and partner values are placeholders. Replace them with the real logical systems before pointing at SAP.

## Human-in-the-loop mechanics

`RequestHumanReview` renders a side-by-side (rules, lines, header) and two links built from `$execution.resumeUrl` with `decision=approve` or `decision=reject` appended with `&`. `WaitForReviewerDecision` resumes on a GET to that URL. `ReviewerApproved` reads `$json.query.decision`.

The links are single-use and die with the execution. There is no timeout in the template. Options for production: a Wait with a time limit on a parallel branch, a scheduled sweep of `pending_review` rows older than N hours, or a reminder email.

## Error handling

Every node that can fail has On Error set to continue on an error output, and that output routes to `NotifyOpsFailure`. Ops receives the error message, the email subject and sender, and the execution id. No posting happens on the failure path. An instance-level error workflow is a sensible addition for anything that fails before the first node.
