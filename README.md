# API Drift Detector - POC

**Config-Driven OpenAPI Contract Monitoring Engine (POC)**

A deterministic, reusable engine for monitoring OpenAPI specs from external providers in Azure DevOps. Detects semantic changes between versions, filters by usage map, and publishes structured diff reports.

## Objectives

- Monitor OpenAPI specs from external providers (starting with VERA - GitHub repo)
- Detect semantic changes between versions
- Filter changes based on what we *actually use* (usage map)
- Publish structured diff reports in CI
- Avoid false positives and PR spam

## High-Level Architecture

- **Engine logic** – Generic, reusable
- **Product configuration** – YAML per API
- **Usage maps** – Per team/product
- **Azure Pipeline** – Wrapper

## Repository Structure

```
/configs
  /products        vera.yaml, template.yaml
  /schema          product.schema.json
/contracts
  /external        Fetched specs (gitignored)
  /baseline        Normalized baseline (gitignored)
  /diff-reports    diff.json, filtered.json, summaries (gitignored)
  /usage-maps      vera.json, etc.
  /collections     Postman/Newman collections (versioned, update here)
  /meta            {product}-latest.json (idempotency, gitignored)
/scripts
  validate-config.js
  fetch-spec.js
  normalize-spec.js
  run-diff.js
  filter-diff.js
  logger.js
azure-pipelines.yml
package.json
```

## Core Flow

1. Validate product config (JSON Schema via AJV)
2. Fetch OpenAPI spec from GitHub release
3. Normalize spec (Redocly bundle → stable format)
4. Compare against baseline using openapi-diff
5. Filter semantic changes via usage map
6. Publish artifacts (diff.json, filtered.json, summary.md)

## Quick Start

```bash
npm install
export PRODUCT=vera
npm run pipeline
```

Or run steps individually:

```bash
export PRODUCT=vera
CONFIG=$(node scripts/validate-config.js)
SPEC=$(node scripts/fetch-spec.js "$CONFIG")
NORM=$(node scripts/normalize-spec.js "$SPEC")
DIFF=$(node scripts/run-diff.js "$NORM")
node scripts/filter-diff.js "$DIFF"
```

## Configuration

**Fetch from repo** (when releases have no assets):

```yaml
productId: vera
specSource:
  type: github_repo
  repo: Aedes-datastandaarden/vera-openapi
  branch: main
  path: Ketenprocessen/VHE.yaml
```

**Fetch from release assets**:

```yaml
specSource:
  type: github_release
  repo: owner/repo
  releaseStrategy: latest
  assetRegex: ".*\\.(ya?ml|json)$"
```

## Usage Map

Only changes affecting listed endpoints are reported:

```json
{
  "endpoints": [
    { "method": "GET", "path": "/v1/vhe/huuropzeggingen" },
    { "method": "GET", "path": "/v1/bdo/contracts" }
  ]
}
```

## Azure Pipeline

- **Trigger:** Manual
- **Parameter:** `Product` (default: vera)
- **Agent:** Ubuntu, Node 20
- **Artifacts:** diff-reports published

Optional: Set `GITHUB_TOKEN` variable for higher rate limits.

## Design Decisions

1. **Config-driven** – Reusable across VHE, BDO, other APIs
2. **Node.js** – Cross-platform, Azure Pipelines native
3. **Deterministic** – No AI in core diff/validation
4. **No PR automation in POC** – Prove value first

## Known Limitations (POC)

- No Postman test generation
- No PR automation
- No Teams notifications
- Usage map updated manually
- Manual trigger only

## Contact

Project owner: Nicolae

Licensed under MIT
