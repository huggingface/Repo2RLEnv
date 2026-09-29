import path from 'node:path';
import { createMDX } from 'fumadocs-mdx/next';

const withMDX = createMDX();

// The repository root: content is read from ../docs.
const repoRoot = path.resolve(import.meta.dirname, '..');
const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? '';

/** @type {import('next').NextConfig} */
const config = {
  output: 'export',
  // /quickstart/ → quickstart/index.html, matching the MkDocs URLs.
  trailingSlash: true,
  basePath,
  reactStrictMode: true,
  images: { unoptimized: true },
  outputFileTracingRoot: repoRoot,
  turbopack: { root: repoRoot },
};

export default withMDX(config);
