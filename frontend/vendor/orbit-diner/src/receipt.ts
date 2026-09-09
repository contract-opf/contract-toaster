/** Receives the same canonical lines as the existing receipt module. No new inferred facts. */
export const receiptText = (lines: readonly string[]) => lines.join("\n");
export async function copyReceipt(lines: readonly string[]) {
  await navigator.clipboard.writeText(receiptText(lines));
}
export async function saveReceipt(
  lines: readonly string[],
  filename = "review-receipt.png",
) {
  await document.fonts.ready;
  const canvas = document.createElement("canvas");
  canvas.width = 840;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("Image export unavailable");
  ctx.font = '28px "IBM Plex Mono", monospace';
  const wrapped: string[] = [];
  for (const line of lines) {
    let segment = "";
    for (const ch of line) {
      if (ctx.measureText(segment + ch).width > 744) {
        wrapped.push(segment);
        segment = ch;
      } else segment += ch;
    }
    wrapped.push(segment);
  }
  canvas.height = 112 + wrapped.length * 46;
  ctx.fillStyle = "#fff6e4";
  ctx.fillRect(0, 0, 840, canvas.height);
  ctx.fillStyle = "#213747";
  ctx.font = '28px "IBM Plex Mono", monospace';
  ctx.textBaseline = "top";
  wrapped.forEach((line, n) => ctx.fillText(line, 48, 48 + n * 46));
  const blob = await new Promise<Blob | null>((resolve) =>
    canvas.toBlob(resolve, "image/png"),
  );
  if (!blob) throw new Error("Image export unavailable");
  const url = URL.createObjectURL(blob),
    a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
