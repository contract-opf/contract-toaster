/**
 * linkify.tsx — turn http(s) URLs inside admin-authored free text into
 * clickable links, and nothing else (issue #476).
 *
 * `AdminPlaybooks.tsx` renders `notes` — free text an admin types into a
 * textarea (see `playbook_versions.py`'s "the one mutable field") — as
 * plain text today, including the shipped sample's
 * `https://github.com/contract-opf/playbooks`, which the sample's own note
 * tells the reader to visit. `notes` is admin-authored, not attacker
 * input from an unauthenticated surface, but it is still rendered inside
 * this SPA, so this helper never introduces HTML injection: every
 * character of the original text — URL included — stays a plain React
 * text node, and only an `<a>` element wraps it. Nothing here parses the
 * text as markup or hands raw HTML to the DOM.
 *
 * Only `http:`/`https:` are ever linkified. The match itself is anchored on
 * the literal `http://` / `https://` prefix, so a `javascript:` (or any
 * other) URI never enters the "is this a link" branch in the first place —
 * it is not a matter of rejecting a scheme after matching, there is no
 * regex path that reaches one. `new URL(...).protocol` is still checked
 * before rendering an anchor, as defense in depth against a future, looser
 * pattern change.
 */
import type { ReactNode } from 'react';

const URL_PATTERN = /https?:\/\/[^\s<>"']+/g;

const SAFE_PROTOCOLS = new Set(['http:', 'https:']);

// A URL sitting at the end of a sentence commonly picks up trailing
// punctuation that was never part of it ("...playbooks.", "(see url)") —
// stripped off the link (and put back into the surrounding text) rather
// than baked into the href.
const TRAILING_PUNCTUATION = /[).,;:!?'"]+$/;

function isSafeUrl(candidate: string): boolean {
  try {
    return SAFE_PROTOCOLS.has(new URL(candidate).protocol);
  } catch {
    return false;
  }
}

/**
 * Render `text` as a mix of plain strings and `<a>` elements: every
 * `http(s)://…` run becomes a link that opens in a new tab
 * (`target="_blank" rel="noopener noreferrer"`); everything else —
 * including any non-http(s) URI such as `javascript:...` — stays exactly
 * the plain text it was.
 */
export function linkifyText(text: string): ReactNode {
  if (text === '') {
    return text;
  }

  const nodes: ReactNode[] = [];
  let lastIndex = 0;
  let key = 0;
  let match: RegExpExecArray | null;

  URL_PATTERN.lastIndex = 0;
  while ((match = URL_PATTERN.exec(text)) !== null) {
    let url = match[0];
    let trailing = '';
    const punctuation = url.match(TRAILING_PUNCTUATION);
    if (punctuation) {
      trailing = punctuation[0];
      url = url.slice(0, url.length - trailing.length);
    }
    if (url === '') {
      continue;
    }

    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }

    if (isSafeUrl(url)) {
      nodes.push(
        <a key={`linkify-${key++}`} href={url} target="_blank" rel="noopener noreferrer">
          {url}
        </a>,
      );
    } else {
      nodes.push(url);
    }
    if (trailing) {
      nodes.push(trailing);
    }

    lastIndex = match.index + match[0].length;
  }

  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }

  return nodes;
}
