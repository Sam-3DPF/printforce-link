# PrintForce Link

**Connect your Bambu 3D printers to [3D PrintForce](https://3dprintforce.com) over your shop network.**

PrintForce Link is a small agent you run on a computer on the same network as your Bambu
printers. It reads each printer's live status and AMS filament colors, routes sliced files
to an idle printer with the right colors, and reports everything back to 3D PrintForce — so
the app can manage your farm end to end. It's the in-house replacement for SimplyPrint on
Bambu machines.

3D PrintForce runs in the cloud and **can't reach your LAN-only printers** — that's why this
piece runs locally. It talks MQTT/FTPS to the printers on your network and reports
**outbound** to 3D PrintForce over HTTPS (no inbound ports, no port forwarding).

---

## Install (the easy way)

You don't install this from here directly. In **3D PrintForce → Integrations → PrintForce
Link**, click **Get install command** and follow the steps. You'll paste one command into a
Terminal window on a computer that stays on your shop network; it downloads, installs,
starts on login, and pairs itself to your account automatically — nothing to configure.

Then, back in 3D PrintForce, your printers appear as they're found on the network — click
**Add**, type each printer's LAN access code, and you're done.

> **Requirements:** a Mac or PC that stays on and on the same Wi-Fi/LAN as the printers, and
> each printer in **LAN-Only + Developer Mode** (the same mode SimplyPrint needs). Prefer a
> DHCP reservation for each printer on your router — though PrintForce Link also self-heals
> if an address changes.

**Prefer not to install a binary?** You can run it from source — see below.

---

## What it does

- **Live status + AMS colors** for every Bambu printer, reported to 3D PrintForce.
- **Auto-routing** of sliced files (via an OctoPrint-compatible print-host) to an idle
  printer whose loaded colors match the job.
- **Completion + clear-plate** hand-off back to 3D PrintForce.
- **Stays connected.** Printers are tracked by serial, not IP, so a DHCP lease change or a
  reboot self-heals with no action from you.
- **Updates itself** from GitHub Releases.

## Run from source (developers)

```bash
git clone https://github.com/Sam-3DPF/printforce-link.git
cd printforce-link
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.example.toml config.toml          # set dpf_base_url; pair via the app or a token
python -m bridge.app config.toml
```

Run the tests with `pytest`. The Python package is named `bridge` for historical reasons;
the product is PrintForce Link.

## Releases

Tagging `vX.Y.Z` triggers `.github/workflows/release.yml`, which builds the standalone agent
for macOS (Apple Silicon + Intel) and Windows with PyInstaller and publishes the archives +
`SHA256SUMS` to a GitHub Release. The installers and the self-updater download from the
latest release.

## Security

The agent is distributed **unsigned** and open source: the install script is readable here,
and it downloads only checksum-verified release archives over HTTPS. Printer access codes are
delivered to the agent encrypted and then deleted from the cloud; the agent stores them
locally in a `chmod 600` file and never logs them.

## License

[MIT](LICENSE) — © 2026 3D PrintForce.
