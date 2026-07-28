/**
 * ui-file-drop.test.tsx — CtFileDrop (issue #393, docs/frontend-design-
 * system.md §6/§7).
 *
 * Renders the React wrapper (`ui/react.ts`'s `CtFileDrop`) exactly as the
 * app consumes it (§10). CtFileDrop is light DOM and builds its own
 * input/label/pill once in `connectedCallback` via hand-rolled accessors
 * (see ct-file-drop.ts's docstring) — the same architecture as CtField, so
 * `getByLabelText`/`getByTestId` resolve synchronously with no
 * `updateComplete` wait needed, exactly like ui-field.test.tsx.
 */
import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { CtFileDrop } from '../ui/react';

const HOSTILE_FILENAME = '<img src=x onerror=alert(1)>.docx';

function docxFile(name = 'contract.docx'): File {
  return new File(['contents'], name, {
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  });
}

describe('CtFileDrop', () => {
  it('forwards data-testid to the real <input type=file>, labeled for getByLabelText', () => {
    render(
      <CtFileDrop
        data-testid="review-file-input"
        label="Drop your contract here or browse"
        accept=".docx"
      />,
    );
    const input = screen.getByTestId('review-file-input');
    expect(input.tagName).toBe('INPUT');
    expect(input).toHaveAttribute('type', 'file');
    expect(screen.getByLabelText('Drop your contract here or browse')).toBe(input);
  });

  it('forwards accept to the real input', () => {
    render(<CtFileDrop data-testid="fd" accept=".docx,.pdf" label="Upload" />);
    expect(screen.getByTestId('fd')).toHaveAttribute('accept', '.docx,.pdf');
  });

  it('emits ct-files with the selected file(s) on input change', () => {
    const onFiles = vi.fn();
    render(
      <CtFileDrop data-testid="fd" label="Upload" onFiles={onFiles} />,
    );
    const file = docxFile();
    fireEvent.change(screen.getByTestId('fd'), { target: { files: [file] } });

    expect(onFiles).toHaveBeenCalledTimes(1);
    const event = onFiles.mock.calls[0]![0] as CustomEvent<{ files: File[] }>;
    expect(event.detail.files).toEqual([file]);
  });

  it('emits ct-files on drop', () => {
    const onFiles = vi.fn();
    render(<CtFileDrop data-testid="fd" label="Upload" onFiles={onFiles} />);
    const well = document.querySelector('.ct-file-drop__well') as HTMLElement;
    const file = docxFile('dropped.docx');

    fireEvent.drop(well, { dataTransfer: { files: [file] } });

    expect(onFiles).toHaveBeenCalledTimes(1);
    const event = onFiles.mock.calls[0]![0] as CustomEvent<{ files: File[] }>;
    expect(event.detail.files).toEqual([file]);
  });

  it('renders the selected-file pill with filename (as text) and human size', () => {
    render(<CtFileDrop data-testid="fd" label="Upload" />);
    const file = new File(['a'.repeat(2048)], 'contract.docx', {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    });
    fireEvent.change(screen.getByTestId('fd'), { target: { files: [file] } });

    const pill = document.querySelector('.ct-file-drop__pill') as HTMLElement;
    expect(pill.hidden).toBe(false);
    expect(pill.textContent).toContain('contract.docx');
    expect(pill.textContent).toContain('KB');
  });

  it('renders a hostile filename as inert text, never as HTML', () => {
    render(<CtFileDrop data-testid="fd" label="Upload" />);
    const file = docxFile(HOSTILE_FILENAME);
    fireEvent.change(screen.getByTestId('fd'), { target: { files: [file] } });

    const pillName = document.querySelector('.ct-file-drop__pill-name') as HTMLElement;
    expect(pillName.textContent).toBe(HOSTILE_FILENAME);
    // Never parsed as markup — no <img> element materializes from it.
    expect(pillName.querySelector('img')).toBeNull();
  });

  it('clear (×) resets the selection and emits ct-files with an empty list', () => {
    const onFiles = vi.fn();
    render(<CtFileDrop data-testid="fd" label="Upload" onFiles={onFiles} />);
    const input = screen.getByTestId('fd') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [docxFile()] } });

    const pill = document.querySelector('.ct-file-drop__pill') as HTMLElement;
    expect(pill.hidden).toBe(false);

    const clearBtn = screen.getByRole('button', { name: 'Remove selected file' });
    fireEvent.click(clearBtn);

    expect(pill.hidden).toBe(true);
    expect(input.value).toBe('');
    const lastCall = onFiles.mock.calls[onFiles.mock.calls.length - 1]![0] as CustomEvent<{
      files: File[];
    }>;
    expect(lastCall.detail.files).toEqual([]);
  });

  it('re-importing the ui/react module does not throw (defineOnce guards registration)', async () => {
    await expect(import('../ui/react')).resolves.toBeDefined();
    await expect(import('../ui/components/ct-file-drop')).resolves.toBeDefined();
    await expect(import('../ui/components/ct-progress')).resolves.toBeDefined();
  });
});
