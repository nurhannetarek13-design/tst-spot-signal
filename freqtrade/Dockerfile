FROM freqtradeorg/freqtrade:stable

USER root
WORKDIR /freqtrade

COPY user_data/config_nfi_dryrun.json /freqtrade/user_data/config_nfi_dryrun.json
COPY run_ready_bot.sh /freqtrade/run_ready_bot.sh
COPY fast_entry_engine.py /freqtrade/fast_entry_engine.py
COPY entrypoint.sh /freqtrade/entrypoint.sh
RUN sed -i 's/NFI_MAX_PAIRS = 28/NFI_MAX_PAIRS = 32/' /freqtrade/run_ready_bot.sh \
    && sed -i '/from telegram_signal_bridge import send_prealert/a send_prealert = lambda **kwargs: None' /freqtrade/run_ready_bot.sh \
    && chmod +x /freqtrade/run_ready_bot.sh /freqtrade/entrypoint.sh \
    && mkdir -p /freqtrade/user_data/strategies /freqtrade/user_data/logs /freqtrade/user_data/signals \
    && chown -R ftuser:ftuser /freqtrade/user_data /freqtrade/run_ready_bot.sh /freqtrade/entrypoint.sh /freqtrade/fast_entry_engine.py

USER ftuser
EXPOSE 8080
ENTRYPOINT ["/bin/bash", "/freqtrade/entrypoint.sh"]
