"""程式進入點（之後 PyInstaller 也從這裡打包）。"""

from fftoolbox.gui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
