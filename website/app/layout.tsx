import { Inter, JetBrains_Mono } from 'next/font/google';
import type { Metadata } from 'next';
import { Provider } from '@/components/provider';
import { siteDescription, siteUrl } from '@/lib/seo';
import './global.css';

const inter = Inter({ subsets: ['latin'], variable: '--font-inter' });
const mono = JetBrains_Mono({ subsets: ['latin'], variable: '--font-jetbrains' });

export const metadata: Metadata = {
  title: {
    template: '%s · Repo2RLEnv',
    default: 'Repo2RLEnv: verifiable RL environments from any repository',
  },
  description: siteDescription,
  applicationName: 'Repo2RLEnv',
  metadataBase: new URL(`${siteUrl}/`),
  keywords: [
    'RL environments',
    'reinforcement learning',
    'coding agents',
    'SWE-bench',
    'Harbor',
    'verifiable rewards',
    'Hugging Face',
    'datasets',
  ],
  openGraph: { type: 'website', siteName: 'Repo2RLEnv', locale: 'en_US' },
  twitter: { card: 'summary_large_image' },
  robots: { index: true, follow: true },
};

export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${mono.variable}`} suppressHydrationWarning>
      <body className="flex min-h-screen flex-col font-sans">
        <Provider>{children}</Provider>
      </body>
    </html>
  );
}
