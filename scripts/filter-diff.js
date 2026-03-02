#!/usr/bin/env node
/**
 * Filters diff results by usage map - only keep changes affecting used endpoints
 * Expects: PRODUCT env var, diff.json path from run-diff
 * Outputs: filtered.json, filtered-summary.md
 */

import { readFileSync, writeFileSync, mkdirSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import yaml from 'js-yaml';
import { info } from './logger.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..');

const product = process.env.PRODUCT;
const diffPath = process.argv[2];

if (!product || !diffPath) {
  console.error('PRODUCT env var and diff path argument required');
  process.exit(1);
}

const configPath = resolve(ROOT, 'configs', 'products', `${product}.yaml`);
const config = yaml.load(readFileSync(configPath, 'utf8'));
const usageMapPath = resolve(ROOT, config.usageMapPath);
const usageMap = JSON.parse(readFileSync(usageMapPath, 'utf8'));
const endpoints = usageMap.endpoints ?? [];
const endpointKeys = new Set(endpoints.map(e => `${e.method}:${normalizePath(e.path)}`));

function normalizePath(path) {
  return path.replace(/\{[^}]+\}/g, '{id}');
}

function diffAffectsEndpoint(diff) {
  const loc = diff.sourceSpecEntityDetails?.[0]?.location ?? diff.destinationSpecEntityDetails?.[0]?.location ?? '';
  const pathMatch = loc.match(/paths\.([^\s/]+(?:\/\{[^}]+\})*(?:\/[^\s/]+)*)/);
  if (!pathMatch) return true; // Keep unclassified/unknown
  const path = pathMatch[1];
  for (const ep of endpoints) {
    const epPath = normalizePath(ep.path);
    const locPath = path.replace(/^\/+/, '/');
    if (locPath.startsWith(epPath.replace(/^\/+/, '/')) || epPath.replace(/^\/+/, '/').startsWith(locPath)) {
      return true;
    }
  }
  return false;
}

function filterDiffs(diffs) {
  if (!Array.isArray(diffs)) return [];
  return diffs.filter(d => {
    const loc = JSON.stringify(d);
    // Match path references in diff (e.g. paths./v1/vhe/huuropzeggingen)
    for (const ep of endpoints) {
      const pathPart = ep.path.replace(/\{[^}]+\}/g, '[^/]+');
      const re = new RegExp(pathPart.replace(/\//g, '\\/'));
      if (re.test(loc)) return true;
    }
    return false;
  });
}

const diffResult = JSON.parse(readFileSync(diffPath, 'utf8'));

const filteredBreaking = filterDiffs(diffResult.breaking);
const filteredNonBreaking = filterDiffs(diffResult.nonBreaking);
const filteredUnclassified = filterDiffs(diffResult.unclassified);

const filtered = {
  breaking: filteredBreaking,
  nonBreaking: filteredNonBreaking,
  unclassified: filteredUnclassified,
  breakingDifferencesFound: filteredBreaking?.length > 0
};

const diffReportsDir = resolve(ROOT, 'contracts', 'diff-reports');
const filteredPath = resolve(diffReportsDir, `${product}-filtered.json`);
const filteredSummaryPath = resolve(diffReportsDir, `${product}-filtered-summary.md`);

writeFileSync(filteredPath, JSON.stringify(filtered, null, 2));

const lines = [
  `# ${product} - Filtered API Diff (Usage Map)`,
  '',
  'Only changes affecting usage-map endpoints are shown.',
  '',
  `**Breaking:** ${filtered.breaking?.length ?? 0}`,
  `**Non-breaking:** ${filtered.nonBreaking?.length ?? 0}`,
  `**Unclassified:** ${filtered.unclassified?.length ?? 0}`,
  '',
  '## Breaking Changes',
  ...(filtered.breaking?.map(b => `- ${typeof b === 'string' ? b : JSON.stringify(b)}`) ?? ['(none)']),
  '',
  '## Non-Breaking Changes',
  ...(filtered.nonBreaking?.map(n => `- ${typeof n === 'string' ? n : JSON.stringify(n)}`) ?? ['(none)'])
];
writeFileSync(filteredSummaryPath, lines.join('\n'));

info('Diff filtered', { breaking: filtered.breaking?.length, nonBreaking: filtered.nonBreaking?.length });
console.log(filteredPath);
