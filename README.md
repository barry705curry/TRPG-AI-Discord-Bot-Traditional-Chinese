# 🎲 TRPG 遊戲主神機器人 (AI Game Master Bot)

這是一款基於 Python 與大語言模型（Gemini API）開發的 Discord 跑團輔助機器人。
透過精心設計的 Prompt 工程與狀態管理架構，解決了傳統 LLM 在複雜遊戲規則下容易產生幻覺（Hallucination）與邏輯脫節的問題。

## ✨ 核心亮點 (Core Features)

* **三階段 Multi-Agent 工作流**：將 AI 的任務嚴格拆分為「行動解析(判斷器)」、「劇情推進(說書人)」與「記憶萃取(紀錄者)」，確保輸出品質與 JSON 格式穩定。
* **純 Python 獨立裁判系統**：針對 LLM 機率運算不穩定的缺陷，將「數值檢定與擲骰」剝離，交由 Python 程式碼強制運算後再將絕對結果回傳給 AI，達成 100% 規則服從。
* **動態 JSON 狀態管理**：使用 JSON 檔案作為輕量級資料庫（儲存玩家面板、圖鑑、世界觀設定），並實作深層字典合併（Deep Merge）來管理非結構化資料。
* **高可用性設計**：導入 `asyncio` 處理 Discord 高併發訊息，並利用 `tenacity` 實作 API 呼叫的指數退避重試機制（Exponential Backoff）。

## 🛠️ 技術棧 (Tech Stack)

* **語言**: Python 3
* **核心套件**: `discord.py`, `google-genai`, `asyncio`, `aiofiles`, `tenacity`
* **資料儲存**: JSON

## 🚀 如何在本機運行 (Installation & Setup)

1. **Clone 專案**
   ```bash
   git clone https://github.com/barry705curry/TRPG-AI-Discord-Bot-Traditional-Chinese.git
   cd TRPG-AI-Discord-Bot-Traditional-Chinese
   ```
2. **安裝依賴套件**
   ```python
   pip install -r requirements.txt
   ```
3. **環境變數設定**
   請將專案根目錄下的 .env.example 重新命名為 .env，並填入你自己的 Token：
   ```text
   DISCORD_TOKEN=你的_Discord_Bot_Token
   GEMINI_API_KEY=你的_Gemini_API_Key
   ```
4. **啟動機器人**
   ```python
   python gm_bot.py
   ```
