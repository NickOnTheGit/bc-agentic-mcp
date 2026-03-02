# Current POC Capabilities

## What Works Now

### 1. Config-driven product setup
- **Product config** (YAML) per API with JSON Schema validation (AJV)
- **Usage map** (JSON) listing endpoints we actually use
- Add new APIs by adding config + usage map; no engine code changes

### 2. Spec fetching
- **github_release** – Fetch OpenAPI spec from GitHub release assets
- **github_repo** – Fetch OpenAPI spec from a file in the repo (branch + path)
- Optional `GITHUB_TOKEN` for higher rate limits

### 3. Spec normalization
- Redocly `bundle` – resolve `$ref`, produce stable output
- Reduces diff noise from reference vs inline differences
- Can be disabled per product

### 4. Semantic diff
- `openapi-diff` – compare specs semantically (not raw file diff)
- Output: breaking, non-breaking, unclassified
- First run: initializes baseline, no diff

### 5. Usage map filtering
- Filter diff results to endpoints in the usage map
- Lowers noise from changes to unused endpoints

### 6. Idempotency (github_release only)
- Store last processed tag in `contracts/meta/{product}-latest.json`
- Exit early if tag unchanged (skips re-fetch)

### 7. Azure Pipeline
- Manual trigger with `Product` parameter
- Steps: validate → fetch → normalize → diff → filter
- Publishes diff reports as artifacts (`diff.json`, `filtered.json`, summaries)

### 8. Collections storage
- `contracts/collections/` – place for Postman/Newman collections
- Versioned in repo, update when APIs change

---

## What Is Not in This POC

| Feature | Status |
|---------|--------|
| Postman test generation | Future (Phase 4) |
| PR automation | Future (Phase 5) |
| AI-generated summaries | Future (Phase 6) |
| Teams/email notifications | Not planned |
| Scheduled runs | Not yet (manual trigger) |
| Auto-update usage map | Manual |
| OAuth flow handling | N/A |
| Multi-spec per product (e.g. VHE + BDO) | One spec per product config; add separate products |

---

## Artifacts Produced

| File | Location | Description |
|------|----------|-------------|
| diff.json | contracts/diff-reports/ | Full semantic diff |
| filtered.json | contracts/diff-reports/ | Diff filtered by usage map |
| summary.md | contracts/diff-reports/ | Human-readable diff summary |
| filtered-summary.md | contracts/diff-reports/ | Filtered summary |

---

## Quick Reference

```bash
# Run full pipeline locally
export PRODUCT=vera
npm run pipeline

# Or step by step
CONFIG=$(node scripts/validate-config.js)
SPEC=$(node scripts/fetch-spec.js "$CONFIG")
NORM=$(node scripts/normalize-spec.js "$SPEC")
DIFF=$(node scripts/run-diff.js "$NORM")
node scripts/filter-diff.js "$DIFF"
```
