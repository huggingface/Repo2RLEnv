'use client';

import { Check, Copy } from 'lucide-react';
import { useState } from 'react';

export function InstallCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);

  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard.writeText(command);
        setCopied(true);
        setTimeout(() => setCopied(false), 1600);
      }}
      className="group inline-flex items-center gap-3 rounded-lg border bg-fd-card px-4 py-2.5 font-mono text-sm text-fd-foreground transition-colors hover:border-fd-foreground/20"
      aria-label={`Copy "${command}"`}
    >
      <span className="text-fd-muted-foreground select-none">$</span>
      <span>{command}</span>
      {copied ? (
        <Check className="size-4 text-brand" aria-hidden />
      ) : (
        <Copy className="size-4 text-fd-muted-foreground group-hover:text-fd-foreground" aria-hidden />
      )}
    </button>
  );
}
