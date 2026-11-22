# Installing Tastebuds

No Python, no terminal. About five minutes, most of it the one-time security
step below. Your ratings stay on your own computer.

Get the download from the
[latest release](https://github.com/VladGiurgiu0/movies/releases/latest):

- **Mac** with Apple Silicon (M1 or newer): the file ending in `mac-AppleSilicon.dmg`
- **Windows 10 or 11:** the file starting with `TastebudsSetup`

Why the warning? Tastebuds is a free hobby project, and its downloads aren't
signed with a paid Apple or Microsoft certificate. So the first time you open it,
your computer asks you to confirm that you trust it. After that, it opens
like any other app.

---

## Mac

1. Double-click the `.dmg` you downloaded.
2. Drag **Tastebuds** onto the **Applications** folder in the window that opens.
3. Open **Applications** and double-click **Tastebuds**.
4. A message says Apple could not verify Tastebuds. Click **Done**.
5. Open **System Settings** and go to **Privacy & Security**.
6. Scroll down. Next to "Tastebuds was blocked", click **Open Anyway**.
7. Enter your Mac password if asked, then click **Open**.

You only do steps 4 to 7 once.

On an older macOS (14 Sonoma or earlier) there is a shortcut instead of steps
4 to 7: right-click **Tastebuds** in Applications, choose **Open**, then click
**Open** again.

If the Mac says the app "is damaged and can't be opened": open the
**Terminal** app, paste this line, press Return, and open Tastebuds again.

```
xattr -dr com.apple.quarantine /Applications/Tastebuds.app
```

## Windows

1. Your browser may say the file "isn't commonly downloaded". Choose **Keep**
   (in Edge: click **...** next to the download, then **Keep**, then
   **Show more** and **Keep anyway**).
2. Double-click `TastebudsSetup-....exe`.
3. A blue box says "Windows protected your PC". Click **More info**, then
   **Run anyway**.
4. Click through the installer. It installs for you only, so it doesn't ask
   for an administrator password.
5. Leave **Launch Tastebuds** ticked and click **Finish**.

Next time, start it from the Start menu (or the desktop icon, if you chose one).

---

## First launch

1. Click **Get started**.
2. Paste the **TMDb key** your friend sent you, and click **Continue**.
   (No key? The link under the box shows how to get your own for free.)
3. Search for a film you love. Tastebuds builds your first channel of similar
   films from it.
4. Start rating: **3** liked, **2** okay, **1** disliked, **0** haven't seen it.

## Good to know

- **Your data** is plain files in a folder called `Tastebuds` in your home
  folder. **About → Open data folder** shows it. Back it up like any folder.
- **Updating:** download the new version and install it the same way. Your
  ratings stay where they are.
- **Uninstalling:** Mac, drag Tastebuds from Applications to the Trash.
  Windows, **Settings → Apps → Tastebuds → Uninstall**. Neither deletes your
  ratings folder.
- **On your phone:** click **Pair a phone**, then **Turn on pairing**, and scan
  the code. If your computer asks whether Tastebuds may accept incoming
  connections, choose **Allow**. Phone and computer must be on the same Wi-Fi.
