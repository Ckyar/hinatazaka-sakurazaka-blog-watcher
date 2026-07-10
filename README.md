# 日向坂46／櫻坂46 部落格圖片 Discord 通知器

在 Windows 或 Raspberry Pi OS 上以同一個 Discord Bot 並行輪詢日向坂46及櫻坂46官方部落格首頁，直接取得實際存在的文章 ID，將兩團的新文章圖片傳送至各自指定頻道。本機圖片儲存可透過 `.env` 開啟或關閉。

## 功能

- 從官方首頁取得真實文章 ID，天然支援跳號，也不會浪費請求猜測空號。
- 日向坂只擷取 `.l-maincontents--blog .c-blog-article__text`，櫻坂只擷取 `article.post .box-article` 裡的圖片。
- 可選擇將圖片分類儲存在 `images/成員名字/日期_部落格標題_ID/`；停用本機儲存時仍會將官方圖片網址傳送至 Discord。
- 每篇文章把標題、連結及最多 10 張圖片合併在同一則 Discord 訊息；超過 10 張時依 Discord 限制自動分批。
- SQLite 保存掃描進度、已公告文章與已傳送圖片；程式重啟後會續跑且不重複傳送。
- HTTP 暫時錯誤會重試，Discord 失敗時保留原編號供下次再試。
- 記錄檔自動輪替，適合長時間運作。

## 1. 建立 Discord Bot

