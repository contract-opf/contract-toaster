/**
 * gallery/main.ts — the dev-only CTDS component gallery entry point (issue
 * #395, docs/frontend-design-system.md §9).
 *
 * Plain Lit/DOM, deliberately NO React: every ct-* element in this file is
 * built with `document.createElement` and wired directly (see
 * sections.ts's module docstring), which doubles as a no-React smoke test
 * of the component library — if any element depended on `ui/react.ts`'s
 * wrapping to function, it would render broken here.
 *
 * Import order mirrors main.tsx's rule (fonts → tokens.css → base.css),
 * MINUS app.css (the gallery proves components stand alone on tokens +
 * base only) and minus the Amplify UI stylesheet (there is no auth flow
 * here). `ui/index.ts` registers every ct-* custom element as a side
 * effect — see that module's own docstring.
 */
import '@fontsource/space-grotesk/latin-500.css';
import '@fontsource/space-grotesk/latin-700.css';
import '@fontsource/instrument-sans/latin-400.css';
import '@fontsource/instrument-sans/latin-500.css';
import '@fontsource/instrument-sans/latin-700.css';
import '@fontsource/ibm-plex-mono/latin-400.css';
import '../styles/tokens.css';
import '../styles/base.css';
import '../ui/index.ts';
import './gallery.css';

import { el, createEl } from './dom';
import type { CtButton } from '../ui/components/ct-button';
import {
  NAV_ITEMS,
  buildChipSection,
  buildButtonSection,
  buildIconButtonSection,
  buildCardSection,
  buildBannerSection,
  buildTabBarSection,
  buildAppShellSection,
  buildFieldSection,
  buildTableSection,
  buildToolbarSection,
  buildFileDropSection,
  buildProgressSection,
  buildTokensSection,
} from './sections';

type Theme = 'light' | 'dark';

// ---------------------------------------------------------------------------
// Theme toggle — flips `data-theme` on :root between 'light'/'dark'
// (tokens.css's override attribute, extended in this same issue so the
// override works in both directions — see tokens.css's
// `:root[data-theme='dark']` block). Starts from the OS preference so the
// gallery opens matching the viewer's system theme, same as the app.
// ---------------------------------------------------------------------------
function initialTheme(): Theme {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function buildThemeToggle(): HTMLElement {
  let theme: Theme = initialTheme();
  document.documentElement.setAttribute('data-theme', theme);

  const button = createEl<CtButton>('ct-button');
  button.setAttribute('variant', 'secondary');
  button.setAttribute('size', 'sm');
  const label = (t: Theme): string => (t === 'dark' ? 'Switch to day' : 'Switch to night');
  button.text = label(theme);

  button.addEventListener('click', () => {
    theme = theme === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', theme);
    button.text = label(theme);
  });

  const wrap = el('div', { className: 'gallery-theme-toggle' });
  wrap.append(button);
  return wrap;
}

// ---------------------------------------------------------------------------
// Page shell
// ---------------------------------------------------------------------------
function buildNav(): HTMLElement {
  const list = el(
    'ul',
    { className: 'gallery-nav__list' },
    NAV_ITEMS.map((item) => {
      const link = el('a', { text: item.label });
      link.setAttribute('href', `#${item.id}`);
      return el('li', {}, [link]);
    }),
  );

  return el('nav', { className: 'gallery-nav' }, [
    el('p', { className: 'gallery-nav__brand', text: 'CTDS Gallery' }),
    el('p', { className: 'gallery-nav__subtitle', text: 'Contract Toaster Design System' }),
    buildThemeToggle(),
    list,
  ]);
}

function buildMain(): HTMLElement {
  const intro = el('div', { className: 'gallery-intro' }, [
    el('h1', { text: 'Component gallery' }),
    el(
      'p',
      {
        text:
          'Every ct-* component in every variant and state, in both themes. Dev-only ' +
          '(excluded from production builds — docs/frontend-design-system.md §9). ' +
          'See frontend/src/ui/README.md for the contributor guide.',
      },
    ),
  ]);

  const main = el('main', { className: 'gallery-main' }, [
    intro,
    buildChipSection(),
    buildButtonSection(),
    buildIconButtonSection(),
    buildCardSection(),
    buildBannerSection(),
    buildTabBarSection(),
    buildAppShellSection(),
    buildFieldSection(),
    buildTableSection(),
    buildToolbarSection(),
    buildFileDropSection(),
    buildProgressSection(),
    buildTokensSection(),
  ]);

  return main;
}

function mount(): void {
  const root = document.getElementById('gallery-root');
  if (!root) {
    throw new Error('gallery.html is missing #gallery-root');
  }
  root.append(el('div', { className: 'gallery-shell' }, [buildNav(), buildMain()]));
}

mount();
