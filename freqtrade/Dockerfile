FROM freqtradeorg/freqtrade:stable

USER root
WORKDIR /freqtrade

COPY user_data/config_nfi_dryrun.json /freqtrade/user_data/config.json
COPY run_ready_bot.sh /freqtrade/run_ready_bot.sh
RUN chmod +x /freqtrade/run_ready_bot.sh \
    && mkdir -p /freqtrade/user_data/strategies /freqtrade/user_data/logs \
    && chown -R ftuser:ftuser /freqtrade/user_data /freqtrade/run_ready_bot.sh

USER ftuser
ENTRYPOINT ["/bin/bash", "/freqtrade/run_ready_bot.sh"]
