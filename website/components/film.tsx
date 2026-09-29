'use client';

import { useEffect, useRef, useState, type ComponentType } from 'react';
import { useTheme } from 'next-themes';
import type { PlayerRef } from '@remotion/player';
import type { Film as FilmData } from '@/vendor/films/films.mjs';
import { basePath } from '@/lib/shared';

type Films = typeof import('@/vendor/films/films.mjs');
type PlayerModule = typeof import('@remotion/player');

// The films and the player load once, on first use, never during SSR.
let loading: Promise<[Films, PlayerModule]> | null = null;
function load() {
  if (!loading) {
    // staticFile() in the films (the score) resolves under /films.
    (window as { remotion_staticBase?: string }).remotion_staticBase = `${basePath}/films`;
    loading = Promise.all([import('@/vendor/films/films.mjs'), import('@remotion/player')]);
  }
  return loading;
}

// The palette is global; `scheme` rides along in inputProps so the player
// re-renders the current frame when the site theme flips.
function Themed({ component: Component }: { component: ComponentType; scheme: string }) {
  return <Component />;
}

export function Film({ id, caption, poster }: { id: string; caption?: string; poster?: number }) {
  const { resolvedTheme } = useTheme();
  const scheme = resolvedTheme === 'dark' ? 'dark' : 'light';
  const [mods, setMods] = useState<[Films, PlayerModule] | null>(null);
  const frame = useRef<HTMLDivElement>(null);
  const player = useRef<PlayerRef>(null);

  useEffect(() => {
    let alive = true;
    void load().then((m) => alive && setMods(m));
    return () => {
      alive = false;
    };
  }, []);

  const film: FilmData | undefined = mods?.[0].FILMS[id];
  if (mods && film) mods[0].setScheme(scheme);

  // Autoplay (muted) as soon as the page is open and the film is on screen:
  // from the beginning the first time, pausing whenever the film scrolls away
  // or the browser tab is hidden, and resuming when both are true again.
  useEffect(() => {
    const el = frame.current;
    if (!el || !film) return;
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
    let inView = false;
    let started = false;

    const sync = () => {
      const p = player.current;
      if (!p) return;
      if (!inView || document.visibilityState !== 'visible') {
        p.pause();
        return;
      }
      if (reduced.matches || p.isPlaying()) return;
      if (!started) {
        started = true;
        p.seekTo(0);
      }
      p.play();
    };

    const io = new IntersectionObserver(
      ([entry]) => {
        inView = entry.isIntersecting;
        sync();
      },
      { threshold: 0.35 },
    );
    io.observe(el);
    document.addEventListener('visibilitychange', sync);
    return () => {
      io.disconnect();
      document.removeEventListener('visibilitychange', sync);
    };
  }, [film]);

  const Player = mods?.[1].Player;
  // 18% in — the same frame render-docs.sh uses for the poster.
  const initialFrame = film
    ? Math.min(poster ?? Math.floor((film.durationInFrames * 18) / 100), film.durationInFrames - 1)
    : 0;

  return (
    <figure className="r2r-film not-prose my-6">
      <div ref={frame} className="r2r-film__frame aspect-[3/2]">
        {Player && film ? (
          <Player
            ref={player}
            component={Themed}
            inputProps={{ component: film.component, scheme }}
            durationInFrames={film.durationInFrames}
            fps={film.fps}
            compositionWidth={film.width}
            compositionHeight={film.height}
            initialFrame={initialFrame}
            controls
            loop
            initiallyMuted
            clickToPlay
            doubleClickToFullscreen
            allowFullscreen
            acknowledgeRemotionLicense
            style={{ width: '100%', height: '100%' }}
          />
        ) : mods && !film ? (
          <div className="flex size-full items-center justify-center font-mono text-xs text-fd-muted-foreground">
            Unknown film “{id}”
          </div>
        ) : (
          // The frame the player opens on, per theme, until it loads (and
          // without JavaScript). Posters come from hf-motion's render-docs.sh.
          <>
            <img
              src={`${basePath}/films/posters/${id}-light.jpg`}
              alt=""
              className="size-full object-cover dark:hidden"
            />
            <img
              src={`${basePath}/films/posters/${id}-dark.jpg`}
              alt=""
              className="hidden size-full object-cover dark:block"
            />
          </>
        )}
      </div>
      {caption || film ? (
        <figcaption className="r2r-film__caption">{caption ?? film?.title}</figcaption>
      ) : null}
    </figure>
  );
}
