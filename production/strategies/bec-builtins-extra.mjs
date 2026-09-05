// Remaining BEC builtin strategy semantics adapted from jptsantossilva/BEC (MIT).
// Signal-only. Callers must provide closed candles only.

function sma(v,p){const o=Array(v.length).fill(null);let s=0;for(let i=0;i<v.length;i++){s+=v[i];if(i>=p)s-=v[i-p];if(i>=p-1)o[i]=s/p;}return o;}
function ema(v,p){const o=Array(v.length).fill(null);if(v.length<p)return o;let s=0;for(let i=0;i<p;i++)s+=v[i];o[p-1]=s/p;const k=2/(p+1);for(let i=p;i<v.length;i++)o[i]=v[i]*k+o[i-1]*(1-k);return o;}
function wma(v,p){const o=Array(v.length).fill(null);const den=p*(p+1)/2;for(let i=p-1;i<v.length;i++){let s=0;for(let j=0;j<p;j++)s+=v[i-p+1+j]*(j+1);o[i]=s/den;}return o;}
function hma(v,p){const half=Math.max(1,Math.floor(p/2)),root=Math.max(1,Math.floor(Math.sqrt(p)));const a=wma(v,half),b=wma(v,p),d=v.map((_,i)=>a[i]!=null&&b[i]!=null?2*a[i]-b[i]:null);const clean=d.map(x=>x==null?NaN:x);const o=Array(v.length).fill(null);const den=root*(root+1)/2;for(let i=root-1;i<v.length;i++){let s=0,ok=true;for(let j=0;j<root;j++){const x=clean[i-root+1+j];if(!Number.isFinite(x)){ok=false;break;}s+=x*(j+1);}if(ok)o[i]=s/den;}return o;}
function rsi(v,p=14){const o=Array(v.length).fill(null);if(v.length<=p)return o;let g=0,l=0;for(let i=1;i<=p;i++){const d=v[i]-v[i-1];if(d>=0)g+=d;else l-=d;}g/=p;l/=p;o[p]=l===0?100:100-100/(1+g/l);for(let i=p+1;i<v.length;i++){const d=v[i]-v[i-1],gg=Math.max(d,0),ll=Math.max(-d,0);g=(g*(p-1)+gg)/p;l=(l*(p-1)+ll)/p;o[i]=l===0?100:100-100/(1+g/l);}return o;}
function linreg(v,p){const o=Array(v.length).fill(null);const sx=(p-1)*p/2,sxx=(p-1)*p*(2*p-1)/6;for(let i=p-1;i<v.length;i++){let sy=0,sxy=0;for(let j=0;j<p;j++){const y=v[i-p+1+j];sy+=y;sxy+=j*y;}const den=p*sxx-sx*sx;const slope=(p*sxy-sx*sy)/den;const intercept=(sy-slope*sx)/p;o[i]=intercept+slope*(p-1);}return o;}
function ca(a,b,i){return i>0&&a[i-1]!=null&&b[i-1]!=null&&a[i]!=null&&b[i]!=null&&a[i-1]<=b[i-1]&&a[i]>b[i];}
function cb(a,b,i){return i>0&&a[i-1]!=null&&b[i-1]!=null&&a[i]!=null&&b[i]!=null&&a[i-1]>=b[i-1]&&a[i]<b[i];}
function values(x){return (x||[]).map(Number).filter(Number.isFinite);}

export const BEC_EXTRA_BUILTINS=Object.freeze([
 {id:'bec_ema_cross',source:'jptsantossilva/BEC',sourceLicense:'MIT',family:'trend',marketType:'spot',side:'long',validated:false},
 {id:'bec_market_phases',source:'jptsantossilva/BEC',sourceLicense:'MIT',family:'trend',marketType:'spot',side:'long',validated:false},
 {id:'bec_hma_rsi_linreg',source:'jptsantossilva/BEC',sourceLicense:'MIT',family:'trend',marketType:'spot',side:'long',validated:false},
 {id:'bec_bullmarketsupportband',source:'jptsantossilva/BEC',sourceLicense:'MIT',family:'trend',marketType:'spot',side:'long',validated:false},
 {id:'bec_wema20',source:'jptsantossilva/BEC',sourceLicense:'MIT',family:'trend',marketType:'spot',side:'long',validated:false},
]);

export function evaluateBecEmaCross(closes){const v=values(closes);if(v.length<22)return {action:'NO_SIGNAL',reason:'INSUFFICIENT_CANDLES'};const a=ema(v,10),b=ema(v,20),i=v.length-1;if(ca(a,b,i))return {action:'BUY_SIGNAL',reason:'EMA_CROSS_ABOVE'};if(cb(a,b,i))return {action:'SELL_SIGNAL',reason:'EMA_CROSS_BELOW'};return {action:'NO_SIGNAL',reason:'NO_CROSS'};}
export function evaluateBecMarketPhases(closes){const v=values(closes);if(v.length<201)return {action:'NO_SIGNAL',reason:'INSUFFICIENT_CANDLES'};const a=sma(v,50),b=sma(v,200),i=v.length-1;if(v[i]>a[i]&&v[i]>b[i])return {action:'BUY_SIGNAL',reason:'ABOVE_SMA50_SMA200'};if(v[i]<a[i]||v[i]<b[i])return {action:'SELL_SIGNAL',reason:'LOST_MARKET_PHASE'};return {action:'NO_SIGNAL',reason:'NEUTRAL'};}
export function evaluateBecHmaRsiLinreg(closes){const v=values(closes);if(v.length<205)return {action:'NO_SIGNAL',reason:'INSUFFICIENT_CANDLES'};const f=hma(v,20),s=hma(v,70),r=rsi(v,14),lr=linreg(v,50),i=v.length-1;if(ca(f,s,i)&&r[i]>52&&v[i]>lr[i])return {action:'BUY_SIGNAL',reason:'HMA_CROSS_RSI_LINREG'};if(cb(f,s,i))return {action:'SELL_SIGNAL',reason:'HMA_CROSS_BELOW'};return {action:'NO_SIGNAL',reason:'FILTER_NOT_MET'};}
export function evaluateBecBullMarketSupportBand(weeklyCloses){const v=values(weeklyCloses);if(v.length<24)return {action:'NO_SIGNAL',reason:'INSUFFICIENT_CANDLES'};const e=ema(v,21),s=sma(v,20),i=v.length-1;if(ca(e,s,i))return {action:'BUY_SIGNAL',reason:'WEEKLY_EMA21_CROSS_ABOVE_SMA20'};if(cb(e,s,i))return {action:'SELL_SIGNAL',reason:'WEEKLY_EMA21_CROSS_BELOW_SMA20'};return {action:'NO_SIGNAL',reason:'NO_CROSS'};}
export function evaluateBecWema20(weeklyCloses){const v=values(weeklyCloses);if(v.length<21)return {action:'NO_SIGNAL',reason:'INSUFFICIENT_CANDLES'};const e=ema(v,20),i=v.length-1;if(v[i]>e[i])return {action:'BUY_SIGNAL',reason:'WEEKLY_CLOSE_ABOVE_EMA20'};if(v[i]<e[i])return {action:'SELL_SIGNAL',reason:'WEEKLY_CLOSE_BELOW_EMA20'};return {action:'NO_SIGNAL',reason:'AT_EMA20'};}
