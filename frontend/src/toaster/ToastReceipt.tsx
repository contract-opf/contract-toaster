/**
 * ToastReceipt — the thermal-style provenance slip that spools out of the toaster
 * when a review finishes (issue #498).
 *
 * All three renderings — the slip on screen, "Copy as text", and "Save
 * receipt" — consume `receiptLines()` from `./receipt`. That is the whole
 * design: a provenance slip that said different things depending on how it
 * was exported would be worse than none, because someone will paste one into
 * a deal thread and it will be taken as a record.
 *
 * The image export draws those same lines onto a canvas. It is a real PNG
 * (the ticket's format — SVG does not paste inline into most deal threads),
 * and the content identity with the text copy holds by construction rather
 * than by a pixel comparison, which is what makes it assertable in an offline
 * jsdom gate at all.
 */
import { useCallback, useRef, useState } from 'react';

import { CtButton } from '../ui/react';
import {
  receiptFilename,
  receiptLines,
  receiptText,
  type ReceiptLine,
  type ReceiptSource,
} from './receipt';

export interface ReceiptProps {
  review: ReceiptSource;
  playbookName?: string | null;
}

// Image geometry, in device pixels at 2x so the slip stays legible when it is
// dropped into a thread and scaled down.
const SCALE = 2;
const IMAGE_WIDTH = 420;
const LINE_HEIGHT = 22;
const PADDING = 24;
const FONT = '13px ui-monospace, SFMono-Regular, Menlo, monospace';

/** The image's pixel height, at 1x. A `wrap` line (issue #570 follow-up) can
 *  reflow into more than one physical text row, so the height is sized off
 *  the actual rendered row count — the same `receiptText(lines, 40)` split
 *  the draw loop below iterates — never off `lines.length`, which
 *  undercounts whenever a wrapped row is present and truncates it against
 *  the bottom of the canvas. */
function imageHeight(lines: ReceiptLine[]): number {
  const rowCount = receiptText(lines, 40).split('\n').length;
  return PADDING * 2 + rowCount * LINE_HEIGHT;
}

/** Draw the receipt onto a 2D context. Exported so the export path and its
 *  test drive the same code — the test captures what was drawn. */
export function drawReceipt(ctx: CanvasRenderingContext2D, lines: ReceiptLine[]): void {
  const height = imageHeight(lines);
  ctx.fillStyle = '#fdfbf5';
  ctx.fillRect(0, 0, IMAGE_WIDTH, height);
  ctx.scale(SCALE, SCALE);
  ctx.font = FONT;
  ctx.textBaseline = 'top';
  ctx.fillStyle = '#2a2119';
  receiptText(lines, 40)
    .split('\n')
    .forEach((text, index) => {
      ctx.fillText(text, PADDING / SCALE, PADDING / SCALE + index * (LINE_HEIGHT / SCALE));
    });
}

function triggerDownload(href: string, filename: string): void {
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

export function ToastReceipt({ review, playbookName }: ReceiptProps): React.ReactElement | null {
  const [copied, setCopied] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  const lines = receiptLines(review, playbookName);

  const handleCopy = useCallback(() => {
    const text = receiptText(lines);
    // navigator.clipboard is absent on http origins and in some embedded
    // browsers. Failing loudly beats a button that silently does nothing.
    void Promise.resolve(navigator.clipboard?.writeText(text))
      .then(() => setCopied(true))
      .catch(() => setSaveError('Could not copy the receipt. Select the text above instead.'));
  }, [lines]);

  const handleSave = useCallback(() => {
    setSaveError(null);
    const canvas = canvasRef.current ?? document.createElement('canvas');
    canvas.width = IMAGE_WIDTH * SCALE;
    canvas.height = imageHeight(lines) * SCALE;
    const ctx = canvas.getContext('2d');
    if (!ctx) {
      setSaveError('This browser could not render the receipt image. Use “Copy as text”.');
      return;
    }
    drawReceipt(ctx, lines);
    triggerDownload(canvas.toDataURL('image/png'), receiptFilename(review.review_id, 'png'));
  }, [lines, review.review_id]);

  return (
    <div className="toaster-receipt" data-testid="review-receipt">
      {/* The slip. `role="group"` with a name rather than a table: the lines
          are a printed record, not tabular data to be navigated cell by cell,
          and each row reads as "label, value" in one utterance. */}
      <div
        className="toaster-receipt__paper"
        role="group"
        aria-label="Receipt for this review"
        data-testid="review-receipt-paper"
      >
        {lines.map((line) =>
          line.rule ? (
            <hr key={line.id} className="toaster-receipt__rule" aria-hidden="true" />
          ) : (
            <p
              key={line.id}
              className={
                line.wrap
                  ? 'toaster-receipt__line toaster-receipt__line--wrap'
                  : 'toaster-receipt__line'
              }
              data-receipt-line={line.id}
            >
              <span className="toaster-receipt__label">{line.label}</span>
              {line.value ? (
                <span
                  className={
                    line.wrap
                      ? 'toaster-receipt__value toaster-receipt__value--wrap'
                      : 'toaster-receipt__value'
                  }
                >
                  {line.value}
                </span>
              ) : null}
            </p>
          ),
        )}
      </div>

      <div className="ct-actions toaster-receipt__actions">
        <CtButton type="button" variant="secondary" data-testid="review-receipt-copy" onClick={handleCopy}>
          {copied ? 'Copied' : 'Copy as text'}
        </CtButton>
        <CtButton type="button" variant="secondary" data-testid="review-receipt-save" onClick={handleSave}>
          Save receipt
        </CtButton>
      </div>

      {saveError && (
        <p className="ct-muted" role="status" data-testid="review-receipt-error">
          {saveError}
        </p>
      )}

      <canvas ref={canvasRef} hidden aria-hidden="true" data-testid="review-receipt-canvas" />
    </div>
  );
}
