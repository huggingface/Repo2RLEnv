import { source } from '@/lib/source';
import { DocsLayout } from 'fumadocs-ui/layouts/docs';
import { baseOptions } from '@/lib/layout.shared';

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    // The sidebar carries every section, so the top-level links would only repeat it.
    <DocsLayout
      tree={source.getPageTree()}
      {...baseOptions()}
      links={baseOptions().links?.filter((link) => link.type === 'icon')}
    >
      {children}
    </DocsLayout>
  );
}
