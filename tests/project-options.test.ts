import { expect, test } from "bun:test";
import { projectOptions } from "../web/src/project-options";

test("project filters keep distinct roots with the same folder name", () => {
  expect(projectOptions([
    { root: "/client/app", project: "app" },
    { root: "/server/app", project: "app" },
    { root: "/client/app", project: "app" },
    { root: "/tools", project: "tools" },
  ])).toEqual([
    { value: "/client/app", label: "app (/client/app)" },
    { value: "/server/app", label: "app (/server/app)" },
    { value: "/tools", label: "tools" },
  ]);
});
