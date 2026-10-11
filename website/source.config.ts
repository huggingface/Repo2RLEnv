import { defineConfig, defineDocs } from 'fumadocs-mdx/config';
import { metaSchema, pageSchema } from 'fumadocs-core/source/schema';
import { remarkMdxFiles, remarkMdxMermaid } from 'fumadocs-core/mdx-plugins';
import { remarkAlerts } from './lib/remark-alerts';
import rehypeRaw from 'rehype-raw';
import { z } from 'zod';

// Content lives in the repository's top-level docs/ so GitHub paths such as
// docs/pipelines/pr_runtime.md stay stable (published dataset cards link there).
export const docs = defineDocs({
  dir: '../docs',
  docs: {
    files: ['**/*.{md,mdx}', '!_tools/**'],
    schema: pageSchema.extend({
      // A live explainer film from hf-motion, rendered above the page body.
      film: z.string().optional(),
      // A shorter label for the sidebar when the title is a full sentence.
      navTitle: z.string().optional(),
      resultsVisual: z.enum(['economics', 'native']).optional(),
      tutorial: z
        .object({
          author: z.object({ name: z.string().min(1), url: z.string().url() }),
          published: z.string().date(),
          updated: z.string().date().optional(),
          testedVersion: z.string().min(1),
          thumbnail: z
            .object({
              src: z.string().regex(/^\/images\/tutorials\/[a-z0-9-]+\.png$/),
              alt: z.string().min(1),
              width: z.number().int().positive(),
              height: z.number().int().positive(),
            })
            .optional(),
        })
        .optional(),
    }),
    postprocess: {
      includeProcessedMarkdown: true,
    },
  },
  meta: {
    schema: metaSchema,
  },
});

// MDX node types rehype-raw must leave alone (they come from remark plugins
// such as the Mermaid transform, even in plain .md files).
const mdxNodes = [
  'mdxjsEsm',
  'mdxFlowExpression',
  'mdxJsxFlowElement',
  'mdxJsxTextElement',
  'mdxTextExpression',
];

export default defineConfig({
  mdxOptions: {
    // Markdown-native syntax so pages also read well on GitHub:
    // `> [!NOTE]` callouts, ```files trees and ```mermaid diagrams.
    remarkPlugins: [remarkAlerts, remarkMdxFiles, remarkMdxMermaid],
    // Legacy pages and the generated prompt reference use raw <details> blocks.
    rehypePlugins: (v) => [[rehypeRaw, { passThrough: mdxNodes }], ...v],
  },
});
