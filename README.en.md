[中文](README.md) | [English](README.en.md)

# InkCards Anki Add-on

Sync text cards and review records between Anki Desktop and [Xteink X4](https://www.xteink.cn/) devices.

Current version: `0.2.0-preview.10`.

Supported Anki versions: `26.08.1` and `26.9.3`.

## Installation

1. Download the `.ankiaddon` package from [Releases](https://github.com/Kare-Udon/inkcard-anki-plugin/releases).
2. In Anki, open **Tools → Add-ons → Install from file**, select the package, and restart Anki.
3. Open the profile you want to sync, then select **Tools → InkCards…**.
4. Configure the computer and X4 connection using the **LAN access** steps below.
5. Generate a pairing code in the add-on and enter it on X4 to pair.
6. Select decks in the add-on, run preflight, prepare a snapshot, and sync on X4. Keep Anki and the target profile open while syncing.

## LAN access

1. Connect the computer and X4 to the same LAN. Find the computer's LAN IP in its system network settings, for example `192.168.1.10`.
2. In **Tools → InkCards…**, enable LAN access. This changes the listening address from `127.0.0.1` to `0.0.0.0`, allowing connections through the computer's LAN address. Restart Anki to apply the change.
3. Allow incoming TCP connections to Anki on port `8766` from the local network in your system firewall.
4. Enter the computer's actual LAN IP and port in the computer address field on X4, for example `192.168.1.10:8766`.

To configure the address manually, open **Tools → Add-ons**, select InkCards, choose **Config**, and set:

```json
{
  "device_server": {
    "host": "0.0.0.0",
    "port": 8766
  }
}
```

To listen on a specific network interface, set `host` to that interface's actual LAN IP, for example `192.168.1.10`. Save the configuration and restart Anki.

## Development

Environment: Python 3.10+ and one of the supported Anki Desktop versions above.

Build the package from the source root:

```sh
python3 tools/package_ankiaddon.py
```

Output: `dist/InkCards-Anki-Bridge-0.2.0-preview.10.ankiaddon`. Install it in Anki, restart, and open the target profile. Launch the interface through **Tools → InkCards…**. After editing the source, rebuild, reinstall, and restart Anki.

Run packaging, pairing authentication, storage, and add-on UI tests:

```sh
PYTHONPATH=. python3 -m unittest tests.test_package_ankiaddon tests.test_device_auth tests.test_storage tests.test_plugin_ui -v
```
