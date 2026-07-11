---
name: document-processing
description: Turn uploaded company documents (catalogs, price lists, past sales, contact lists, proposals) into validated structured records — products, contacts, sales history — ready for the Company Brain.
version: 1.0.0
author: Interfaze
metadata:
  hermes:
    tags: [sales, documents, extraction, ingestion, products, contacts]
    category: sales
---

# Document Processing & Product Extraction

Process a typed document upload (§7.6 document types) into structured data.
Covers both the `document_processing` and `product_extraction` run types.

## Per document type

- **product_catalog / technical_sheet** → product records (§6.3 fields:
  name, category, description, specs, materials, certifications, target
  industries, buyer roles left for brain-build). One record per product,
  deduped against existing products by normalized name.
- **price_list** → price ranges attached to products; never expose raw price
  lists to outreach content, only ranges flagged `internal`.
- **past_sales / past_customers / lost_deals** → sales-history records
  (customer, country, product, value/volume when present, outcome). These
  power the brain's market assumptions — capture country and product even
  when other fields are missing.
- **current_contacts / dealer_list / distributor_list** → contact records
  (§6.5 fields), validated: email regex, phone → E.164, country → ISO
  3166-1 alpha-2, relationship_type set from list type.
- **proposal_example / email_examples** → tone/argument corpus for the brain's
  sales arguments; extract claims and phrasings, not full documents.
- **certificate / case_study** → certification and proof-point records.

## Rules

- **Extract, don't summarize.** Output is records matching the product schema,
  each with a `source` pointer (document id + page/row) so a human can audit.
- **Validation is a gate**: invalid rows go to a rejects list with reasons —
  visible in the processing status (§7.6), never silently dropped.
- **Ambiguity is flagged, not guessed**: unreadable price, uncertain country,
  duplicate-looking contact → mark `needs_review`, keep going.
- Multi-language documents are normal; extract in the source language and
  record the language per field where it matters (names stay verbatim).
- Processing is idempotent per document: re-running replaces that document's
  records, never duplicates them.
- Read-only toward the outside world; nothing leaves the tenant.
