import { createGetUrl } from 'fumadocs-core/source';

export const appName = 'Repo2RLEnv';

// GitHub Pages serves the site under /Repo2RLEnv; local dev runs at the root.
export const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? '';

// Docs are mounted at the site root so MkDocs-era URLs (/quickstart/,
// /pipelines/pr_runtime/) keep working.
export const docsRoute = '/';
export const docsImageRoute = '/og';
export const docsContentRoute = '/llms.mdx';

export const gitConfig = {
  user: 'huggingface',
  repo: 'Repo2RLEnv',
  branch: 'main',
};

export const githubUrl = `https://github.com/${gitConfig.user}/${gitConfig.repo}`;

export function githubEditUrl(path: string) {
  return `${githubUrl}/blob/${gitConfig.branch}/docs/${path}`;
}

const getContentUrl = createGetUrl(docsContentRoute);

export function getPageMarkdownUrl(page: { slugs: string[]; locale?: string }) {
  const segments = [...page.slugs, 'content.md'];

  return { segments, url: getContentUrl(segments, page.locale) };
}

const getImageUrl = createGetUrl(docsImageRoute);

export function getPageImageUrl(page: { slugs: string[]; locale?: string }) {
  const segments = [...page.slugs, 'image.png'];

  return { segments, url: getImageUrl(segments, page.locale) };
}
