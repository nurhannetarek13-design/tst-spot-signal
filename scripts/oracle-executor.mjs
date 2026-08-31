import app from "../server.mjs";

const port = Number(process.env.PORT || 10000);

app.listen(port, "0.0.0.0", () => {
  console.log(`tst-spot-signal Oracle executor listening on ${port}`);
});
