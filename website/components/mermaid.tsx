'use client';

import { use, useEffect, useId, useState } from 'react';
import { useTheme } from 'next-themes';

// Neutral palette mirrored from app/global.css; emerald marks edges' labels
// sparingly. Flat nodes, hairline borders, no shadows.
const palette = {
  light: {
    background: 'transparent',
    mainBkg: '#ffffff',
    primaryColor: '#ffffff',
    primaryBorderColor: '#d9d9d9',
    primaryTextColor: '#171717',
    secondaryColor: '#f7f7f7',
    secondaryBorderColor: '#d9d9d9',
    tertiaryColor: '#fafafa',
    tertiaryBorderColor: '#e5e5e5',
    clusterBkg: '#fafafa',
    clusterBorder: '#e5e5e5',
    lineColor: '#a3a3a3',
    textColor: '#404040',
    edgeLabelBackground: '#ffffff',
    noteBkgColor: '#f7f7f7',
    noteBorderColor: '#e5e5e5',
    noteTextColor: '#404040',
    actorBkg: '#ffffff',
    actorBorder: '#d9d9d9',
    actorTextColor: '#171717',
    actorLineColor: '#d4d4d4',
    signalColor: '#737373',
    signalTextColor: '#404040',
    labelBoxBkgColor: '#ffffff',
    labelBoxBorderColor: '#d9d9d9',
    labelTextColor: '#404040',
    loopTextColor: '#404040',
    activationBkgColor: '#f5f5f5',
    activationBorderColor: '#d4d4d4',
  },
  dark: {
    background: 'transparent',
    mainBkg: '#141414',
    primaryColor: '#141414',
    primaryBorderColor: '#333333',
    primaryTextColor: '#ededed',
    secondaryColor: '#1a1a1a',
    secondaryBorderColor: '#333333',
    tertiaryColor: '#111111',
    tertiaryBorderColor: '#2a2a2a',
    clusterBkg: '#111111',
    clusterBorder: '#2a2a2a',
    lineColor: '#5c5c5c',
    textColor: '#d4d4d4',
    edgeLabelBackground: '#0f0f0f',
    noteBkgColor: '#1a1a1a',
    noteBorderColor: '#2a2a2a',
    noteTextColor: '#d4d4d4',
    actorBkg: '#141414',
    actorBorder: '#333333',
    actorTextColor: '#ededed',
    actorLineColor: '#333333',
    signalColor: '#8a8a8a',
    signalTextColor: '#d4d4d4',
    labelBoxBkgColor: '#141414',
    labelBoxBorderColor: '#333333',
    labelTextColor: '#d4d4d4',
    loopTextColor: '#d4d4d4',
    activationBkgColor: '#1a1a1a',
    activationBorderColor: '#333333',
  },
};

// Flat, rounded nodes; thin edges. Mermaid's classic look adds drop shadows.
const themeCSS = `
  .node rect, .node polygon, .node circle, .node path, .actor, .note, .labelBox {
    filter: none !important; stroke-width: 1px !important;
  }
  .node rect, .note, .labelBox { rx: 8px; ry: 8px; }
  .cluster rect { rx: 10px; ry: 10px; stroke-dasharray: 3 3; }
  .flowchart-link, .edgePath .path, .messageLine0, .messageLine1 { stroke-width: 1.25px !important; }
  .edgeLabel, .edgeLabel p, .edgeLabel rect { font-size: 12px; }
  .nodeLabel, .label { line-height: 1.35; }
  marker path { stroke-width: 0 !important; }
`;

export function Mermaid({ chart }: { chart: string }) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  if (!mounted) return <div className="r2r-mermaid min-h-24" aria-hidden />;
  return <MermaidContent chart={chart} />;
}

const cache = new Map<string, Promise<unknown>>();

function cachePromise<T>(key: string, create: () => Promise<T>): Promise<T> {
  const cached = cache.get(key);
  if (cached) return cached as Promise<T>;
  const promise = create();
  cache.set(key, promise);
  return promise;
}

function MermaidContent({ chart }: { chart: string }) {
  const id = useId();
  const { resolvedTheme } = useTheme();
  const scheme = resolvedTheme === 'dark' ? 'dark' : 'light';
  const { default: mermaid } = use(cachePromise('mermaid', () => import('mermaid')));

  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'loose',
    theme: 'base',
    look: 'classic',
    fontFamily: 'Inter, ui-sans-serif, system-ui, sans-serif',
    themeVariables: { ...palette[scheme], fontSize: '13px' },
    themeCSS,
    flowchart: {
      curve: 'basis',
      padding: 12,
      nodeSpacing: 28,
      rankSpacing: 30,
      // Wider labels make nodes wider and chains shorter.
      wrappingWidth: 260,
      diagramPadding: 8,
      useMaxWidth: true,
    },
    sequence: { useMaxWidth: true, boxMargin: 8, messageMargin: 32, mirrorActors: false },
  });

  const { svg, bindFunctions } = use(
    cachePromise(`${chart}-${scheme}`, () =>
      mermaid.render(id.replaceAll(':', ''), chart.replaceAll('\\n', '\n')),
    ),
  );

  return (
    <figure className="r2r-mermaid not-prose">
      <div
        className="r2r-mermaid__canvas"
        ref={(container) => {
          if (container) bindFunctions?.(container);
        }}
        dangerouslySetInnerHTML={{ __html: svg }}
      />
    </figure>
  );
}
