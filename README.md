# 自製壓制工具箱

給社員用的 Windows 壓制工具，用來取代小丸工具箱。支援硬體編碼（NVIDIA NVENC、Intel QSV、AMD AMF），也可以調整編碼參數。

可以做三件事：

| 任務 | 內容 |
|---|---|
| **燒錄字幕** | 影片 + `.ass`（含 karaoke 特效字幕）→ H.264 / H.265 + AAC |
| **只轉音訊** | 視訊不動，音訊轉成 AAC（例：FLAC、Opus → AAC） |
| **一般轉檔** | 任意影片 → H.264 / H.265 + AAC；已經相容的串流直接複製 |

> 目前還沒打包成 exe，要用 Python 執行，請見下方的「[開發者](#開發者)」。

---

## 準備

1. 準備 ffmpeg：建議用 [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) 的 **full build**，它內建 libass、nvenc、qsv、amf。
2. 把 ffmpeg 的 `bin` 資料夾內容整份複製到 `bin\ffmpeg\`。如果是 shared 版，`.dll` 也要一起複製：
   ```
   fftoolbox\
   └─ bin\
      ├─ ffmpeg\           ← 必要
      │   ├─ ffmpeg.exe
      │   └─ ffprobe.exe
      └─ ffmpeg_compat\    ← 選用：舊版 ffmpeg，給驅動比較舊的 NVIDIA 顯示卡用
          ├─ ffmpeg.exe
          └─ ffprobe.exe
   ```
   程式找 ffmpeg 的順序：
   1. 環境變數 `FFTOOLBOX_FFMPEG_DIR` 指定的資料夾
   2. 程式附帶的 `bin\ffmpeg\`
   3. 系統 PATH 上的 ffmpeg（`ffmpeg.exe` 與 `ffprobe.exe` 要在同一個資料夾）

   所以電腦裡如果已經有裝 ffmpeg（例如用 winget 安裝），不放進 `bin\ffmpeg\` 也能用。不過 PATH 上的版本不一定有 libass 或硬體編碼，建議還是附帶 gyan.dev full build。目前用的是哪一份 ffmpeg，會顯示在下方日誌和「詳細」報告裡。

   相容版只會在 `bin\ffmpeg_compat\`（或環境變數 `FFTOOLBOX_FFMPEG_COMPAT_DIR`）找，不會去 PATH 找。

---

## 使用方式

### 基本流程

1. 上方選擇任務：燒錄字幕／只轉音訊／一般轉檔。
2. 把影片**拖進視窗**，或按「加入檔案…」。
3. 調整右側參數。
4. 按「**開始**」，佇列裡的檔案會一個一個處理。

右側的參數在按下「開始」時統一套用到整個佇列；已完成的項目不會重跑。按「停止」會中斷目前的檔案並停止整個佇列，沒做完的輸出檔會自動刪除。

### 拖放規則

| 拖進來的東西 | 效果 |
|---|---|
| 影片 | 加入佇列 |
| `.ass` / `.ssa` | 設為字幕檔，並切換到「燒錄字幕」 |
| 裡面只有字型的資料夾 | 設為字型資料夾（fontsdir） |
| 含有影片的資料夾 | 把資料夾裡的影片加入佇列 |

如果是燒錄字幕任務、字幕欄還空著，加入影片時會自動帶入同資料夾、同檔名的 `.ass`。

### 輸出

- 預設輸出到來源檔所在的資料夾，檔名後面加 `_sub`（燒錄字幕）、`_aac`（只轉音訊）或 `_enc`（一般轉檔）。也可以改成統一輸出到指定資料夾。
- 檔名重複時會自動加上 `(2)`、`(3)`…，不會覆蓋既有檔案。
- 容器可選 mp4 或 mkv；只轉音訊時沿用來源的容器，mkv 裡的字幕軌與字型附件會保留。來源容器放不了 AAC 時（webm、ogv、avi…）改輸出成 mkv，純音訊檔（flac、wav…）改輸出成 m4a。

在佇列項目上按右鍵，可以開啟來源或輸出資料夾、查看錯誤訊息、重設為等待。

---

## 編碼器

### 自動偵測

ffmpeg 有編譯進某個編碼器，不代表這台電腦能用（例如沒有 NVIDIA 顯示卡就不能用 NVENC）。所以第一次啟動時，程式會實際用每個編碼器跑一次很短的測試編碼：

- 候選編碼器：`h264_nvenc` `hevc_nvenc` `h264_qsv` `hevc_qsv` `h264_amf` `hevc_amf` `libx264` `libx265`
- HEVC 編碼器會另外測試能不能輸出 10bit
- 「自動」的選擇順序：**NVENC > QSV > AMF > 軟體編碼**

偵測結果會快取起來。換了 ffmpeg、換電腦或 NVIDIA 驅動版本變了，會自動重新偵測；也可以手動按「重新偵測」。

編碼器下拉選單中，不能用的項目會反灰，滑鼠移過去可以看到原因。按「詳細」可以看完整的偵測報告，回報問題時請附上這份報告。

### NVIDIA 驅動太舊

新版 ffmpeg 需要新版的 NVIDIA 驅動，驅動太舊時會出現 `Driver does not support the required nvenc API version`。這時：

1. 如果有放相容版 ffmpeg（`bin\ffmpeg_compat\`），程式會自動改用它，編碼器旁會標示 `[相容版]`。
2. 介面會顯示目前的驅動版本和所需的版本，例如「目前驅動 551.23，更新 NVIDIA 驅動至 570.0 以上可使用新版 ffmpeg」。

### 參數

不同編碼器的參數名稱不一樣，介面只有一套，程式會自動換算：

| 介面參數 | NVENC | QSV | AMF | x264 / x265 |
|---|---|---|---|---|
| 品質（越小越好） | `-rc vbr -cq N -b:v 0` | `-global_quality N` | `-rc cqp -qp_i N -qp_p N` | `-crf N` |
| 速度 1（快）～ 7（慢） | `-preset p1`～`p7` | `veryfast`～`veryslow` | `speed` / `balanced` / `quality` | `veryfast`～`veryslow` |

進階參數：

- **碼率上限**：例如 `20M`；緩衝留空時自動設為上限的 2 倍。QSV、AMF 在品質模式下不支援碼率上限，會被忽略。
- **10bit 輸出**：只有 H.265 可以用，而且要該編碼器支援。
- **GOP**：關鍵影格間隔，留「預設」就好。
- **額外參數**：原樣加在視訊參數的最後面，例如 `-tune animation`。

音訊統一轉成 AAC，預設 256k；取樣率和聲道數可以保持原始，也可以指定。

### 直接複製還是重新編碼

視訊和音訊各有一個「處理」選單，預設是「自動」：

| | 自動的規則 |
|---|---|
| 視訊（只有一般轉檔） | 來源是 H.264 / H.265 → 直接複製；其他格式（AV1、VP9…）→ 重新編碼 |
| 音訊（一般轉檔、燒錄字幕） | 來源是 AAC → 直接複製；其他格式 → 轉成 AAC。有多條音軌時逐軌判斷 |

- 燒錄字幕的視訊**一定**要重新編碼。
- 只轉音訊的音訊**一定**會轉檔。
- 有指定取樣率或聲道數時，音訊一定會重新編碼。
- 來源是 H.264 10bit（Hi10P）或 4:2:2 / 4:4:4 這類相容性較差的格式時，一樣會直接複製，但會跳出提醒。
- ⚠ 想把 H.264 重新壓小，或改轉成 H.265 時，要把視訊處理改成「**重新編碼**」，否則「自動」會直接複製，等於沒有轉。

選取佇列中的檔案時，「來源資訊」下方會列出每條串流打算怎麼處理。

---

## 字幕

### 字型檢查

選好 `.ass` 之後，程式會自動列出字幕**實際用到**的字型，並檢查電腦裡有沒有：

- 判斷時會考慮行內的 `\fn` 與 `\r`。樣式字型如果被 `\fn` 整段蓋掉，就不算用到。
- karaoke template 的 `Comment` 行，和 `\p` 繪圖都不算用到字型。
- 比對範圍包含系統字型、使用者自己安裝的字型、字型資料夾，以及 ASS 內嵌的 `[Fonts]`。中文、日文的字型名稱也比對得到。

缺字型時會以紅字警告，按「開始」前也會再確認一次。缺少的字型會被 libass 用別的字型代替，畫面會跟預期的不同。安裝字型後按「重新檢查」即可，不用重開程式。

> 字型資料夾裡的字型要直接放在那一層，libass 不會讀子資料夾。

### 預覽

輸入時間點（例：`1:23.5`）後按「預覽截圖」，就能看到這一格燒上字幕後的樣子，正式壓制前先確認字型和位置。

### 字幕檔編碼

Big5、GBK、Shift-JIS、UTF-16 編碼的 ASS 都可以直接用，程式會自動轉成 UTF-8 再交給 ffmpeg。

> 字幕特效很重時，速度的瓶頸是在 CPU 上渲染字幕的 libass，不是編碼器，所以換成硬體編碼也不一定會變快。

---

## 預設檔

上方的「預設檔」可以把目前的編碼參數存起來：

- **儲存／刪除**：存在自己的電腦上。
- **匯出／匯入**：存成 `.json` 檔，可以分享給其他社員。

建議分享出去的預設檔把編碼器設成「自動」，因為每個人的顯示卡不同。如果匯入的預設檔指定了你的電腦不能用的編碼器，程式會自動改成「自動」。

---

## 問題排除

| 狀況 | 處理方式 |
|---|---|
| 啟動時說找不到 ffmpeg | 確認 `bin\ffmpeg\ffmpeg.exe` 存在（shared 版要連 `.dll` 一起放進去），或把含有 `ffmpeg.exe` 與 `ffprobe.exe` 的資料夾加到系統 PATH |
| NVENC / AMF 顯示不可用 | 按「詳細」看原因。`Cannot load nvcuda.dll`、`amfrt64.dll failed to open` 表示沒有對應的顯示卡或驅動 |
| 壓制失敗 | 下方日誌會顯示 ffmpeg 的錯誤；按「複製」可以複製完整指令，貼到命令列自己跑或回報問題 |
| 字幕字型不對 | 看字型檢查有沒有紅字；把字型放進一個資料夾，再指定為「字型資料夾」 |
| 偵測結果怪怪的 | 按「重新偵測」 |

設定檔與快取存在 `%APPDATA%\fftoolbox\`：

- `config.json`：偵測結果、預設檔、上次的設定
- `font_cache.json`：字型名稱快取

刪掉這兩個檔案就會恢復成初始狀態。

---

## 開發者

### 環境

需要 Python 3.13（3.12 以上應該都可以）：

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe main.py
```

開發時如果不想把 ffmpeg 複製進 `bin\`，可以直接用系統 PATH 上的 ffmpeg，或用環境變數指定 ffmpeg 所在的資料夾：

```
$env:FFTOOLBOX_FFMPEG_DIR = "C:\ffmpeg\bin"
$env:FFTOOLBOX_FFMPEG_COMPAT_DIR = "C:\ffmpeg-old\bin"   # 選用
```

### 測試

```
.venv\Scripts\python.exe -m pytest tests
```

需要實際執行 ffmpeg 的整合測試，在找不到 ffmpeg 時會自動跳過。

### CLI

每個核心模組都可以單獨在命令列執行，方便除錯：

```
python -m fftoolbox.encoders            # 偵測結果（有快取就用快取）
python -m fftoolbox.encoders --force    # 重新偵測
python -m fftoolbox.encoders --args     # 各編碼器在預設參數下的指令片段

python -m fftoolbox.ass_fonts 字幕.ass [--fontsdir 資料夾]

python -m fftoolbox.job burn 影片.mp4 --ass 字幕.ass [--fontsdir fonts] [-o out.mp4]
python -m fftoolbox.job audio 影片.mkv
python -m fftoolbox.job transcode 影片.mkv --vmode encode --codec hevc --quality 24
python -m fftoolbox.job preview 影片.mp4 --ass 字幕.ass -t 0:44.5 -o shot.png
python -m fftoolbox.job transcode 影片.mkv --dry-run   # 只印出指令
```

### 程式結構

```
main.py                   進入點（之後 PyInstaller 從這裡打包）
fftoolbox/
  paths.py                程式路徑、附帶 ffmpeg 的位置、執行子程序
  config.py               config.json 讀寫
  encoders.py             編碼器偵測、介面參數 → 各編碼器參數（要加 AV1 改這裡）
  job.py                  組 ffmpeg 指令、決定串流要複製或編碼、執行、解析進度、預覽截圖
  ass_fonts.py            ASS 解析、讀取字型名稱表、字型檢查
  probe.py                ffprobe 來源資訊
  presets.py              預設檔存取、匯出、匯入
  gui/
    main_window.py        主視窗
    dialogs.py            預覽視窗、文字視窗
    workers.py            背景執行緒（偵測、佇列、ffprobe、字型檢查）
tests/                    pytest
```

核心模組（`encoders`、`job`、`ass_fonts`、`probe`）不依賴 Qt，CLI 和 GUI 共用同一套程式碼。

### 尚未完成

- PyInstaller 打包，以及在沒裝 Python 的電腦上測試
- NVENC、AMF、相容版 ffmpeg 的切換，還沒在實機上驗證過（只用模擬資料測過邏輯）
