# Design notes

## Extraction schema

The Information Extractor runs a manual JSON schema. Required fields: `po_number`, `vendor_name`, `document_type`, `currency`, `total_amount`, `line_items`, `extraction_confidence`. Optional: `vendor_reference`, `document_date`, `notes`.

`document_type` is an enum: `order_confirmation`, `invoice`, `shipping_notice`, `other`. It drives the IDoc message type.

Each line item carries `line_no`, `sku`, `description`, `quantity`, `unit_price`, `line_total`, `promised_date`. `sku`, `quantity` and `unit_price` are required.

`extraction_confidence` is the model's own 0 to 1 estimate. It is 30% of the final score by default. Treat it as a weak signal and tune `AI_WEIGHT` down if the model is overconfident.

## Validation rules

| Rule | Check | Weight | Hard |
|---|---|---|---|
| R1 | PO exists in the system of record | 3 | yes |
| R2 | Vendor matches the PO vendor (name overlap or sender domain) | 3 | yes |
| R3 | Currency matches the PO | 2 | |
| R4 | Line count matches the PO | 1 | |
| R5 | Every extracted line maps to a PO line by SKU, falling back to line number | 2 | |
| R6 | Quantities do not exceed the ordered quantity | 2 | |
| R7 | Unit prices within `PRICE_TOLERANCE_PCT` of the PO | 3 | |
| R8 | Document total equals the sum of its lines, within one currency unit | 1 | |
| R9 | Document total within `TOTAL_TOLERANCE_PCT` of the PO total | 2 | |

`rules_score` is the weighted pass ratio. `score = (1 - AI_WEIGHT) * rules_score + AI_WEIGHT * ai_confidence`. `decision` is `auto` when no hard rule failed and `score >= AUTO_THRESHOLD`, otherwise `review`.

Every rule result (pass, weight, hard, detail) is stored in `reconciliations.rule_results` and shown in the reviewer email.

## Reconciliation lifecycle

`reconciliations.status` moves through:

- `auto_approved` or `pending_review` on insert
- `posted` after the IDoc is queued and the landing row written
- `rejected` when the reviewer rejects

`reviewer_decision` is `auto`, `approve` or `reject`. `idoc_number` is set on post.

## IDoc mapping

`BuildIdocXml` produces an ORDERS05 document.

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
