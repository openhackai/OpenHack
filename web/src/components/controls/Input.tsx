import { Input as BaseInput } from "@base-ui/react/input";
import type { ComponentProps } from "react";
export function Input({ className = "", ...props }: ComponentProps<"input">) {
  return <BaseInput {...props} className={`control-field ${className}`} />;
}
