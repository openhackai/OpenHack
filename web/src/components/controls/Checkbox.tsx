import { Checkbox as BaseCheckbox } from "@base-ui/react/checkbox";
import { Check } from "../nucleo-icons";
export interface CheckboxProps {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
  id?: string;
  "aria-label"?: string;
}
export function Checkbox(props: CheckboxProps) {
  return (
    <BaseCheckbox.Root {...props} className="control-checkbox">
      <BaseCheckbox.Indicator className="checkbox-indicator">
        <Check size={12} />
      </BaseCheckbox.Indicator>
    </BaseCheckbox.Root>
  );
}
