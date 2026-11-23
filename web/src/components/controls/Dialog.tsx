import { Dialog as BaseDialog } from "@base-ui/react/dialog";
import type { ReactNode, RefObject } from "react";
export interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: ReactNode;
  dismissible?: boolean;
  initialFocus?: RefObject<HTMLElement | null>;
}
export function Dialog({
  open,
  onOpenChange,
  children,
  dismissible = true,
  initialFocus,
}: DialogProps) {
  return (
    <BaseDialog.Root
      open={open}
      onOpenChange={(next, details) => {
        if (!next && !dismissible) {
          details.cancel();
          return;
        }
        onOpenChange(next);
      }}
    >
      <BaseDialog.Portal>
        <BaseDialog.Backdrop className="dialog-backdrop" />
        <BaseDialog.Viewport className="dialog-viewport">
          <BaseDialog.Popup className="dialog" initialFocus={initialFocus}>
            {children}
          </BaseDialog.Popup>
        </BaseDialog.Viewport>
      </BaseDialog.Portal>
    </BaseDialog.Root>
  );
}
export const DialogTitle = BaseDialog.Title;
export const DialogDescription = BaseDialog.Description;
