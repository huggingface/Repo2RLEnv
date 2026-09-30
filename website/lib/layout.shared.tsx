import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';
import { appName, githubUrl } from './shared';
import { GitHubStars } from '@/components/github-stars';

export function baseOptions(): BaseLayoutProps {
  return {
    nav: {
      title: <span className="font-semibold tracking-tight">{appName}</span>,
    },
    links: [
      { text: 'Docs', url: '/introduction/', active: 'nested-url' },
      { text: 'Tutorials', url: '/tutorials/', active: 'nested-url' },
      { text: 'Pipelines', url: '/pipelines/', active: 'nested-url' },
      { text: 'Reference', url: '/reference/cli/', active: 'nested-url' },
      {
        type: 'icon',
        text: 'Star on GitHub',
        url: githubUrl,
        label: 'Star Repo2RLEnv on GitHub',
        icon: <GitHubStars />,
        external: true,
      },
    ],
  };
}
