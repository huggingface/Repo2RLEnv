import { readFileSync } from 'node:fs';
import { join } from 'node:path';

/** The released package version, read from the repository's pyproject.toml at build time. */
export const version = (() => {
  const pyproject = readFileSync(join(process.cwd(), '..', 'pyproject.toml'), 'utf8');
  const match = /^version\s*=\s*"([^"]+)"/m.exec(pyproject);
  if (!match) throw new Error('version not found in pyproject.toml');
  return match[1];
})();