1. 前往 [Discord Developer Portal](https://discord.com/developers/applications)，建立 Application。
2. 在 **Bot** 頁建立 Bot，複製 Token。Token 等同密碼，不要貼到聊天、截圖或提交 Git。
3. 在 **OAuth2 → URL Generator** 勾選 `bot`，Bot Permissions 至少勾選：
   - View Channels
   - Send Messages
   - Embed Links
   - Read Message History
4. 用產生的網址邀請 Bot 進伺服器。
5. Discord 設定中開啟「開發者模式」，右鍵點目標頻道 →「複製頻道 ID」。

本程式不讀取聊天內容，因此不需要 Message Content Intent。

程式會以兩層方式避免重傳：首先查詢各團獨立的 SQLite；若本地紀錄不完整，再檢查對應 Discord 頻道歷史，只比對 Bot 自己送出的文章與圖片網址。`DISCORD_HISTORY_LIMIT` 預設為 500，可在 `.env` 調整。若沒有 Read Message History 權限，仍會使用本地 SQLite 去重。

## 2. Windows 安裝

先安裝 [Python 3.10 以上版本](https://www.python.org/downloads/windows/)，安裝時勾選 **Add Python to PATH**。

雙擊 `install.bat`。完成後用記事本開啟 `.env`，至少填入：

```dotenv
DISCORD_BOT_TOKEN=你的Token
DISCORD_CHANNEL_ID=日向坂頻道ID
SAKURA_DISCORD_CHANNEL_ID=櫻坂頻道ID
SAVE_IMAGES_LOCALLY=true
```

再雙擊 `start.bat`。看到 `Discord connected` 即表示運作中。

## 本地圖片分類

在 `.env` 選擇是否下載圖片：

```dotenv
# 儲存空間有限的 Raspberry Pi 建議設為 false
SAVE_IMAGES_LOCALLY=false
```

設為 `false` 時不會下載或寫入任何部落格圖片；Discord 通知、圖片 embed、SQLite 去重與輪詢功能皆維持正常。可接受 `true/false`、`yes/no`、`on/off` 或 `1/0`。預設值為 `true`，以保留既有行為。

啟用本機儲存時，兩個團體使用獨立子目錄：

```text
images/
├─ 日向坂46/
│  └─ 高井 俐香/
│     └─ 2026-07-09_鼓動早くなり周りの温度が上がる_70143/
└─ 櫻坂46/
   └─ 的野 美青/
      └─ 2026-07-07_無題_70124/
```

可分別使用 `IMAGE_DIR` 與 `SAKURA_IMAGE_DIR` 改變儲存位置。Windows 不允許的檔名字元會自動替換成底線，過長標題也會安全截短；下載會先寫入 `.part` 暫存檔，完成後才改成正式檔名。重新掃描時，已完整存在的圖片不會再次下載。

## 首頁輪詢與跳號策略

程式預設每 15 秒分別讀取日向坂及櫻坂首頁，兩個工作錯開 5 秒，從首頁連結直接擷取最新文章 ID。只有該團 SQLite 尚未標記為完整處理的文章，才會再讀取 detail 頁、依設定下載圖片並通知 Discord。

任何團體第一次啟用時，都會將當下首頁文章視為 pending 並依發布順序處理；之後新出現在首頁的文章也會持續通知。程式不使用 ID 大小判斷新舊，因此即使網站稍後公開一篇較小 ID 的文章仍會被偵測。

每次輪詢會在終端顯示：

```text
日向坂46 homepage poll found 12 article(s); 0 pending
櫻坂46 homepage poll found 12 article(s); 0 pending
```

可用 `CHECK_INTERVAL_SECONDS` 和 `SAKURA_CHECK_INTERVAL_SECONDS` 分別調整輪詢秒數；預設值皆為 15。

## 24 小時自動啟動（工作排程器）

1. `Win + R`，輸入 `taskschd.msc`。
2. 選「建立工作」而非「建立基本工作」。
3. 「一般」：勾選「不論使用者登入與否均執行」及「以最高權限執行」。
4. 「觸發程序」：新增「啟動時」。
5. 「動作」：
   - 程式或指令碼：`C:\Windows\System32\cmd.exe`
   - 新增引數：`/c "你的專案完整路徑\start.bat"`
   - 開始位置：填入本資料夾的完整路徑。
6. 「條件」：若是筆電，可取消「只有在使用 AC 電源時才啟動」。
7. 「設定」：勾選「如果工作失敗，每隔 1 分鐘重新啟動」，並將「如果工作執行時間超過以下時間則停止」取消。

電腦必須保持開機、不能進入睡眠，程式才可能 24 小時運作。

## Raspberry Pi OS 24 小時部署

建議使用 Raspberry Pi OS 64-bit（Bookworm 或更新版本）。Bookworm 之後第三方 Python 套件應安裝在 virtual environment，本專案的安裝腳本會自動建立 `.venv`。

### 第一次部署

在 Raspberry Pi 終端執行：

```bash
sudo apt-get update
sudo apt-get install -y git
git clone https://github.com/YOUR_ACCOUNT/YOUR_REPOSITORY.git
cd YOUR_REPOSITORY
chmod +x install_raspberry_pi.sh update_raspberry_pi.sh status_raspberry_pi.sh
./install_raspberry_pi.sh
```

第一次執行會建立 `.env` 並停止。編輯設定：

```bash
nano .env
```

至少填入 Discord Token 與兩個頻道 ID。Raspberry Pi 儲存空間有限時，同時設定：

```dotenv
SAVE_IMAGES_LOCALLY=false
```

儲存 `.env` 後再次執行：

```bash
./install_raspberry_pi.sh
```

安裝器會建立並啟用 `sakamichi-blog-watcher.service`。服務具有以下行為：

- Raspberry Pi 開機後自動啟動。
- 程式異常退出後等待 15 秒自動重啟。
- 等待網路連線後啟動。
- 以目前使用者執行，不使用 root 執行 Python。
- 只允許寫入專案的 `data`、`images`、`logs`。

### 狀態與日誌

```bash
./status_raspberry_pi.sh
sudo systemctl status sakamichi-blog-watcher --no-pager
journalctl -u sakamichi-blog-watcher -f
tail -f logs/watcher.log
```

停止、啟動及重新啟動：

```bash
sudo systemctl stop sakamichi-blog-watcher
sudo systemctl start sakamichi-blog-watcher
sudo systemctl restart sakamichi-blog-watcher
```

### 從 GitHub 更新

```bash
cd ~/YOUR_REPOSITORY
./update_raspberry_pi.sh
```

更新腳本會使用 `git pull --ff-only`、同步 Python 相依套件、執行測試，成功後重新啟動服務。

### SD 卡與低功耗注意事項

- `STATUS_LOG_INTERVAL_SECONDS=300` 會讓無新文章的狀態每 5 分鐘才寫入一次，減少 SD 卡寫入。
- `SAVE_IMAGES_LOCALLY=false` 會完全略過圖片下載，僅保留容量很小的 SQLite 狀態與輪替日誌。
- 日誌最多約 20 MB，SQLite 只在狀態變更時寫入。
- 使用穩定的 Raspberry Pi 4 電源，並避免讓系統因供電不足反覆重啟。
- `.env`、下載圖片、SQLite 與 logs 都被 `.gitignore` 排除，不會上傳 GitHub。

## 維護與疑難排解

- 執行記錄：`logs/watcher.log`
- 日向坂去重資料：`data/watcher.db`
- 櫻坂去重資料：`data/sakura_watcher.db`
- 日向坂圖片（僅 `SAVE_IMAGES_LOCALLY=true`）：`images/日向坂46/成員名字/日期_部落格標題_ID/`
- 櫻坂圖片（僅 `SAVE_IMAGES_LOCALLY=true`）：`images/櫻坂46/成員名字/日期_部落格標題_ID/`
- 要重設單一團體，先關閉程式並備份後，只刪除該團的資料庫。刪除資料庫可能使當下首頁文章重新進入 pending，但 Discord 歷史比對仍會避免重傳已存在的內容。
- `Forbidden (403)`：確認 Bot 在該頻道擁有 View Channel、Send Messages、Embed Links、Read Message History。
- 修改 `.env` 後需要重新啟動 `start.bat`。

## 測試

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
```
