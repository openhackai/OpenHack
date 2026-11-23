import { z } from "zod";

const sourceString = z.string().max(12000);
const sourceOffset = z.number().int().min(0).max(Number.MAX_SAFE_INTEGER);
const sourceLine = z.number().int().min(1).max(Number.MAX_SAFE_INTEGER);
/** One source of truth for direct-provider callbacks and typed CLI requests. */
export const scanToolArgumentSchemas = {
  graph_neighbors: z.object({path: sourceString, offset: sourceOffset}).strict(),
  list_files: z.object({contains: sourceString, offset: sourceOffset}).strict(),
  read_file: z.object({path: sourceString, start: sourceLine}).strict(),
  search: z.object({query: sourceString, pathContains: sourceString}).strict(),
} as const;
export type ScanToolName = keyof typeof scanToolArgumentSchemas;
export const scanToolCallSchema = z.discriminatedUnion("name", [
  z.object({name: z.literal("graph_neighbors"), args: scanToolArgumentSchemas.graph_neighbors}).strict(),
  z.object({name: z.literal("list_files"), args: scanToolArgumentSchemas.list_files}).strict(),
  z.object({name: z.literal("read_file"), args: scanToolArgumentSchemas.read_file}).strict(),
  z.object({name: z.literal("search"), args: scanToolArgumentSchemas.search}).strict(),
]);
export type ScanToolCall = z.infer<typeof scanToolCallSchema>;

function tool(name: ScanToolName, description: string) {
  const schema = z.toJSONSchema(scanToolArgumentSchemas[name]);
  // Preserve the existing provider-compatible primitive signatures. The complete
  // constraints are enforced locally and in the CLI's structured-output schema.
  const properties = Object.fromEntries(Object.entries(schema.properties ?? {}).map(([key, value]) => [key, {type: typeof value === "object" ? value.type : undefined}]));
  return {name, description, parameters: {type: "object", properties, required: Object.keys(properties), additionalProperties: false}};
}
/** The only source-navigation capabilities exposed by scanner providers. */
export const scanTools = [
  tool("graph_neighbors", "Page through static imports and callers of a source file, with route-handler leads. Verify leads in source."),
  tool("list_files", "List source snapshot paths, 150 per page. Use empty contains for all."),
  tool("read_file", "Read up to 160 lines from an exact relative path, starting at a 1-based line."),
  tool("search", "Search a literal case-insensitive string. Empty pathContains searches all files."),
];

// Provider-side schemas do not replace validation at the execution boundary.
export function parseScanToolArguments(name: string, input: unknown): Record<string, unknown> {
  if (!Object.hasOwn(scanToolArgumentSchemas, name)) throw new Error("Invalid source tool request.");
  const result = scanToolArgumentSchemas[name as ScanToolName].safeParse(input);
  if (!result.success) throw new Error("Invalid source tool arguments.");
  return result.data;
}
