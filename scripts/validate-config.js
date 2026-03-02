#!/usr/bin/env node
/**
 * Validates product configuration against JSON Schema (AJV)
 * Expects: PRODUCT env var (e.g. vera)
 * Outputs: config path to stdout for next step
 */

import Ajv from 'ajv';
import { readFileSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import yaml from 'js-yaml';
import { info, error } from './logger.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..');

const product = process.env.PRODUCT;
if (!product) {
  error('PRODUCT env var is required');
  process.exit(1);
}

const configPath = resolve(ROOT, 'configs', 'products', `${product}.yaml`);
if (!existsSync(configPath)) {
  error(`Product config not found: ${configPath}`);
  process.exit(1);
}

const schemaPath = resolve(ROOT, 'configs', 'schema', 'product.schema.json');
const schema = JSON.parse(readFileSync(schemaPath, 'utf8'));
const configRaw = readFileSync(configPath, 'utf8');
const config = yaml.load(configRaw);

const ajv = new Ajv({ strict: false });
const validate = ajv.compile(schema);

if (!validate(config)) {
  error('Config validation failed', { errors: validate.errors });
  process.exit(1);
}

info('Config validated', { product, configPath });
console.log(configPath);
