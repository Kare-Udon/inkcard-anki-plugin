[中文](README.md) | [English](README.en.md)

# InkCards Anki Add-on

Sync text and image cards and review records between Anki Desktop and [Xteink X4](https://www.xteink.cn/) devices.

Current version: `0.2.0-preview.20`, paired with **InkCards device `0.2.77` (API 2.6, source baseline 0.2.76 / r8.3)**. The add-on and device use separate version numbers.

The bundled image converter targets macOS Apple Silicon / CPython 3.13. Image support on Windows, Linux, Intel Mac, or another Python ABI is unverified.

This is a pre-release. The device-side `descriptor json_encode` sync failure remains under investigation; 0.2.76 adds diagnostics and does not claim to fix it. API 0.8 device data and pending reviews are not migrated automatically to the new file storage. Upload pending reviews and verify migration before upgrading.

Supported Anki versions: `26.08.1` and `26.9.3`.

## Installation

1. Download the `.ankiaddon` package from [Releases](https://github.com/Kare-Udon/inkcard-anki-plugin/releases).
2. In Anki, open **Tools → Add-ons → Install from file**, select the package, and restart Anki.
3. Open the profile you want to sync, then select **Tools → InkCards…**.
4. Configure the computer and X4 connection using the **LAN access** steps below.
5. Add a named device in **Device management**, then enter its one-time fixed pairing code on X4.
6. Tap **Sync** on X4. The add-on checks and prepares the data automatically. Keep Anki and the target profile open; the InkCards window may be closed.

## Deck sync

All decks participate by default, including newly created decks. Check the **Exclude** column to stop syncing a deck. Excluding a parent also excludes its current and future descendants. Rules are saved by deck ID, survive renaming, and are shared by devices using the same Anki profile.

Changes are saved automatically and take effect on the next sync. An active sync keeps its original scope. Failed saves restore the previous setting; damaged policy files block sync. Search and **Excluded only** change the view, not the sync scope. Parent card counts include descendants and must not be added to child counts.

The add-on confirms outstanding reviews before preparing the current scope and transferring changed files. Excluding a deck removes only the device copy after a successful sync; Anki cards remain intact. Failure or cancellation preserves the previous device library. Excluding every deck produces an empty device library. Capacity and unsupported-content errors require a clear corrective action rather than silently reporting a partial library as complete.

Device management is available from the header. LAN access, logging, and recovery are in **Settings**. Manual preflight, snapshot preparation, and package generation are no longer part of normal use.

## Device management

The device list shows each name, whether it is pending, bound, or ready for rebinding, and its last connection time. Removed devices are hidden by default; enable **Show removed** to view them. When adding a device, enter a name. The add-on displays a 12-digit fixed pairing code grouped as `4-4-4`. It does not expire automatically, and is shown only when created or manually reset, so save it at that time. If it is lost, select the device and reset its code.

A bound slot cannot be claimed directly by another device. To replace a device, select its slot, choose **Allow rebinding**, then pair the new device with that slot's code. Resetting a code invalidates the old code and unfinished pairing attempts without disconnecting the currently bound device. Removing a device revokes its credentials and session while retaining existing sync and review history. Device management is available only in the local Anki interface.

## LAN access

1. Connect the computer and X4 to the same LAN. Find the computer's LAN IP in its system network settings, for example `192.168.1.10`.
2. In **Tools → InkCards… → Settings**, enable LAN access. This changes the listening address from `127.0.0.1` to `0.0.0.0`, allowing connections through the computer's LAN address. Restart Anki to apply the change.
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

## Add-on logging

The default log level is **Warning and errors**, and console output is off. In the InkCards window, choose Off, Errors, Warning and errors, Normal, or Detailed, and toggle console output independently. Changes take effect immediately. If Anki cannot save the setting, the controls return to the previous values.

All add-on diagnostics share `diagnostics/plugin.jsonl` under the active profile, capped at 128 KiB with one rotating backup. Successful HTTP requests use info, rejected requests use warn, and server errors and exceptions use error. Debug adds fixed stages and redacted source locations.

Logs never include credentials, request bodies, card contents, profile/device/session IDs, raw URLs or queries, or exception messages. Turning logs off does not disable scoring, synchronization receipts, or recovery ledgers.

## Development

Environment: Python 3.10+ and one of the supported Anki Desktop versions above.

Build the package from the source root:

```sh
python3 tools/package_ankiaddon.py
```

Output: `dist/InkCards-Anki-Bridge-0.2.0-preview.20.ankiaddon`. Install it in Anki, restart, and open the target profile. Launch the interface through **Tools → InkCards…**. After editing the source, rebuild, reinstall, and restart Anki.

Run packaging, pairing authentication, storage, and add-on UI tests:

```sh
PYTHONPATH=. python3 -m unittest tests.test_package_ankiaddon tests.test_device_auth tests.test_storage tests.test_plugin_ui tests.test_ui_presenter tests.test_ui_device_management tests.test_runtime_device_management -v
```
