# 自製壓制工具箱 — 規格

## 目標
Windows 桌面工具，給社員在各種不同電腦上使用。取代小丸工具箱，支援硬體編碼與可調參數。

## 三種任務
1. **燒錄 ASS 字幕**（重點：karaoke 特效字幕）：影片 + .ass → H.264/H.265 + AAC
2. **只轉音訊**：影片視訊流不動（`-c:v copy`），音訊轉 AAC（例：FLAC → AAC）
3. **一般轉檔**：任意輸入 → H.264/H.265 + AAC

## 技術選擇
- Python 3.12 + PySide6，最後用 PyInstaller 打包成單一資料夾
- **隨程式附帶 ffmpeg/ffprobe**（gyan.dev full build，含 libass、nvenc、qsv、amf），不依賴系統 PATH
- 呼叫 ffmpeg CLI（subprocess），用 `-progress pipe:1` 解析進度

## 硬體編碼偵測（最重要）
- `ffmpeg -encoders` 只代表「有編譯進去」，不代表能用 → 必須**實測**
- 對每個候選編碼器跑一次短測試：
  `ffmpeg -f lavfi -i color=black:s=640x360:d=0.2 -c:v <encoder> -f null -`
  回傳碼 0 = 可用；失敗則保留 stderr 最後幾行當「不可用原因」
- 候選：`h264_nvenc` `hevc_nvenc` `h264_qsv` `hevc_qsv` `h264_amf` `hevc_amf` `libx264` `libx265`
- 另外用 `-pix_fmt p010le`（或 yuv420p10le）測 HEVC 10bit 是否支援
- 自動選擇優先序：NVENC > QSV > AMF > 軟體編碼
- **驅動過舊的對策**：新版 ffmpeg 綁新版 NVENC SDK，舊驅動會報
  `Driver does not support the required nvenc API version`，stderr 會寫出最低需求驅動版本
  - 附帶兩份 ffmpeg：新版（主要）+ 舊 SDK 版（相容用）
  - 新版 nvenc 測試失敗且訊息含 API version → 改用相容版再測；兩者都失敗才判定不可用
  - 用 `nvidia-smi --query-gpu=driver_version --format=csv,noheader` 讀目前驅動版本，跟錯誤訊息中的需求版本一起顯示，提示「更新驅動至 X 以上可用新版」
  - AMF、QSV 失敗時同樣保留原始錯誤訊息顯示給使用者
- 結果快取到 `config.json`（附偵測時間），介面提供「重新偵測」按鈕
- 手動選擇：下拉選單列出全部編碼器，不可用的反灰並顯示原因

## 統一參數面板（UI 一套，內部對應不同編碼器）
| UI 參數 | nvenc | qsv | amf | x264/x265 |
|---|---|---|---|---|
| 品質值 | `-rc vbr -cq N -b:v 0` | `-global_quality N` | `-rc cqp -qp_i N -qp_p N` | `-crf N` |
| 速度 | `-preset p1..p7` | `-preset veryfast..veryslow` | `-quality speed/balanced/quality` | `-preset` |
- 進階：碼率上限（maxrate/bufsize）、10bit、GOP、自訂額外參數文字框
- 音訊：AAC 碼率（預設 256k）、取樣率、聲道
- 參數對應表寫在獨立模組 `encoders.py`，方便之後擴充 AV1

## 字幕相關
- 濾鏡：`subtitles=<ass>:fontsdir=<fonts>`（Windows 路徑的冒號、反斜線要跳脫）
- 解析 ASS 的 `[V4+ Styles]` 與行內 `\fn`，列出用到的字型，比對系統字型 + fontsdir，缺字型時警告
- 預覽：指定時間點輸出一張帶字幕的截圖（`-ss T -frames:v 1`）
- 註：libass 渲染在 CPU，特效字幕重時瓶頸在這裡，不在編碼器

## 介面
- 拖放加入檔案，批次佇列（逐一執行，可取消）
- ffprobe 顯示來源資訊（解析度、fps、編碼、音訊）
- 預設檔存成 JSON，可匯出/匯入給其他社員
- 進度條 + 剩餘時間 + 執行中的完整 ffmpeg 指令（可複製，方便除錯）

## 開發順序
1. `encoders.py`：偵測 + 參數對應，寫 CLI 測試
2. `job.py`：組 ffmpeg 指令、執行、進度解析，用範例檔測三種任務
3. `ass_fonts.py`：字型檢查
4. GUI
5. PyInstaller 打包，在沒裝 Python 的電腦測試
