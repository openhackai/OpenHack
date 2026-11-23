import { spawn } from "node:child_process";
export function openBrowser(url: string): Promise<void> {
  const [command, args] = process.platform === "darwin" ? ["open", [url]] as const
    : process.platform === "win32" ? ["rundll32", ["url.dll,FileProtocolHandler", url]] as const
    : ["xdg-open", [url]] as const;
  return new Promise((resolve, reject) => {
    const child = spawn(command, [...args], {stdio: "ignore", shell: false});
    child.once("error", reject);
    child.once("exit", code => code === 0 ? resolve() : reject(new Error("Could not open your browser.")));
  });
}
