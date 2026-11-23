import { Button as BaseButton } from "@base-ui/react/button";
import type { ComponentProps } from "react";
export function Button({
  type = "button",
  ...props
}: ComponentProps<"button">) {
  return <BaseButton type={type} {...props} />;
}
