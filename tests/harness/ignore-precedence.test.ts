import { expect, test } from "bun:test";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { inventory } from "../../src/harness/project";

test("nested ignore exceptions override ancestor patterns without reopening excluded directories or secrets", async () => {
  const root = await mkdtemp(join(tmpdir(), "openhack-ignore-"));
  try {
    await mkdir(join(root, "src"));
    await mkdir(join(root, "excluded"));
    await writeFile(join(root, ".gitignore"), "*.ts\nexcluded/\n");
    await writeFile(join(root, "src/.gitignore"), "!app.ts\n!.env\n");
    await writeFile(join(root, "src/app.ts"), "export const app = true;\n");
    await writeFile(join(root, "src/other.ts"), "export const other = true;\n");
    await writeFile(join(root, "src/.env"), "SECRET=fixture\n");
    await writeFile(join(root, "excluded/.gitignore"), "!app.ts\n");
    await writeFile(join(root, "excluded/app.ts"), "export const excluded = true;\n");
    const project = await inventory(root);
    expect([...project.files.keys()]).toEqual(["src/app.ts"]);
    await writeFile(join(root, "src/.openhackignore"), "app.ts\n");
    expect([...(await inventory(root)).files.keys()]).toEqual([]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
