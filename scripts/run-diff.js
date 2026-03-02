#!/usr/bin/env node
/**
 * Runs openapi-diff between baseline and fetched spec
 * Produces diff.json and summary
 * Exits early if no baseline (first run) - initializes baseline
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import { spawnSync } from 'child_process';
import yaml from 'js-yaml';
import { info, error } from './logger.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..');

const product = process.env.PRODUCT;
const newSpecPath = process.argv[2];

if (!product || !newSpecPath) {
  error('PRODUCT env var and spec path argument required');
  process.exit(1);
}

const baselinePath = resolve(ROOT, 'contracts', 'baseline', `${product}-baseline.json`);
const diffReportsDir = resolve(ROOT, 'contracts', 'diff-reports');
mkdirSync(diffReportsDir, { recursive: true });
const diffJsonPath = resolve(diffReportsDir, `${product}-diff.json`);
const summaryPath = resolve(diffReportsDir, `${product}-summary.md`);

// First run: no baseline yet - copy normalized spec as baseline
if (!existsSync(baselinePath)) {
  info('No baseline found, initializing', { baselinePath });
  const content = readFileSync(newSpecPath, 'utf8');
  writeFileSync(baselinePath, content);
  const summary = `# ${product} - Initial Baseline\n\nNo previous baseline. Current spec saved as baseline.\n`;
  writeFileSync(summaryPath, summary);
  writeFileSync(diffJsonPath, JSON.stringify({ breaking: [], nonBreaking: [], unclassified: [], message: 'Initial baseline' }, null, 2));
  info('Baseline initialized');
  console.log(diffJsonPath);
  process.exit(0);
}

const openapiDiff = spawnSync(
  'npx',
  ['openapi-diff', baselinePath, newSpecPath],
  { encoding: 'utf8', cwd: ROOT }
);

// openapi-diff exits 1 when breaking changes found - we still want the output
// Output format: breakingDifferences, nonBreakingDifferences, unclassifiedDifferences
let diffResult;
if (openapiDiff.stdout) {
  try {
    const raw = JSON.parse(openapiDiff.stdout);
    diffResult = {
      breaking: raw.breakingDifferences ?? [],
      nonBreaking: raw.nonBreakingDifferences ?? [],
      unclassified: raw.unclassifiedDifferences ?? [],
      breakingDifferencesFound: raw.breakingDifferencesFound ?? false
    };
  } catch (e) {
    diffResult = { breaking: [], nonBreaking: [], unclassified: [], raw: openapiDiff.stdout };
  }
} else {
  diffResult = { breaking: [], nonBreaking: [], unclassified: [], stderr: openapiDiff.stderr };
}

writeFileSync(diffJsonPath, JSON.stringify(diffResult, null, 2));

const lines = [
  `# ${product} - API Diff Summary`,
  '',
  `**Breaking:** ${diffResult.breaking?.length ?? 0}`,
  `**Non-breaking:** ${diffResult.nonBreaking?.length ?? 0}`,
  `**Unclassified:** ${diffResult.unclassified?.length ?? 0}`,
  '',
  '## Breaking Changes',
  ...(diffResult.breaking?.map(b => `- ${typeof b === 'string' ? b : JSON.stringify(b)}`) ?? ['(none)']),
  '',
  '## Non-Breaking Changes',
  ...(diffResult.nonBreaking?.map(n => `- ${typeof n === 'string' ? n : JSON.stringify(n)}`) ?? ['(none)'])
];
writeFileSync(summaryPath, lines.join('\n'));

info('Diff completed', { breaking: diffResult.breaking?.length ?? 0, nonBreaking: diffResult.nonBreaking?.length ?? 0 });
console.log(diffJsonPath);
