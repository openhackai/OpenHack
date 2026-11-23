import { Select as BaseSelect } from "@base-ui/react/select";
import { Check, ChevronDown } from "../nucleo-icons";

export interface SelectOption {
  value: string;
  label: string;
}
export interface SelectProps {
  value: string;
  onValueChange: (value: string) => void;
  options: readonly SelectOption[];
  className?: string;
  /** Muted label shown before the value inside the trigger ("Project  All"). */
  prefix?: string;
  id?: string;
  name?: string;
  disabled?: boolean;
  required?: boolean;
  "aria-label"?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean;
}

/** Shared OpenHack select: a keyboard-navigable, portalled Base UI listbox. */
export function Select({
  value,
  onValueChange,
  options,
  className = "",
  prefix,
  id,
  name,
  disabled,
  required,
  ...aria
}: SelectProps) {
  return (
    <BaseSelect.Root
      value={value}
      onValueChange={(next) => {
        if (next !== null) onValueChange(next);
      }}
      items={options}
      id={id}
      name={name}
      disabled={disabled}
      required={required}
    >
      <BaseSelect.Trigger
        className={`control-field select-trigger ${className}`}
        {...aria}
      >
        {prefix && <span className="select-prefix">{prefix}</span>}
        <BaseSelect.Value className="select-value" />
        <BaseSelect.Icon className="select-icon">
          <ChevronDown size={12} />
        </BaseSelect.Icon>
      </BaseSelect.Trigger>
      <BaseSelect.Portal>
        <BaseSelect.Positioner
          className="select-positioner"
          sideOffset={6}
          alignItemWithTrigger={false}
          align="start"
        >
          <BaseSelect.Popup className="select-popup">
            <BaseSelect.List className="select-list">
              {options.map((option) => (
                <BaseSelect.Item
                  key={option.value}
                  value={option.value}
                  className="select-item"
                >
                  <BaseSelect.ItemText>{option.label}</BaseSelect.ItemText>
                  <BaseSelect.ItemIndicator className="select-indicator">
                    <Check size={14} />
                  </BaseSelect.ItemIndicator>
                </BaseSelect.Item>
              ))}
            </BaseSelect.List>
          </BaseSelect.Popup>
        </BaseSelect.Positioner>
      </BaseSelect.Portal>
    </BaseSelect.Root>
  );
}
