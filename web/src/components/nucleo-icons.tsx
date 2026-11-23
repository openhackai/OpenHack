import type { SVGProps, ReactNode } from "react";

/**
 * Existing product controls reuse the exact OpenHack app icon geometry.
 * New local-only controls use the installed Nucleo UI library. The manifest
 * records each app export and its corresponding Desktop Nucleo source.
 */
export type IconProps = SVGProps<SVGSVGElement> & { size?: number };
function NucleoSvg({
  size = 16,
  children,
  ...props
}: IconProps & { children: ReactNode }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 18 18"
      width={size}
      height={size}
      aria-hidden="true"
      focusable="false"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
      style={{ flexShrink: 0, ...props.style }}
    >
      {children}
    </svg>
  );
}

/** OpenHack app: components/ui/nucleo-icons.tsx#IconChevronRight */
export const ChevronRight = (p: IconProps) => (
  <NucleoSvg {...p}>
    <polyline points="6.5 2.75 12.75 9 6.5 15.25" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconChevronLeft */
export const ArrowLeft = (p: IconProps) => (
  <NucleoSvg {...p}>
    <polyline points="11.5 15.25 5.25 9 11.5 2.75" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconArrowUpRight */
export const ArrowUpRight = (p: IconProps) => (
  <NucleoSvg {...p}>
    <line x1="14.25" y1="3.75" x2="3.75" y2="14.25" />
    <polyline points="8.24 3.75 14.25 3.75 14.25 9.76" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconCheck */
export const Check = (p: IconProps) => (
  <NucleoSvg {...p}>
    <polyline points="2.75 9.25 6.75 14.25 15.25 3.75" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconCopy */
export const Copy = (p: IconProps) => (
  <NucleoSvg {...p}>
    <path d="M2.25 6.75V13.25C2.25 14.355 3.145 15.25 4.25 15.25H11.75" />
    <path d="M7.25 12.25H13.75C14.855 12.25 15.75 11.355 15.75 10.25V4.75C15.75 3.645 14.855 2.75 13.75 2.75H7.25C6.145 2.75 5.25 3.645 5.25 4.75V10.25C5.25 11.355 6.145 12.25 7.25 12.25Z" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconRefresh */
export const RefreshCw = (p: IconProps) => (
  <NucleoSvg {...p}>
    <path d="M1.75 3.25L2.29 6.25C3.373 3.61 5.969 1.75 9 1.75C12.9367 1.75 16.1407 4.89165 16.2473 8.79895" />
    <path d="M16.25 14.75L15.71 11.75C14.627 14.39 12.031 16.25 8.99998 16.25C5.06328 16.25 1.85929 13.1083 1.75269 9.20093" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconFolder */
export const Folder = (p: IconProps) => (
  <NucleoSvg {...p}>
    <path d="M2.25,8.75V4.75c0-1.105,.895-2,2-2h1.951c.607,0,1.18,.275,1.56,.748l.603,.752h5.386c1.105,0,2,.895,2,2v2.844" />
    <path d="M4.25,6.75H13.75c1.105,0,2,.895,2,2v4.5c0,1.105-.895,2-2,2H4.25c-1.105,0-2-.895-2-2v-4.5c0-1.105,.895-2,2-2Z" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconPlay */
export const Play = (p: IconProps) => (
  <NucleoSvg strokeWidth={1.75} {...p}>
    <path d="M5.245,2.878l9.492,5.256c.685,.379,.685,1.353,0,1.732L5.245,15.122c-.669,.371-1.495-.108-1.495-.866V3.744c0-.758,.825-1.237,1.495-.866Z" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconMediaStopFill */
export const Square = (p: IconProps) => (
  <NucleoSvg {...p} stroke="none" fill="currentColor">
    <rect x="2" y="2" width="14" height="14" rx="2.75" ry="2.75" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconStatusNewDots */
export const Circle = (p: IconProps) => (
  <NucleoSvg {...p}>
    <path d="M4.987,2.961c.778-.518,1.662-.89,2.612-1.075" />
    <path d="M1.879,7.631c.185-.968,.562-1.867,1.091-2.657" />
    <path d="M4.987,15.039c.778,.518,1.662,.89,2.612,1.075" />
    <path d="M1.879,10.369c.185,.968,.562,1.867,1.091,2.657" />
    <path d="M10.401,1.886c.95,.185,1.834,.557,2.612,1.075" />
    <path d="M15.03,4.974c.529,.79,.906,1.689,1.091,2.657" />
    <path d="M10.401,16.114c.95-.185,1.834-.557,2.612-1.075" />
    <path d="M15.03,13.026c.529-.79,.906-1.689,1.091-2.657" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconXmark */
export const X = (p: IconProps) => (
  <NucleoSvg {...p}>
    <line x1="14" y1="4" x2="4" y2="14" />
    <line x1="4" y1="4" x2="14" y2="14" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconStatusWontFix */
export const XCircle = (p: IconProps) => (
  <NucleoSvg {...p}>
    <circle cx="9" cy="9" r="7.25" />
    <line x1="6.25" y1="6.25" x2="11.75" y2="11.75" />
    <line x1="11.75" y1="6.25" x2="6.25" y2="11.75" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconCircleCheck */
export const CheckCircle2 = (p: IconProps) => (
  <NucleoSvg {...p}>
    <circle cx="9" cy="9" r="7.25" />
    <polyline points="5.75 9.25 8 11.75 12.25 6.25" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconCircleWarning */
export const CircleAlert = (p: IconProps) => (
  <NucleoSvg {...p}>
    <path d="M9 16.25C13.0041 16.25 16.25 13.004 16.25 9C16.25 4.996 13.0041 1.75 9 1.75C4.9959 1.75 1.75 4.996 1.75 9C1.75 13.004 4.9959 16.25 9 16.25Z" />
    <path d="M9 5.431V9.5" />
    <path
      d="M9 13.417C8.448 13.417 8 12.968 8 12.417C8 11.866 8.448 11.417 9 11.417C9.552 11.417 10 11.866 10 12.417C10 12.968 9.552 13.417 9 13.417Z"
      fill="currentColor"
      stroke="none"
    />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconStatusFalsePositive */
export const CircleSlash = (p: IconProps) => (
  <NucleoSvg {...p}>
    <circle cx="9" cy="9" r="7.25" />
    <line x1="4" y1="14" x2="14" y2="4" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconArrowRotateClockwise */
export const LoaderCircle = (p: IconProps) => (
  <NucleoSvg {...p}>
    <path d="M15,13.071c-1.304,1.919-3.505,3.179-6,3.179-4.004,0-7.25-3.246-7.25-7.25S4.996,1.75,9,1.75c3.031,0,5.627,1.86,6.71,4.5" />
    <polyline points="16.12 3.305 15.712 6.25 12.768 5.843" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconSearch */
export const Search = (p: IconProps) => (
  <NucleoSvg {...p}>
    <path d="M15.75 15.75L11.639 11.639" />
    <path d="M7.75 13.25C10.787 13.25 13.25 10.787 13.25 7.75C13.25 4.713 10.787 2.25 7.75 2.25C4.713 2.25 2.25 4.713 2.25 7.75C2.25 10.787 4.713 13.25 7.75 13.25Z" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconStatusVerified */
export const ShieldCheck = (p: IconProps) => (
  <NucleoSvg {...p}>
    <path d="M9.305,1.848l5.25,1.68c.414,.133,.695,.518,.695,.952v6.52c0,3.03-4.684,4.748-5.942,5.155-.203,.066-.413,.066-.616,0-1.258-.407-5.942-2.125-5.942-5.155V4.48c0-.435,.281-.82,.695-.952l5.25-1.68c.198-.063,.411-.063,.61,0Z" />
    <polyline points="6.497 9.75 8.106 11.25 11.503 6.75" />
  </NucleoSvg>
);

/** Nucleo UI: outline/18px/Files/floppy-disk.svg; local harness settings action. */
export function Save(props: IconProps) {
  return (
    <NucleoSvg viewBox="0 0 18 18" {...props}>
      <g className="nc-icon-wrapper">
        <path
          d="M10.75,2.25v3c0,.552-.448,1-1,1h-3.5c-.552,0-1-.448-1-1V2.25"
          fill="none"
          stroke="currentColor"
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth="1.5"
          data-color="color-2"
        />
        <path
          d="M5.25,15.75v-5c0-.552,.448-1,1-1h5.5c.552,0,1,.448,1,1v5"
          fill="none"
          stroke="currentColor"
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth="1.5"
          data-color="color-2"
        />
        <path
          d="M13.59,15.75H4.41c-1.193,0-2.16-.967-2.16-2.16V4.41c0-1.193,.967-2.16,2.16-2.16h7.426c.265,0,.52,.105,.707,.293l2.914,2.914c.188,.188,.293,.442,.293,.707v7.426c0,1.193-.967,2.16-2.16,2.16Z"
          fill="none"
          stroke="currentColor"
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth="1.5"
        />
      </g>
    </NucleoSvg>
  );
}

/** OpenHack app: components/ui/nucleo-icons.tsx#IconMonitor */
export const Monitor = (p: IconProps) => (
  <NucleoSvg {...p}>
    <path d="M5.75 15.75C6.508 15.511 7.628 15.25 9 15.25C9.795 15.25 10.941 15.338 12.25 15.75" />
    <path d="M9 12.75V15.25" />
    <path d="M14.25 2.75H3.75C2.64543 2.75 1.75 3.64543 1.75 4.75V10.75C1.75 11.8546 2.64543 12.75 3.75 12.75H14.25C15.3546 12.75 16.25 11.8546 16.25 10.75V4.75C16.25 3.64543 15.3546 2.75 14.25 2.75Z" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconMoon */
export const Moon = (p: IconProps) => (
  <NucleoSvg {...p}>
    <path d="M13,11.75c-3.452,0-6.25-2.798-6.25-6.25,0-1.352,.433-2.599,1.162-3.622-3.364,.628-5.912,3.575-5.912,7.122,0,4.004,3.246,7.25,7.25,7.25,3.372,0,6.198-2.306,7.009-5.424-.95,.583-2.063,.924-3.259,.924Z" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconSun */
export const Sun = (p: IconProps) => (
  <NucleoSvg {...p}>
    <line x1="9" y1="1.25" x2="9" y2="2.25" />
    <line x1="14.48" y1="3.52" x2="13.773" y2="4.227" />
    <line x1="16.75" y1="9" x2="15.75" y2="9" />
    <line x1="14.48" y1="14.48" x2="13.773" y2="13.773" />
    <line x1="9" y1="16.75" x2="9" y2="15.75" />
    <line x1="3.52" y1="14.48" x2="4.227" y2="13.773" />
    <line x1="1.25" y1="9" x2="2.25" y2="9" />
    <line x1="3.52" y1="3.52" x2="4.227" y2="4.227" />
    <circle cx="9" cy="9" r="4.25" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconSidebarToggle */
export const PanelLeftClose = (p: IconProps) => (
  <NucleoSvg {...p}>
    <g fill="none">
      <rect x="1.75" y="3.25" width="14.5" height="11.5" rx="2" ry="2" />
      <rect x="11.25" y="6.25" width="2" height="5.5" fill="currentColor" />
    </g>
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconSidebarToggle */
export const PanelLeftOpen = (p: IconProps) => (
  <NucleoSvg {...p}>
    <g fill="none">
      <rect x="1.75" y="3.25" width="14.5" height="11.5" rx="2" ry="2" />
      <rect x="11.25" y="6.25" width="2" height="5.5" fill="currentColor" />
    </g>
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconNavOverview */
export const IconNavOverview = (p: IconProps) => (
  <NucleoSvg {...p}>
    <rect x="2.75" y="2.75" width="4.5" height="4.5" rx="1" ry="1" />
    <rect x="10.75" y="10.75" width="4.5" height="4.5" rx="1" ry="1" />
    <circle cx="13" cy="5" r="2.5" />
    <circle cx="5" cy="13" r="2.5" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconNavScans */
export const IconNavScans = (p: IconProps) => (
  <NucleoSvg {...p}>
    <path d="m15.16,6.25h-3.41c-.552,0-1-.448-1-1V1.852" />
    <path d="m15.25,13.25v1c0,1.105-.895,2-2,2H4.75c-1.105,0-2-.895-2-2v-1" />
    <path d="m2.75,10.25V3.75c0-1.105.895-2,2-2h5.586c.265,0,.52.105.707.293l3.914,3.914c.188.188.293.442.293.707v3.586" />
    <line x1="1.5" y1="10.25" x2="16.5" y2="10.25" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconNavShield */
export const IconNavShield = (p: IconProps) => (
  <NucleoSvg {...p}>
    <path d="M9.305,1.848l5.25,1.68c.414,.133,.695,.518,.695,.952v6.52c0,3.03-4.684,4.748-5.942,5.155-.203,.066-.413,.066-.616,0-1.258-.407-5.942-2.125-5.942-5.155V4.48c0-.435,.281-.82,.695-.952l5.25-1.68c.198-.063,.411-.063,.61,0Z" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconNavSettings */
export const IconNavSettings = (p: IconProps) => (
  <NucleoSvg {...p}>
    <path d="M9 11.249C10.243 11.249 11.25 10.242 11.25 9C11.25 7.757 10.243 6.75 9 6.75C7.757 6.75 6.75 7.757 6.75 9C6.75 10.242 7.757 11.249 9 11.249Z" />
    <path d="M16.25 9.354V8.645C16.25 8.134 15.867 7.707 15.36 7.651L14.266 7.529L13.763 6.315L14.451 5.456C14.769 5.058 14.738 4.484 14.377 4.123L13.875 3.621C13.515 3.261 12.941 3.229 12.543 3.547L11.684 4.236L10.47 3.733L10.348 2.638C10.292 2.132 9.864 1.748 9.354 1.748H8.644C8.134 1.748 7.706 2.131 7.65 2.638L7.528 3.733L6.314 4.236L5.455 3.548C5.057 3.23 4.484 3.261 4.123 3.623L3.621 4.125C3.261 4.485 3.229 5.059 3.547 5.457L4.235 6.315L3.732 7.53L2.638 7.651C2.132 7.707 1.748 8.136 1.748 8.646V9.355C1.748 9.866 2.131 10.293 2.638 10.35L3.732 10.472L4.235 11.685L3.548 12.544C3.23 12.943 3.261 13.517 3.622 13.877L4.124 14.379C4.484 14.739 5.058 14.771 5.456 14.453L6.315 13.764L7.529 14.268L7.651 15.361C7.707 15.867 8.135 16.252 8.645 16.252H9.355C9.865 16.252 10.293 15.868 10.349 15.361L10.471 14.268L11.685 13.764L12.544 14.453C12.942 14.771 13.515 14.739 13.876 14.379L14.378 13.877C14.738 13.517 14.77 12.943 14.452 12.544L13.765 11.685L14.268 10.472L15.362 10.35C15.868 10.293 16.252 9.866 16.252 9.355L16.25 9.354Z" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconChevronDown */
export const ChevronDown = (p: IconProps) => (
  <NucleoSvg {...p}>
    <polyline points="15.25 6.5 9 12.75 2.75 6.5" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconPlus */
export const Plus = (p: IconProps) => (
  <NucleoSvg {...p}>
    <line x1="9" y1="3.25" x2="9" y2="14.75" />
    <line x1="3.25" y1="9" x2="14.75" y2="9" />
  </NucleoSvg>
);

/** OpenHack app: components/ui/nucleo-icons.tsx#IconScMinus */
export const Minus = (p: IconProps) => (
  <NucleoSvg {...p}>
    <line x1="3.25" y1="9" x2="14.75" y2="9" />
  </NucleoSvg>
);
