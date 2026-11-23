import { test, expect } from "bun:test";
import { buildSourceGraph } from "../../src/harness/source-graph";
import type { Project } from "../../src/harness/project";

test("source graph follows snapshot-local imports and exposes reverse callers and route leads", () => {
  const files = new Map([
    ["src/api/route.ts", `import { guard } from '@/auth';\nimport { db } from '../db';\nimport secret from '../../../outside';\nexport async function GET() { return guard(db); }`],
    ["src/auth/index.ts", "export const guard = x => x;"],
    ["src/db.ts", "export const db = {};"],
  ]);
  const graph = buildSourceGraph({files} as Project);
  expect(graph.summary).toEqual({files:3, importEdges:2, routeHandlers:1});
  expect(graph.neighbors("src/api/route.ts").edges).toEqual([
    {kind:"imports",file:"src/auth/index.ts"}, {kind:"imports",file:"src/db.ts"},
  ]);
  expect(graph.neighbors("src/db.ts").edges).toEqual([{kind:"imported_by",file:"src/api/route.ts"}]);
  expect(graph.neighbors("src/api/route.ts").routes).toEqual([{method:"GET",line:4}]);
  expect(graph.neighbors("../outside").error).toBeDefined();
});
