import { NumberField } from "@base-ui/react/number-field";
import { Minus, Plus } from "../nucleo-icons";
export interface NumberInputProps {
  value: number;
  onValueChange: (value: number) => void;
  min?: number;
  max?: number;
  id?: string;
  name?: string;
  required?: boolean;
  disabled?: boolean;
  "aria-label"?: string;
}
export function NumberInput({
  value,
  onValueChange,
  "aria-label": label,
  ...props
}: NumberInputProps) {
  return (
    <NumberField.Root
      {...props}
      value={Number.isFinite(value) ? value : null}
      onValueChange={(next) => onValueChange(next ?? Number.NaN)}
      step={1}
      smallStep={1}
      format={{ useGrouping: false, maximumFractionDigits: 0 }}
      className="number-field"
    >
      <NumberField.Group className="control-field number-group">
        <NumberField.Input className="number-input" aria-label={label} />
        <NumberField.Decrement
          className="number-step"
          aria-label={label ? `Decrease ${label}` : "Decrease value"}
        >
          <Minus size={12} />
        </NumberField.Decrement>
        <NumberField.Increment
          className="number-step"
          aria-label={label ? `Increase ${label}` : "Increase value"}
        >
          <Plus size={12} />
        </NumberField.Increment>
      </NumberField.Group>
    </NumberField.Root>
  );
}
