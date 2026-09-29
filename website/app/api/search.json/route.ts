import { source } from '@/lib/source';
import { createSearchAPI } from 'fumadocs-core/search/server';

export const revalidate = false;

// The generated prompt reference is several MB of verbatim templates; index
// those pages by title and description only so the static index stays small.
const generated = (path: string) => path.startsWith('pipelines/prompts/');

export const { staticGET: GET } = createSearchAPI('advanced', {
  language: 'english',
  indexes: source.getPages().map((page) => ({
    id: page.url,
    url: page.url,
    title: page.data.title,
    description: page.data.description,
    structuredData: generated(page.path)
      ? { headings: [], contents: [] }
      : page.data.structuredData,
  })),
});
