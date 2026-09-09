/**
 * receiptImage.ts — the ONE canvas exporter for the receipt (issue #721).
 *
 * There used to be two: this app's 420 px / 2x drawing inside
 * `ToastReceipt.tsx`, and the Orbit Diner kit's own 840 px / 28 px IBM Plex
 * Mono one in `orbit-diner/receipt.ts`. Two exporters mean two ideas of what
 * the saved image says, and the receipt's whole claim is that the slip on
 * screen, the clipboard payload and the PNG cannot disagree. So the drawing
 * lives here, once, and both homes call it.
 *
 * IT DRAWS ROWS, NOT LINES.
 *
 * The input is the already-rendered `string[]` — `receiptText(receiptLines(
 * detail)).split('\n')` — not `ReceiptLine[]`. That is deliberate: wrapping,
 * dot leaders and rule width are decided once, by `toaster/receipt.ts`, and
 * this module never re-flows what it is handed. Whatever the clipboard got,
 * the canvas draws, character for character. A long value (an
 * `original_filename`, when #518 lands one on the row) therefore breaks in
 * exactly the same place in both renderings, and the image simply grows wide
 * enough to hold the longest row rather than clipping it.
 *
 * Nothing here is markup: `fillText` draws the string literally, the same way
 * React renders the on-screen slip as a text node. There is no escaping seam
 * for document text to slip through in either rendering.
 */

/** Device-pixel multiplier, so the slip stays legible when it is dropped into
 *  a thread and scaled down. */
const SCALE = 2;
/** Narrowest image we ever emit, in CSS px — a short receipt still looks like
 *  a receipt rather than a label. */
const MIN_WIDTH = 420;
const LINE_HEIGHT = 22;
const PADDING = 24;
const FONT = '13px ui-monospace, SFMono-Regular, Menlo, monospace';
/** Advance width of one character at `FONT`, in CSS px. The stack is
 *  monospace, so a constant is exact enough to size the canvas and needs no
 *  `measureText` — which jsdom does not implement, and which would make the
 *  geometry untestable offline. */
const CHAR_WIDTH = 7.8;

/** The column budget `ToastReceipt` renders its rows at. Exported so the
 *  component and this module cannot pick different widths. */
export const RECEIPT_IMAGE_COLUMNS = 40;

/** Image width in CSS px: wide enough for the longest row it was given, never
 *  narrower than `MIN_WIDTH`. Sizing off the content rather than a fixed width
 *  is what keeps an over-long row (a long filename) whole instead of clipped
 *  against the right edge. */
export function receiptImageWidth(rows: readonly string[]): number {
  const longest = rows.reduce((widest, row) => Math.max(widest, row.length), 0);
  return Math.max(MIN_WIDTH, Math.ceil(PADDING * 2 + longest * CHAR_WIDTH));
}

/** Image height in CSS px, off the ROW count — never off the count of
 *  `ReceiptLine`s, which undercounts whenever a `wrap` line reflowed into
 *  more than one physical row and truncates it against the bottom edge. */
export function receiptImageHeight(rows: readonly string[]): number {
  return PADDING * 2 + rows.length * LINE_HEIGHT;
}

/** Draw the rows onto a 2D context. Exported so the export path and its test
 *  drive the same code — the test captures what was drawn. */
export function drawReceiptRows(ctx: CanvasRenderingContext2D, rows: readonly string[]): void {
  const width = receiptImageWidth(rows);
  const height = receiptImageHeight(rows);
  // Scale FIRST, then paint the ground in CSS px, so the fill covers the whole
  // device-pixel canvas. Filling before the scale covered only the top-left
  // quarter of it and left the rest of the PNG transparent.
  ctx.scale(SCALE, SCALE);
  ctx.fillStyle = '#fdfbf5';
  ctx.fillRect(0, 0, width, height);
  ctx.font = FONT;
  ctx.textBaseline = 'top';
  ctx.fillStyle = '#2a2119';
  rows.forEach((text, index) => {
    ctx.fillText(text, PADDING, PADDING + index * LINE_HEIGHT);
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

/**
 * Render `rows` to a PNG and hand it to the browser under `filename` — which
 * is always `receiptFilename()`'s `toast-receipt-<shortid>.png`, never a name
 * invented at a call site.
 *
 * Returns `false` when the browser gave no 2D context, so the caller can say
 * so rather than leave a button that silently does nothing. Reports success
 * by return value rather than by throwing: both callers are click handlers,
 * where a rejected promise is swallowed and a thrown error is worse.
 *
 * `canvas` is optional so a component can reuse the offscreen element it
 * already holds; the default is a throwaway one.
 */
export function saveReceiptImage(
  rows: readonly string[],
  filename: string,
  canvas: HTMLCanvasElement = document.createElement('canvas'),
): boolean {
  canvas.width = receiptImageWidth(rows) * SCALE;
  canvas.height = receiptImageHeight(rows) * SCALE;
  const ctx = canvas.getContext('2d');
  if (!ctx) {
    return false;
  }
  drawReceiptRows(ctx, rows);
  triggerDownload(canvas.toDataURL('image/png'), filename);
  return true;
}
