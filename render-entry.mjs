import app from "./server.mjs";

const port = Number(process.env.PORT || 10000);
const host = "0.0.0.0";

const server = app.listen(port, host, () => {
  console.log(`tst-spot-signal Render service listening on ${host}:${port}`);
});

function shutdown(signal) {
  console.log(`${signal} received; shutting down Render service`);
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(1), 10_000).unref();
}

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
