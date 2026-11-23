import { LocalWorkspace } from "../src/core/workspace";
import { startDashboard } from "../src/server/index";
import { createServer } from "vite";
const workspace = new LocalWorkspace();
const port = Number(process.env.OPENHACK_DEV_API_PORT ?? 6969);
const api = startDashboard({
  workspace,
  port,
  devOrigins: ["http://127.0.0.1:5173", "http://localhost:5173"],
});
const web = await createServer();
await web.listen();
console.log("OpenHack development: http://127.0.0.1:5173");
const stop = async () => {
  await workspace.shutdown();
  await web.close();
  await api.stop(true);
};
process.once("SIGINT", stop);
process.once("SIGTERM", stop);
