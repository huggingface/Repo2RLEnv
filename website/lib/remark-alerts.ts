import type { Blockquote, Paragraph, Root, RootContent } from 'mdast';
import type { MdxJsxFlowElement } from 'mdast-util-mdx-jsx';

// GitHub alert syntax → Fumadocs <Callout>. The same file then reads well on
// GitHub and on the site:
//
//   > [!NOTE]
//   > Exports are generation results, not quality acceptance.
const types: Record<string, string> = {
  NOTE: 'info',
  TIP: 'success',
  IMPORTANT: 'info',
  WARNING: 'warn',
  CAUTION: 'error',
};

const marker = /^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*(?:\n|$)/;

function convert(node: Blockquote): MdxJsxFlowElement | null {
  const first = node.children[0] as Paragraph | undefined;
  const lead = first?.type === 'paragraph' ? first.children[0] : undefined;
  if (!lead || lead.type !== 'text') return null;
  const match = marker.exec(lead.value);
  if (!match) return null;

  lead.value = lead.value.slice(match[0].length);
  if (!lead.value) first!.children.shift();
  if (first!.children.length === 0) node.children.shift();

  return {
    type: 'mdxJsxFlowElement',
    name: 'Callout',
    attributes: [{ type: 'mdxJsxAttribute', name: 'type', value: types[match[1]] }],
    children: node.children,
  };
}

function walk(parent: { children: RootContent[] }) {
  parent.children = parent.children.map((child) => {
    if (child.type === 'blockquote') {
      const callout = convert(child);
      if (callout) {
        walk(callout as unknown as { children: RootContent[] });
        return callout as unknown as RootContent;
      }
    }
    if ('children' in child && Array.isArray(child.children)) {
      walk(child as unknown as { children: RootContent[] });
    }
    return child;
  });
}

export function remarkAlerts() {
  return (tree: Root) => walk(tree);
}
