import { Tooltip as BaseTooltip } from "@base-ui/react/tooltip";
import type { ReactElement } from "react";
export function Tooltip({
  children,
  content,
}: {
  children: ReactElement;
  content: string;
}) {
  return (
    <BaseTooltip.Root>
      <BaseTooltip.Trigger render={children} />
      <BaseTooltip.Portal>
        <BaseTooltip.Positioner sideOffset={6} className="tooltip-positioner">
          <BaseTooltip.Popup className="tooltip-popup">
            {content}
          </BaseTooltip.Popup>
        </BaseTooltip.Positioner>
      </BaseTooltip.Portal>
    </BaseTooltip.Root>
  );
}
export const TooltipProvider = BaseTooltip.Provider;
