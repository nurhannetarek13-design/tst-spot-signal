#!/usr/bin/env python3
import io,json,urllib.request,zipfile
import pandas as pd
BASE='https://data.binance.vision/data/futures/um'
SYMBOL='BTCUSDT'; START=pd.Timestamp('2026-03-01T00:00:00Z');END=pd.Timestamp('2026-09-01T00:00:00Z')
COLS=['open_time','open','high','low','close','volume','close_time','quote_volume','count','taker_buy_volume','taker_buy_quote_volume','ignore']
months=['2026-03','2026-04','2026-05','2026-06','2026-07','2026-08']
rows=[]
for ym in months:
 u=f'{BASE}/monthly/premiumIndexKlines/{SYMBOL}/5m/{SYMBOL}-5m-{ym}.zip'
 with urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'tst-premium-diagnostic/1.0'}),timeout=30) as r:raw=r.read()
 with zipfile.ZipFile(io.BytesIO(raw)) as z:b=z.read(z.namelist()[0])
 df=pd.read_csv(io.BytesIO(b),header=None);df.columns=COLS
 t=pd.to_numeric(df.open_time,errors='coerce');unit='us' if float(t.dropna().median())>1e14 else 'ms';ts=pd.to_datetime(t,unit=unit,utc=True,errors='coerce').dropna()
 rows.append({'month':ym,'n':len(ts),'first':str(ts.min()),'last':str(ts.max())})
allts=[]
for ym in months:
 u=f'{BASE}/monthly/premiumIndexKlines/{SYMBOL}/5m/{SYMBOL}-5m-{ym}.zip'
 with urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'tst-premium-diagnostic/1.0'}),timeout=30) as r:raw=r.read()
 with zipfile.ZipFile(io.BytesIO(raw)) as z:b=z.read(z.namelist()[0])
 df=pd.read_csv(io.BytesIO(b),header=None);t=pd.to_numeric(df.iloc[:,0],errors='coerce');unit='us' if float(t.dropna().median())>1e14 else 'ms';allts.extend(pd.to_datetime(t,unit=unit,utc=True,errors='coerce').dropna().tolist())
grid=pd.date_range(START,END-pd.Timedelta(minutes=5),freq='5min');present=pd.DatetimeIndex(allts).drop_duplicates();missing=grid.difference(present)
byday=pd.Series(1,index=missing).groupby(missing.date).sum().sort_values(ascending=False)
print(json.dumps({'monthly':rows,'expected':len(grid),'presentInWindow':int(grid.isin(present).sum()),'missing':len(missing),'missingFirst':str(missing.min()) if len(missing) else None,'missingLast':str(missing.max()) if len(missing) else None,'missingByDay':{str(k):int(v) for k,v in byday.items()}},indent=2))
