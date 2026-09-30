import { source } from './source';

export function tutorialPages() {
  return source
    .getPages()
    .filter((page) => page.data.tutorial)
    .sort((a, b) => b.data.tutorial!.published.localeCompare(a.data.tutorial!.published));
}

export function readingMinutes(text: string) {
  return Math.max(1, Math.ceil(text.trim().split(/\s+/).length / 200));
}

export function dateLabel(date: string) {
  return new Intl.DateTimeFormat('en', {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(`${date}T00:00:00Z`));
}
