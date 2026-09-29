import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';
import { appName, githubUrl } from './shared';

export function baseOptions(): BaseLayoutProps {
  return {
    nav: {
      title: <span className="font-semibold tracking-tight">{appName}</span>,
    },
    links: [
      { text: 'Docs', url: '/introduction/', active: 'nested-url' },
      { text: 'Pipelines', url: '/pipelines/', active: 'nested-url' },
      { text: 'Reference', url: '/reference/cli/', active: 'nested-url' },
    ],
    githubUrl,
  };
}
