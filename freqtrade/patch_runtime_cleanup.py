from pathlib import Path

path = Path('/freqtrade/run_ready_bot.sh')
s = path.read_text(encoding='utf-8')

# Railway must never delete or poll Telegram's webhook. Cloudflare owns inbound
# callbacks; Railway only needs the already-configured private chat id for
# outbound validation. This removes the restart race that produced 409/reclaim
# messages and could temporarily steal the webhook from Cloudflare.
start_marker = "    hook = api('getWebhookInfo', {})\n"
end_marker = "    print(resolved)\n"
start = s.find(start_marker)
if start == -1:
    if '[telegram-resolve] Cloudflare webhook preserved' not in s:
        raise SystemExit('runtime cleanup failed: telegram resolver start marker missing')
else:
    end = s.find(end_marker, start)
    if end == -1:
        raise SystemExit('runtime cleanup failed: telegram resolver end marker missing')
    end += len(end_marker)
    replacement = """    hook = api('getWebhookInfo', {})
    hook_url = str((hook.get('result') or {}).get('url') or '')
    if hook_url:
        print('[telegram-resolve] Cloudflare webhook preserved; Railway outbound validation only', file=sys.stderr)
    if not configured:
        print('[telegram-resolve] missing configured private chat id', file=sys.stderr)
    elif configured == bot_id:
        print('[telegram-resolve] configured chat id is the bot id; refusing unsafe auto-resolution', file=sys.stderr)
    print(configured)
"""
    s = s[:start] + replacement + s[end:]

# BUY-only Telegram mode: keep the lightweight shortlist updater, but skip the
# old pre-alert metrics loop entirely. It was still consuming Binance kline
# calls even though send_prealert had already been disabled in the image.
old_loop = "        now = time.time()\n        for m in movers[:16]:\n"
new_loop = "        # BUY-only mode: no pre-alert evaluation or extra kline calls.\n        for m in ():\n"
if old_loop in s:
    s = s.replace(old_loop, new_loop, 1)
elif 'for m in ():' not in s:
    raise SystemExit('runtime cleanup failed: prealert loop marker missing')

for forbidden in ["api('deleteWebhook'", "api('getUpdates'"]:
    if forbidden in s:
        raise SystemExit(f'runtime cleanup failed: forbidden Telegram ownership call remains: {forbidden}')

for required in [
    '[telegram-resolve] Cloudflare webhook preserved; Railway outbound validation only',
    'for m in ():',
    'exec freqtrade trade',
]:
    if required not in s:
        raise SystemExit(f'runtime cleanup failed: missing {required}')

path.write_text(s, encoding='utf-8')
print('[runtime-cleanup] OK Cloudflare-only Telegram ownership + BUY-only scanner loop')
