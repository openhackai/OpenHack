import { Field } from "@base-ui/react/field";
import type { ComponentProps } from "react";
/** Base UI's polymorphic field control, rendered as an authored multiline field. */
export function Textarea({
  className = "",
  ...props
}: ComponentProps<"textarea">) {
  return (
    <Field.Control
      render={<textarea {...props} />}
      className={`control-field control-textarea ${className}`}
    />
  );
}
