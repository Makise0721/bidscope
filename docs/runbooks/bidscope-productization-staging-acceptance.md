# BidScope Productization Staging Acceptance Runbook

**Scope:** First internal-team pilot: weekly, manually reviewed CCGP central
public tender excerpts.

## Admission checklist

- [ ] Data owner and authorization basis are recorded outside the repository.
- [ ] Coverage, retention, correction/takedown and weekly update SLA are
  approved.
- [ ] Bundle uses `source=ccgp`,
  `capture_kind=curated_public_excerpt`, `schema_version=2` and a safe
  `batch_id`.
- [ ] `data_contract.review_status=approved` and `reviewed_at` is timezone-aware.
- [ ] Manifest and payload hashes match; no undeclared files, symlinks, URLs with
  credentials or synthetic IDs are present.

## Staging sequence

1. Copy the controlled bundle into a staging-only input directory.
2. Run `bidscope snapshots inspect <bundle> --json`.
3. Stop on `disposition=quarantined`; fix the source package through the
   authorized acquisition process and inspect again.
4. Run `bidscope snapshots import <bundle> --json`.
5. Save the JSON result in the restricted staging evidence store. It must contain
   the manifest hash, notice count, payload-file count, warnings and import ID.
6. Re-run the same import and confirm the import ID is unchanged and no new
   notice version is created.
7. Generate one cited report and confirm every claim resolves to the imported
   immutable evidence span.

## Evaluation sequence

1. Create a restricted dataset manifest referencing only approved snapshot
   bundle IDs and SHA-256 hashes.
2. Run the offline baseline and record the dataset, model, prompt and pricing
   metadata in a `real-evaluation-result-v1` artifact.
3. Validate the pair with:

   ```bash
   bidscope eval validate-real \
     --manifest /controlled/staging/evaluation/dataset-manifest.json \
     --result /controlled/staging/evaluation/result.json \
     --json
   ```

4. Review retrieval, deduplication, citation support, latency, cost and human
   usefulness separately from deterministic fixture consistency.
5. If live-model evaluation is approved, run it only in staging with bounded
   cost and record provider/model/prompt versions and failure codes. Never put
   prompts, raw reports, credentials or request headers in the result artifact.

## Release block conditions

Block the pilot when any of the following is true:

- no approved governance record or no approved data contract;
- any provenance, hash, evidence or citation-support failure;
- real evaluation manifest/result linkage is not reproducible;
- `validate-real` reports `status=blocked`;
- backup verification, external replication or clean-host recovery evidence is
  missing;
- the operator would need a live scraper, captcha bypass, source probing,
  multi-tenant auth or production credentials in Git.

