import { useEffect, useState } from "react";
import { RequestGate } from "./request-gate";

/** Stops observing a request on unmount; it does not undo server mutations. */
export function useRequestGate(): RequestGate {
  const [gate] = useState(() => new RequestGate());
  useEffect(() => () => gate.cancel(), [gate]);
  return gate;
}
