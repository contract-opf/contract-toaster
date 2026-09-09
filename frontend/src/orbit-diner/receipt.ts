/**
 * The console's two receipt actions. It owns neither the words nor the pixels
 * (issue #721).
 *
 * The lines arrive already rendered — `receiptText(receiptLines(detail))
 * .split('\n')`, composed once in `ReviewSubmission` — so this module never
 * decides what the receipt says, and the image comes from the app's one canvas
 * exporter rather than a second one the kit shipped. The kit's own 840 px /
 * 28 px IBM Plex Mono drawing is gone: two exporters meant two ideas of what
 * the saved PNG contains, which is exactly the drift `toaster/receipt.ts`
 * exists to prevent.
 */
import { saveReceiptImage } from '../toaster/receiptImage';

/** Copy the rendered receipt. Rejects when the clipboard is unavailable (a
 *  non-secure origin) so the caller can leave the confirmation off rather than
 *  claim a copy that never happened. */
export async function copyReceipt(lines: readonly string[]): Promise<void> {
  await navigator.clipboard.writeText(lines.join('\n'));
}

/**
 * Save the rendered receipt as a PNG under `filename` — always
 * `receiptFilename()`'s `toast-receipt-<shortid>.png`. The caller passes it,
 * because the rows carry no review id and this module must not invent a name
 * the rest of the app does not use.
 *
 * Returns `false` when the browser gave no 2D context, so the caller does not
 * confirm a save that did not happen.
 */
export function saveReceipt(lines: readonly string[], filename: string): boolean {
  return saveReceiptImage(lines, filename);
}
