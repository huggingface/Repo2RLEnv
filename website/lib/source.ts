import { docs } from 'collections/server';
import { llms, loader, type LoaderPlugin } from 'fumadocs-core/source';
import { docsRoute } from './shared';

// Sidebar entries use `navTitle` from front matter when a page sets one.
const navTitles: LoaderPlugin = {
  name: 'nav-title',
  transformPageTree: {
    file(node, filePath) {
      if (!filePath) return node;
      const file = this.storage.read(filePath);
      const navTitle =
        file?.format === 'page' ? (file.data as { navTitle?: string }).navTitle : undefined;
      return navTitle ? { ...node, name: navTitle } : node;
    },
  },
};

// See https://fumadocs.dev/docs/headless/source-api
export const source = loader({
  baseUrl: docsRoute,
  source: docs.toFumadocsSource(),
  plugins: [navTitles],
});

export const docsLlms = llms(source, {
  renderPage: async (page) => `# ${page.data.title} (${page.url})

${await page.data.getText('processed')}`,
});
