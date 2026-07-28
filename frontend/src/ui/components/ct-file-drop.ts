/**
 * ct-file-drop — drag-and-drop + click-to-browse upload control (issue #393,
 * docs/frontend-design-system.md §6/§7). Replaces the naked
 * `<input type="file">` in ReviewSubmission.tsx with a sunken drop well
 * (`--ct-surface-sunken`, dashed `--ct-border-strong`) that visually rhymes
 * with the toaster slot.
 *
 * LIGHT DOM, mandatorily (§5.1): the accessible surface is a REAL
 * `<input type="file">` wired to a REAL `<label for>` — both are ID
 * references that must resolve in the same tree scope RTL's
 * `getByLabelText`/`getByTestId` (and screen readers) query, which a
 * shadow root would break.
 *
 * Same synchronous-hand-rolled-accessor architecture as ct-button.ts /
 * ct-icon-button.ts / ct-field.ts (see those docstrings for the
 * empirically-verified rationale): `accept`/`label` are built once into
 * real DOM in `connectedCallback` and mutated directly by their setters,
 * never through lit-html — this repo's tests call
 * `fireEvent.change(screen.getByTestId('review-file-input'), ...)`
 * immediately after `render()`, with no `await` for Lit's inherently-async
 * update cycle. `data-testid` is moved from the host onto the internal
 * `<input>` for the same reason ct-button/ct-icon-button do it: the
 * queried element must be the real, interactive, form-participating node.
 *
 * The input itself is visually hidden (clip-rect technique, never
 * `display:none`/`visibility:hidden`, which would drop it from the tab
 * order) but stays the only focus stop — the well/label are pointer-only
 * affordances, exactly the ticket's "the well itself is not a focus stop"
 * requirement. A visible `:focus-visible` ring on the input is mirrored
 * onto the label via a sibling selector (ct-file-drop.css) since the
 * input itself is clipped to nothing.
 *
 * Selected-file filenames are untrusted input (docs/threat-model.md) and
 * render via `textContent` only — never HTML — so a hostile filename
 * stays inert (§72/XSS posture, checked by
 * tests/test_frontend_xss_posture.py).
 */
import { LitElement } from 'lit';
import { defineOnce } from '../define';
import './ct-file-drop.css';
// Registers the ct-icon-button custom element (side effect) before this
// module's connectedCallback creates one via document.createElement, so the
// clear (×) control below is upgraded synchronously rather than racing the
// custom-element-upgrade lifecycle.
import './ct-icon-button';

const TAG = 'ct-file-drop';

let nextDropId = 0;

function humanFileSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unitIndex]}`;
}

export class CtFileDrop extends LitElement {
  createRenderRoot(): this {
    return this;
  }

  private readonly _id = `ct-file-drop-${++nextDropId}`;

  private _well: HTMLDivElement | null = null;
  private _input: HTMLInputElement | null = null;
  private _labelText: HTMLSpanElement | null = null;
  private _pill: HTMLDivElement | null = null;
  private _pillName: HTMLSpanElement | null = null;
  private _pillSize: HTMLSpanElement | null = null;

  private _label = 'Drop your contract here or browse';
  private _accept = '';
  private _dragDepth = 0;

  connectedCallback(): void {
    super.connectedCallback();
    if (this._well) {
      return;
    }

    const testId = this.getAttribute('data-testid');
    if (testId !== null) {
      this.removeAttribute('data-testid');
    }

    const well = document.createElement('div');
    well.className = 'ct-file-drop__well';

    const input = document.createElement('input');
    input.type = 'file';
    input.id = `${this._id}-input`;
    input.className = 'ct-file-drop__input';
    if (this._accept) {
      input.accept = this._accept;
    }
    if (testId !== null) {
      input.setAttribute('data-testid', testId);
    }
    input.addEventListener('change', () => {
      const files = input.files ? Array.from(input.files) : [];
      this._applySelection(files[0] ?? null);
      this._emitFiles(files);
    });

    const label = document.createElement('label');
    label.className = 'ct-file-drop__label';
    label.htmlFor = input.id;

    const SVG_NS = 'http://www.w3.org/2000/svg';
    const icon = document.createElementNS(SVG_NS, 'svg');
    icon.setAttribute('class', 'ct-file-drop__icon');
    icon.setAttribute('viewBox', '0 0 24 24');
    icon.setAttribute('aria-hidden', 'true');
    // Built as real SVG DOM nodes — never raw-HTML/React's unsafe-HTML prop
    // (§72 XSS posture) — even though this markup is a static,
    // developer-authored constant with nothing untrusted flowing through it.
    for (const d of [
      'M12 4v11m0-11 4 4m-4-4-4 4',
      'M5 16v2a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-2',
    ]) {
      const path = document.createElementNS(SVG_NS, 'path');
      path.setAttribute('d', d);
      path.setAttribute('fill', 'none');
      path.setAttribute('stroke', 'currentColor');
      path.setAttribute('stroke-width', '1.8');
      path.setAttribute('stroke-linecap', 'round');
      path.setAttribute('stroke-linejoin', 'round');
      icon.appendChild(path);
    }

    const text = document.createElement('span');
    text.className = 'ct-file-drop__text';
    text.textContent = this._label;
    this._labelText = text;

    label.append(icon, text);
    well.append(input, label);

    well.addEventListener('dragover', (event: DragEvent) => {
      event.preventDefault();
    });
    well.addEventListener('dragenter', (event: DragEvent) => {
      event.preventDefault();
      this._dragDepth += 1;
      well.classList.add('ct-file-drop--over');
    });
    well.addEventListener('dragleave', () => {
      this._dragDepth = Math.max(0, this._dragDepth - 1);
      if (this._dragDepth === 0) {
        well.classList.remove('ct-file-drop--over');
      }
    });
    well.addEventListener('drop', (event: DragEvent) => {
      event.preventDefault();
      this._dragDepth = 0;
      well.classList.remove('ct-file-drop--over');

      const dataTransfer = event.dataTransfer;
      const files = dataTransfer?.files ? Array.from(dataTransfer.files) : [];
      if (dataTransfer?.files) {
        // Assign to the input's own FileList when the runtime allows it (real
        // browsers support `input.files = dataTransfer.files`) so the input
        // stays the single source of truth for what's selected — this is
        // best-effort only: jsdom/older engines may reject the assignment,
        // and the ct-files event below carries the files regardless.
        try {
          input.files = dataTransfer.files;
        } catch {
          // Ignored — see comment above.
        }
      }
      this._applySelection(files[0] ?? null);
      this._emitFiles(files);
    });

    const pill = document.createElement('div');
    pill.className = 'ct-file-drop__pill';
    pill.hidden = true;

    const pillName = document.createElement('span');
    pillName.className = 'ct-file-drop__pill-name';
    const pillSize = document.createElement('span');
    pillSize.className = 'ct-file-drop__pill-size';

    // The clear control is a ct-icon-button primitive (not a hand-rolled
    // <button>) so it inherits the primitive's documented ≥44px hit target
    // (docs/frontend-design-system.md:270, ct-icon-button.css:17-18) and
    // hover/focus-ring styling instead of re-implementing it here.
    const clearBtn = document.createElement('ct-icon-button') as HTMLElement & {
      label: string;
      text: string;
    };
    clearBtn.className = 'ct-file-drop__clear';
    clearBtn.label = 'Remove selected file';
    clearBtn.text = '×';
    clearBtn.addEventListener('click', () => {
      input.value = '';
      this._applySelection(null);
      this._emitFiles([]);
    });

    pill.append(pillName, pillSize, clearBtn);

    this.append(well, pill);

    this._well = well;
    this._input = input;
    this._pill = pill;
    this._pillName = pillName;
    this._pillSize = pillSize;
  }

  /** Accept list forwarded verbatim to the internal `<input accept>`. */
  get accept(): string {
    return this._accept;
  }

  set accept(value: string) {
    this._accept = value ?? '';
    if (this._input) {
      if (this._accept) {
        this._input.setAttribute('accept', this._accept);
      } else {
        this._input.removeAttribute('accept');
      }
    }
  }

  /** Visible browse-affordance text, also the input's accessible name. */
  get label(): string {
    return this._label;
  }

  set label(value: string) {
    this._label = value || 'Drop your contract here or browse';
    if (this._labelText) {
      this._labelText.textContent = this._label;
    }
  }

  // Renders the selected-file pill (filename as a TEXT node — never HTML —
  // and human-readable size) or hides it when nothing is selected.
  private _applySelection(file: File | null): void {
    if (!this._pill || !this._pillName || !this._pillSize) {
      return;
    }
    if (!file) {
      this._pill.hidden = true;
      this._pillName.textContent = '';
      this._pillSize.textContent = '';
      return;
    }
    this._pillName.textContent = file.name;
    this._pillSize.textContent = humanFileSize(file.size);
    this._pill.hidden = false;
  }

  private _emitFiles(files: File[]): void {
    this.dispatchEvent(
      new CustomEvent<{ files: File[] }>('ct-files', {
        detail: { files },
        bubbles: true,
        composed: true,
      }),
    );
  }

  // No render() override — see ct-field.ts's module docstring: this
  // component builds its own DOM once and mutates it directly.
}

defineOnce(TAG, CtFileDrop);
