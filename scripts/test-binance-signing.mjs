import assert from "node:assert/strict";
import {
  canonicalBinancePayload,
  hmacSha256Hex,
  buildSignedBinanceQuery,
  computeServerTimeOffset,
} from "../src/binance-signing.js";

const params={
  symbol:"LTCBTC",
  side:"BUY",
  type:"LIMIT",
  timeInForce:"GTC",
  quantity:"1",
  price:"0.1",
};
const payload=canonicalBinancePayload(params,{timestampMs:1499827319559,recvWindow:5000});
assert.equal(
  payload,
  "price=0.1&quantity=1&recvWindow=5000&side=BUY&symbol=LTCBTC&timeInForce=GTC&timestamp=1499827319559&type=LIMIT",
);

const secret="NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j";
assert.equal(
  await hmacSha256Hex(secret,payload),
  "70fd30433bc3a2e3b5ff17d075e50538dde3734841da6dc28d79113dd37fa9c7",
);

const unicode=canonicalBinancePayload(
  {symbol:"１２３４５６",side:"SELL",type:"LIMIT"},
  {timestampMs:1668481559918,recvWindow:5000},
);
assert.match(unicode,/symbol=%EF%BC%91%EF%BC%92%EF%BC%93%EF%BC%94%EF%BC%95%EF%BC%96/);
assert.doesNotMatch(unicode,/１２３４５６/);

const credentials={apiKey:"A".repeat(32),hmacSecret:secret,ed25519PrivateKey:"",signingMode:"HMAC"};
const signed=await buildSignedBinanceQuery(credentials,{symbol:"BTCUSDT",side:"BUY",type:"MARKET",quoteOrderQty:"5.50"},{timestampMs:1800000000000,recvWindow:5000});
assert.equal(new URLSearchParams(signed.query).get("signature"),signed.signature);
assert.equal(new URLSearchParams(signed.query).getAll("timestamp").length,1);
assert.equal(new URLSearchParams(signed.query).getAll("recvWindow").length,1);
assert.equal(signed.timestampMs,1800000000000);
assert.equal(signed.recvWindow,5000);

const timing=computeServerTimeOffset({localBeforeMs:1000,localAfterMs:1100,serverTimeMs:1075});
assert.equal(timing.midpointMs,1050);
assert.equal(timing.offsetMs,25);
assert.equal(timing.roundTripMs,100);

assert.throws(
  ()=>canonicalBinancePayload({symbol:"BTCUSDT"},{timestampMs:1800000000000,recvWindow:60001}),
  /BAD_BINANCE_RECV_WINDOW/,
);

console.log("BINANCE_SIGNING_SELFTEST_PASS");
