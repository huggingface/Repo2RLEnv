// Check every internal link in the static export: the page must exist and, for
// `#anchors`, the target heading id must exist on it. This replaces MkDocs'
// `--strict` link checking and runs against exactly what ships.
//
//   node tools/check-links.mjs            # after `next build` (reads ./out)
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join, posix, relative } from 'node:path';

const out = join(import.meta.dirname, '..', 'out');
const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? '';
// Generated per-page variants; their links mirror the pages they come from.
const skipDirs = new Set(['_next', 'llms.mdx', 'og']);

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      if (!skipDirs.has(name)) yield* walk(path);
    } else if (name.endsWith('.html')) {
      yield path;
    }
  }
}

const idCache = new Map();
function idsOf(file) {
  if (!idCache.has(file)) {
    const html = readFileSync(file, 'utf8');
    idCache.set(file, new Set([...html.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1])));
  }
  return idCache.get(file);
}

function targetFile(urlPath) {
  const clean = decodeURIComponent(urlPath).replace(/^\/+/, '');
  const candidates = [join(out, clean, 'index.html'), join(out, clean), join(out, `${clean}.html`)];
  return candidates.find((c) => existsSync(c) && statSync(c).isFile());
}

const errors = [];
let checked = 0;
for (const file of walk(out)) {
  const pageUrl = '/' + relative(out, file).split('\\').join('/').replace(/index\.html$/, '');
  const html = readFileSync(file, 'utf8');
  for (const [, raw] of html.matchAll(/<a\s[^>]*href="([^"]+)"/g)) {
    const href = raw.replaceAll('&amp;', '&');
    if (/^(https?:|mailto:|tel:|javascript:|data:)/.test(href)) continue;
    checked++;
    const [pathPart, hash] = href.split('#');
    let path = pathPart === '' ? pageUrl : posix.resolve(pageUrl, pathPart);
    if (basePath && path.startsWith(basePath)) path = path.slice(basePath.length) || '/';
    const target = targetFile(path);
    if (!target) {
      errors.push(`${pageUrl}: broken link → ${href}`);
      continue;
    }
    if (hash && target.endsWith('.html') && !idsOf(target).has(decodeURIComponent(hash))) {
      errors.push(`${pageUrl}: missing anchor → ${href}`);
    }
  }
}

if (errors.length) {
  console.error(errors.sort().join('\n'));
  console.error(`\n${errors.length} broken of ${checked} internal links`);
  process.exit(1);
}
console.log(`${checked} internal links OK`);
