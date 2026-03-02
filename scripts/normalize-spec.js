#!/usr/bin/env node
/**
 * Normalizes OpenAPI spec using Redocly bundle (resolve $ref, stable format)
 * Expects: PRODUCT env var, spec path from fetch-spec
 * Skips if normalize.enabled is false in config
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
const specPath = process.argv[2];

if (!product || !specPath) {
  error('PRODUCT env var and spec path argument required');
  process.exit(1);
}

const configPath = resolve(ROOT, 'configs', 'products', `${product}.yaml`);
const config = yaml.load(readFileSync(configPath, 'utf8'));

if (config.normalize?.enabled === false) {
  info('Normalization disabled, skipping');
  console.log(specPath);
  process.exit(0);
}

const baselineDir = resolve(ROOT, 'contracts', 'baseline');
mkdirSync(baselineDir, { recursive: true });
const outPath = resolve(baselineDir, `${product}-normalized.json`);

const redocly = spawnSync(
  'npx',
  ['redocly', 'bundle', specPath, '--output', outPath, '--ext', 'json'],
  { stdio: 'inherit', cwd: ROOT }
);

if (redocly.status !== 0) {
  error('Redocly bundle failed');
  process.exit(1);
}

info('Spec normalized', { input: specPath, output: outPath });
console.log(outPath);
