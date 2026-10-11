'use client';

import { useEffect, useState } from 'react';
import { Star } from 'lucide-react';
import { gitConfig } from '@/lib/shared';

const cacheKey = 'repo2rlenv:github-stars';
const maxAge = 60 * 60 * 1000;
type Count = { count: number; checkedAt: number };
let request: Promise<Count | null> | undefined;

function valid(value: unknown): value is Count {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Count;
  return (
    Number.isSafeInteger(candidate.count) &&
    candidate.count >= 0 &&
    Number.isFinite(candidate.checkedAt) &&
    candidate.checkedAt > 0
  );
}

// One public, unauthenticated request per page, shared by desktop/mobile chrome.
function refresh(): Promise<Count | null> {
  return (request ??= fetch(`https://api.github.com/repos/${gitConfig.user}/${gitConfig.repo}`, {
    signal: AbortSignal.timeout(5000),
    headers: { Accept: 'application/vnd.github+json' },
  })
    .then(async (response) => {
      if (!response.ok) return null;
      const body = await response.json();
      const result = { count: body.stargazers_count, checkedAt: Date.now() };
      if (!valid(result)) return null;
      try {
        localStorage.setItem(cacheKey, JSON.stringify(result));
      } catch {
        /* Storage is optional. */
      }
      return result;
    })
    .catch(() => null)
    .finally(() => {
      request = undefined;
    }));
}

export function GitHubStars() {
  const [result, setResult] = useState<Count | null>(null);
  const [display, setDisplay] = useState<number | null>(null);

  useEffect(() => {
    let active = true;
    let cached: Count | null = null;
    try {
      const value: unknown = JSON.parse(localStorage.getItem(cacheKey) ?? 'null');
      if (valid(value)) cached = value;
    } catch {
      /* Disabled storage or an old cache must not hide the link. */
    }
    if (cached) setResult(cached);
    const age = cached ? Date.now() - cached.checkedAt : Infinity;
    if (age < 0 || age >= maxAge) {
      void refresh().then((value) => {
        if (active && value) setResult(value);
      });
    }
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!result) return;
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
    let frame = 0;
    const finish = () => {
      cancelAnimationFrame(frame);
      setDisplay(result.count);
    };
    if (motion.matches) {
      finish();
      return;
    }
    const start = performance.now();
    const animate = (now: number) => {
      const progress = Math.min((now - start) / 750, 1);
      setDisplay(Math.round(result.count * (1 - (1 - progress) ** 3)));
      if (progress < 1) frame = requestAnimationFrame(animate);
    };
    frame = requestAnimationFrame(animate);
    motion.addEventListener('change', finish);
    return () => {
      cancelAnimationFrame(frame);
      motion.removeEventListener('change', finish);
    };
  }, [result]);

  const title = result
    ? `${result.count.toLocaleString('en-US')} GitHub stars; last checked ${new Date(result.checkedAt).toLocaleString()}`
    : 'Star Repo2RLEnv on GitHub';
  return (
    <span className="github-stars" title={title}>
      <svg aria-hidden="true" viewBox="0 0 24 24" fill="currentColor">
        <path d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12" />
      </svg>
      <span>Star</span>
      <Star aria-hidden="true" className="github-stars__star" />
      <span
        className="github-stars__count"
        aria-label={result ? `${result.count} stars` : 'Star count unavailable'}
      >
        {display === null ? '—' : display.toLocaleString('en-US')}
      </span>
    </span>
  );
}
