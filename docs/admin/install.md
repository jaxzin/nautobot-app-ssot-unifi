# Install the UniFi SSoT fork

Use Python 3.12, Nautobot 3.2.4, and Nautobot SSoT 4.6.1 for the CI-tested
runtime.

1. Open the fork's [`develop` branch](https://github.com/jaxzin/nautobot-app-ssot-unifi/tree/develop)
   and copy the commit SHA you want to use.
2. Add the fork as a requirement in your Nautobot build. Replace `COMMIT_SHA`
   with the full SHA.

   ```text
   nautobot-ssot-unifi @ https://github.com/jaxzin/nautobot-app-ssot-unifi/archive/COMMIT_SHA.tar.gz
   ```

   If your build already lists `nautobot-ssot-unifi`, replace that requirement
   with the fork's URL. Both distributions use the same Python module.
3. Add `nautobot_ssot` and `nautobot_ssot_unifi` to `PLUGINS` in
   `nautobot_config.py`. Preserve the other entries.
4. Run `nautobot-server post_upgrade` in the updated environment and roll out
   the updated Nautobot web, worker, and scheduler processes.
5. Open Nautobot's Jobs page and confirm the UniFi data source job is listed.

[Configure the controller and run a dry run](../user/app_getting_started.md).
