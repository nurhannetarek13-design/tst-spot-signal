import express from 'express';

const app = express();
app.get('/', (_req, res) => res.status(200).json({ ok: true, service: 'tst-spot-signal' }));

export default app;
