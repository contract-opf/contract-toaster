/**
 * ui/index.ts — side-effect element registrations (CTDS foundation, issue #385).
 *
 * Importing this module registers every ct-* custom element (each component
 * module calls `defineOnce` at module scope — see ./define.ts). Non-React
 * consumers (the dev component gallery, any future server-rendered page)
 * import this module directly for its side effects; React code instead
 * imports the typed wrappers from `./react.ts`, which pulls in the same
 * component modules and so registers them too.
 */
import './components/ct-chip';
import './components/ct-button';
import './components/ct-icon-button';
import './components/ct-card';
import './components/ct-banner';
import './components/ct-tab-bar';
import './components/ct-app-shell';
import './components/ct-field';
import './components/ct-table';
import './components/ct-toolbar';
import './components/ct-file-drop';
import './components/ct-progress';
