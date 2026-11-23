import { useId } from "react";

/** Gold logo geometry and colors from app/components/openhack-logo.tsx. */
export function ProLogo({ size = 18 }: { size?: number }) {
  const gradient = useId();
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 166 181"
      fill="none"
      aria-hidden="true"
    >
      <defs>
        <linearGradient
          id={gradient}
          x1="-24.89"
          y1="31"
          x2="52.61"
          y2="199"
          gradientUnits="userSpaceOnUse"
        >
          <stop stopColor="#FCD34D" />
          <stop offset="1" stopColor="#CA8A04" />
        </linearGradient>
      </defs>
      <path
        d="M1 108V90.25H73.75V0.999988H91.5V90.25H164.25V108H1ZM46 179.25V161.5H119.25V179.25H46ZM23.5 143.5V125.75H141.75V143.5H23.5Z"
        fill={`url(#${gradient})`}
      />
    </svg>
  );
}
