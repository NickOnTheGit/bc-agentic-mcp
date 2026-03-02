#!/usr/bin/env node
/**
 * Fetches OpenAPI spec from GitHub
 * Supports: github_release (release assets) | github_repo (raw file from branch)
 * Expects: PRODUCT env var, config path from validate-config
 * Uses: GITHUB_TOKEN env var optionally for rate limit
 * Outputs: path to fetched spec
 */

import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import yaml from 'js-yaml';
import { info, error } from './logger.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(__dirname, '..');

const product = process.env.PRODUCT;
const configPath = process.argv[2] || resolve(ROOT, 'configs', 'products', `${product}.yaml`);

if (!product) {
  error('PRODUCT env var is required');
  process.exit(1);
}

const config = yaml.load(readFileSync(configPath, 'utf8'));
const { type, repo } = config.specSource;
const [owner, repoName] = repo.split('/');

const headers = { Accept: 'application/vnd.github+json' };
if (process.env.GITHUB_TOKEN) {
  headers['Authorization'] = `Bearer ${process.env.GITHUB_TOKEN}`;
}

const externalDir = resolve(ROOT, 'contracts', 'external');
const metaDir = resolve(ROOT, 'contracts', 'meta');
const metaPath = resolve(metaDir, `${product}-latest.json`);
mkdirSync(externalDir, { recursive: true });
mkdirSync(metaDir, { recursive: true });

let outPath;
let versionId;

if (type === 'github_repo') {
  const { branch = 'main', path: specPath } = config.specSource;
  if (!specPath) {
    error('specSource.path is required for github_repo');
    process.exit(1);
  }
  const rawUrl = `https://raw.githubusercontent.com/${owner}/${repoName}/${branch}/${specPath}`;
  info('Fetching spec from repo', { url: rawUrl });
  const res = await fetch(rawUrl);
  if (!res.ok) {
    error('Failed to fetch spec from repo', { status: res.status, url: rawUrl });
    process.exit(1);
  }
  const ext = specPath.endsWith('.json') ? 'json' : 'yaml';
  versionId = branch;
  outPath = resolve(externalDir, `${product}-${branch.replace(/\//g, '-')}.${ext}`);
  const buf = Buffer.from(await res.arrayBuffer());
  writeFileSync(outPath, buf);
  info('Spec fetched', { path: outPath });
} else if (type === 'github_release') {
  const { releaseStrategy = 'latest', assetRegex = '.*\\.(ya?ml|json)$' } = config.specSource;
  const releasesUrl = `https://api.github.com/repos/${owner}/${repoName}/releases`;
  const res = await fetch(releasesUrl, { headers });
  if (!res.ok) {
    error('GitHub API error', { status: res.status, url: releasesUrl });
    process.exit(1);
  }
  const releases = await res.json();
  if (!releases.length) {
    error('No releases found');
    process.exit(1);
  }
  const release = releaseStrategy === 'latest' ? releases[0] : releases.find(r => r.tag_name === releaseStrategy);
  if (!release) {
    error('Release not found', { releaseStrategy });
    process.exit(1);
  }
  if (existsSync(metaPath)) {
    const meta = JSON.parse(readFileSync(metaPath, 'utf8'));
    if (meta.lastTag === release.tag_name && existsSync(meta.specPath)) {
      info('Tag unchanged, exiting early (idempotency)');
      console.log(meta.specPath);
      process.exit(0);
    }
  }
  const regex = new RegExp(assetRegex);
  const asset = release.assets?.find(a => regex.test(a.name));
  if (!asset) {
    error('No matching asset found', { assetRegex, assets: release.assets?.map(a => a.name) });
    process.exit(1);
  }
  info('Fetching spec from release', { tag: release.tag_name, asset: asset.name });
  const specRes = await fetch(asset.browser_download_url, { headers: { ...headers, Accept: 'application/octet-stream' } });
  if (!specRes.ok) {
    error('Failed to download asset', { status: specRes.status });
    process.exit(1);
  }
  versionId = release.tag_name.replace(/^v/, '');
  const ext = asset.name.endsWith('.json') ? 'json' : 'yaml';
  outPath = resolve(externalDir, `${product}-${versionId}.${ext}`);
  writeFileSync(outPath, Buffer.from(await specRes.arrayBuffer()));
  info('Spec fetched', { path: outPath });
} else {
  error('Unknown specSource type', { type });
  process.exit(1);
}

const meta = { lastTag: versionId, lastFetched: new Date().toISOString(), specPath: outPath };
writeFileSync(metaPath, JSON.stringify(meta, null, 2));
console.log(outPath);
