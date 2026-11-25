# Rob Run Soundboard

A Windows soundboard that plays sounds **through your microphone** into game voice chat (made for Rob Run, works with any game or app that lets you pick a mic).

Your voice and the sounds get mixed and sent into a virtual cable, and the game uses that cable as its microphone. You hear the sounds in your own headphones too.

## Features

- Boards: folders of sounds you can browse, create and switch between
- Hotkeys: F1–F12 automatically, or bind any key, key combo or mouse button 4/5
- Hotkeys work while the game is focused, even while you hold W/A/S/D
- In-game HUD overlay showing your sounds and keys (game must be in borderless/windowed fullscreen)
- Loop any sound forever (press its key again to stop)
- Silence at the start and end of clips is trimmed automatically, so loops are seamless
- Volume per sound and overall, up to 1000%
- Drag and drop audio files (wav, mp3, ogg, flac) onto the window to add them

## Setup (Windows)

1. **Install VB-CABLE** (free virtual audio cable): https://vb-audio.com/Cable/ — download, unzip, right-click `VBCABLE_Setup_x64.exe` → *Run as administrator* → *Install Driver*, then restart your PC.
2. **Install Python 3.12** from https://www.python.org/downloads/ — tick **"Add python.exe to PATH"** in the installer.
3. **Download this repo** (green *Code* button → *Download ZIP*), unzip it somewhere permanent, and double-click **`install.bat`**. It installs the packages and puts a *Rob Run Soundboard* shortcut on your desktop.

## Make the game hear it

1. Open the soundboard and check **⚙ Settings**:
   - **Your microphone** → your real mic (headset mic)
   - **Send to game** → `CABLE Input (VB-Audio Virtual Cable)`
   - **Let me hear sounds on** → your headphones
2. In the game, set the microphone to **`CABLE Output (VB-Audio Virtual Cable)`**.
   Rob Run has no mic picker, so set it as the Windows default instead: **Settings → System → Sound → Input → CABLE Output**. Restart the game after changing it.

⚠️ Installing VB-CABLE can make it your default *speaker* too, so you hear nothing. If that happens, go to **Settings → System → Sound → Output** and pick your headphones again.

Check it works: talk and watch the **Mic in** meter, play a sound and watch **To game**.

## Default hotkeys

| Action | Key |
| --- | --- |
| Sounds | F1–F12 (in board order) |
| Stop all sounds | Ctrl+Shift+S |
| Mute/unmute your mic | Ctrl+Shift+M |
| Show/hide HUD | Ctrl+Shift+H |
| Move/unlock HUD | Ctrl+Shift+J |
| Next / previous board | Ctrl+Shift+PgDn / PgUp |

Change any of them in **⌨ Sounds & Keybinds**.

Your boards are saved in `Documents\RobRun Soundboard\Boards`, settings in `%APPDATA%\RobRunSoundboard`.

## Please

Don't use this to blast slurs or harass people — that's a quick way to get reported and banned.
