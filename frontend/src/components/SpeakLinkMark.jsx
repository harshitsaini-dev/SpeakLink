/**
 * The SpeakLink mark, inline.
 *
 * Inline rather than an <img> so it takes the surrounding text colour: the
 * same component serves the dark sidebar, the login card and a light panel
 * without three copies of the artwork drifting apart. An <img> loads a
 * separate document and cannot inherit currentColor at all.
 *
 * The geometry is the one in assets/brand/speaklink-mark.svg, drawn on a 64
 * unit grid and judged at 16px - which is why there is one wave each side and
 * not three, and why the transmitter dot is solid.
 */
export default function SpeakLinkMark({ size = 24, className = "" }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      role="img"
      aria-label="SpeakLink"
      className={className}
    >
      <title>SpeakLink</title>
      <g
        fill="none"
        stroke="currentColor"
        strokeWidth="5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M25 53 L32 21 L39 53" />
        <path d="M27.5 41 L36.5 41" />
        <path d="M21 29 A 14 14 0 0 1 21 11" />
        <path d="M43 11 A 14 14 0 0 1 43 29" />
      </g>
      <circle cx="32" cy="20" r="5" fill="currentColor" />
    </svg>
  );
}
