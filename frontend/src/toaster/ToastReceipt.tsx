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
 * jsdom gate at all. The drawing itself lives in `./receiptImage` — the one
 * canvas exporter in the app (issue #721), shared with the Orbit Diner
 * console so the two homes cannot grow two ideas of what the PNG says.
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
import {
  RECEIPT_IMAGE_COLUMNS,
  drawReceiptRows,
  saveReceiptImage,
} from './receiptImage';

export interface ReceiptProps {
  review: ReceiptSource;
  playbookName?: string | null;
}

/** The rows both the image and the export path draw — the rendered receipt,
 *  split back into physical rows. A `wrap` line (issue #570 follow-up) can
 *  reflow into more than one row, so everything downstream counts ROWS and
 *  never `lines.length`, which undercounts and truncates a wrapped receipt
 *  against the bottom of the canvas. */
function receiptRows(lines: ReceiptLine[]): string[] {
  return receiptText(lines, RECEIPT_IMAGE_COLUMNS).split('\n');
}

/** Draw the receipt onto a 2D context, from the canonical lines. A thin shim
 *  over the shared exporter, kept so this component's own test drives the
 *  same code the "Save receipt" button does. */
export function drawReceipt(ctx: CanvasRenderingContext2D, lines: ReceiptLine[]): void {
  drawReceiptRows(ctx, receiptRows(lines));
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
    const saved = saveReceiptImage(
      receiptRows(lines),
      receiptFilename(review.review_id, 'png'),
      canvasRef.current ?? undefined,
    );
    if (!saved) {
      setSaveError('This browser could not render the receipt image. Use “Copy as text”.');
    }
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
