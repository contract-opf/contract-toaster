import { useId } from "react";
import { platePath } from "./plates";
const material = {
  toaster: {
    view: "0 0 1536 1024",
    path: "M443 48 L1145 48 C1267 46 1347 113 1377 253 C1417 434 1434 696 1439 881 Q1446 920 1396 932 L1385 955 Q1376 963 1350 963 L1268 963 L1240 929 L294 929 L275 961 L178 960 Q151 955 144 934 Q93 921 95 885 L112 375 C126 176 182 69 443 48Z",
  },
  register: {
    view: "0 0 1536 1024",
    path: "M324 48 L1256 48 Q1359 43 1389 112 C1430 260 1469 534 1493 798 L1496 910 Q1497 945 1460 950 L1441 975 L1380 975 L1355 951 L190 951 L169 977 L82 977 L56 958 Q28 945 33 909 L36 804 C68 517 131 190 179 97 Q207 47 324 48Z",
  },
  toast: {
    view: "0 0 1254 1254",
    path: "M141 418 C47 427 35 374 59 287 C96 130 253 50 437 49 Q575 48 620 116 Q672 49 794 55 C998 62 1145 152 1185 289 C1216 382 1197 417 1128 427 Q1194 443 1180 540 L1196 1109 Q1195 1180 1126 1183 L115 1181 Q55 1174 56 1121 L56 511 Q48 454 141 418Z",
  },
  pad: {
    view: "0 0 1024 1536",
    path: "M259 28 L765 28 L774 65 L931 66 Q993 68 991 128 L993 1399 Q995 1479 929 1484 L94 1482 Q29 1480 30 1417 L35 134 Q34 70 95 69 L248 69Z",
  },
  dial: {
    view: "0 0 1254 1254",
    path: "M1193 596 A566 553 0 1 1 61 596 A566 553 0 1 1 1193 596Z",
  },
};
export type Material = keyof typeof material;
export function MaterialArt({
  kind,
  base,
  className = "",
  part,
  compact = false,
  toasterVariant = "desktop",
}: {
  kind: Material;
  /** Serve the plates from this folder instead of the bundled, fingerprinted ones. */
  base?: string;
  className?: string;
  part?: string;
  compact?: boolean;
  /** Match the .od-console content-width breakpoint: phone at <= 560px. */
  toasterVariant?: "desktop" | "phone";
}) {
  const id = useId().replace(/:/g, "");
  const spec = material[kind];
  if (kind === "pad")
    return (
      <div className="od-pad-art" aria-hidden="true" data-part={part ?? "pad"}>
        {[
          ["top", "0 0 1024 350"],
          ["middle", "0 350 1024 850"],
          ["bottom", "0 1200 1024 336"],
        ].map(([section, view]) => (
          <svg
            key={section}
            className={`od-material od-pad-${section}`}
            viewBox={view}
            preserveAspectRatio={
              section === "middle" ? "none" : "xMidYMid meet"
            }
            focusable="false"
          >
            <defs>
              <clipPath id={`${id}-${section}`}>
                <path d={spec.path} />
              </clipPath>
            </defs>
            <image
              href={platePath("pad", base)}
              width={1024}
              height={1536}
              clipPath={`url(#${id}-${section})`}
            />
          </svg>
        ))}
      </div>
    );
  return (
    <svg
      className={`od-material ${className}`}
      data-part={part ?? kind}
      viewBox={compact && kind === "register" ? "0 0 1536 268" : spec.view}
      preserveAspectRatio="xMidYMid meet"
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <clipPath id={id}>
          <path d={spec.path} />
        </clipPath>
      </defs>
      <image
        href={platePath(
          kind === "toaster" && toasterVariant === "phone"
            ? "toaster-phone"
            : kind,
          base,
        )}
        width={kind === "toast" || kind === "dial" ? 1254 : 1536}
        height={kind === "toast" || kind === "dial" ? 1254 : 1024}
        clipPath={`url(#${id})`}
      />
    </svg>
  );
}
export function SmallArt({
  kind,
  part,
}: {
  kind: "butter" | "steam" | "handle";
  part?: string;
}) {
  const id = useId().replace(/:/g, "");
  if (kind === "steam")
    return (
      <svg
        viewBox="0 0 180 180"
        className="od-decoration od-steam"
        aria-hidden="true"
        data-part="steam"
      >
        <g fill="none" stroke="#c2cbd2" strokeWidth="8" strokeLinecap="round">
          <path d="M48 165 C15 115 93 105 60 45" />
          <path d="M91 145 C133 91 62 94 102 12" />
          <path d="M129 170 C174 122 112 98 147 57" />
        </g>
      </svg>
    );
  if (kind === "butter")
    return (
      <svg
        viewBox="0 0 120 74"
        aria-hidden="true"
        className="od-decoration od-butter-art"
        data-part={part ?? "butter"}
      >
        <defs>
          <linearGradient id={id} x2="0" y2="1">
            <stop stopColor="#fff5b1" />
            <stop offset=".55" stopColor="#f4d667" />
            <stop offset="1" stopColor="#caa64e" />
          </linearGradient>
        </defs>
        <path
          d="M12 27 L74 8 L110 29 L43 54Z"
          fill="#fff0a6"
          stroke="#c7a54d"
        />
        <path d="M12 27 L43 54 L43 67 L12 42Z" fill="#cdb367" />
        <path d="M43 54 L110 29 L110 43 L43 67Z" fill={`url(#${id})`} />
        <path
          d="M17 27L75 12L105 29"
          fill="none"
          stroke="#fff9d3"
          strokeWidth="3"
        />
      </svg>
    );
  return (
    <svg viewBox="0 0 140 100" aria-hidden="true" className="od-decoration">
      <defs>
        <linearGradient id={id} x2="0" y2="1">
          <stop stopColor="#fffdf4" />
          <stop offset=".1" stopColor="#737d86" />
          <stop offset=".18" stopColor="#f7ebda" />
          <stop offset=".6" stopColor="#e7d7c1" />
          <stop offset=".77" stopColor="#b3a693" />
          <stop offset=".88" stopColor="#fff3db" />
          <stop offset="1" stopColor="#444b53" />
        </linearGradient>
      </defs>
      <path
        d="M13 18L115 10L130 28L130 79L113 91L14 88L5 75L5 32Z"
        fill={`url(#${id})`}
        stroke="#545250"
        strokeWidth="3"
      />
      <rect
        x="17"
        y="23"
        width="101"
        height="54"
        rx="8"
        fill={`url(#${id})`}
        stroke="#fff8ea"
        strokeWidth="2"
      />
    </svg>
  );
}
