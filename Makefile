# Tastebuds build helpers — run these on macOS, from the repo root.
#
#   make app     build Tastebuds.app into dist/  (needs: pip install pyinstaller pywebview numpy)
#   make icns    build assets/icon.icns from the iconset PNGs (macOS iconutil)
#   make dmg     wrap dist/Tastebuds.app into a distributable .dmg (needs: brew install create-dmg)
#   make clean   remove build artifacts
#
# Distribution notes:
# - Unsigned builds: recipients right-click the app -> Open (once) to pass Gatekeeper.
# - Proper distribution: Apple Developer ID, then
#     codesign --deep --force --options runtime -s "Developer ID Application: NAME" dist/Tastebuds.app
#     xcrun notarytool submit Tastebuds.dmg --keychain-profile PROFILE --wait
#     xcrun stapler staple Tastebuds.dmg

.PHONY: app icns dmg clean

app: icns
	cd app && pyinstaller --noconfirm tastebuds.spec --distpath ../dist --workpath ../build

icns:
	@test -d assets/icon.iconset || { echo "assets/icon.iconset missing"; exit 1; }
	iconutil -c icns assets/icon.iconset -o assets/icon.icns

dmg:
	create-dmg --volname "Tastebuds" --app-drop-link 400 120 \
	  --window-size 600 300 --icon "Tastebuds.app" 150 120 \
	  Tastebuds.dmg dist/Tastebuds.app

clean:
	rm -rf build dist app/build app/dist Tastebuds.dmg
