# Rin-Shima

北市大學分規劃與畢業審核工具，沿用 UT-checker 的日式留白介面。

目前可填寫入學年度、主修、雙主修或輔系設定，上傳 PDF 後查看成績。通識集中在上方，其餘依學期、必選修排列。預設 113 入學；雙主修目標年度獨立選擇。

右上角選單可在「學分」與「查課」之間切換，不會重設本頁資料。查課使用 115-1 博愛校區的既有開課資料，可依課名／教師／課碼、系所、年級、通識／共同選修、星期與節次篩選。此為 2026/09/07 快照，不是即時選課名額；共同選修只採用同學期、校區、課碼與課名完全相符的正式分類。

核對成績後按「確認成績並檢核」，下方顯示分類進度、缺課與總學分互動圖。待確認不等於通過，正式認定以校方為準。數學系專業領域尚未接入，目前會阻擋該主修的審核。

Streamlit 版本會將 PDF 傳到雲端記憶體解析，不寫入成績單檔案、不全域快取個人成績；資料僅用於目前工作階段。重新整理會清除畫面資料。請勿將真實成績單、密碼或部署憑證提交到 GitHub。

## Streamlit 部署

使用 `diego950427/Rin-Shima`、`main` 分支、入口 `streamlit_app.py`。Community Cloud 安裝根目錄的 `requirements.txt`，建議 Python 3.12。本機可執行 `streamlit run streamlit_app.py` 測試相同入口。

現有 HTML 介面透過 Streamlit 自訂元件與 Python 溝通，不依赖 localhost API，也不額外開公開連接埠。元件僅提供公開 UI 資產。

## 本機預覽

在專案目錄安裝 `pip install -r requirements.txt`，執行 `python server.py`，再開啟 http://localhost:8521 。一般靜態伺服器無法解析 PDF。

此服務只供本機預覽，不適合直接公開部署。上線前需補齊部署與上傳安全限制。

## 素材

- 首次載入的山林線描動畫：使用者提供的動畫包，作者 zanina-yassine／Uiverse，MIT 授權見 `assets/mountain-loader-LICENSE.txt`。載入完成即淡出；Streamlit 平台自身的啟動畫面不受本程式控制。

- 視覺參考：使用者提供的 Gallery03・留白插畫／Utsusemi Design。保留作者署名；本專案 HTML、CSS 與互動程式另行撰寫。
- `assets/yuru-camp.gif`：使用者提供的素材，原檔不變。不宣稱擁有角色或插畫著作權，不將它列為開源程式授權素材。
- `core/` 重用原版解析器、分類工具與規則設定，未修改已確認規則。未加入個人成績、帳號或權杖。
- 按鈕動效改編自 Uiverse.io 的 Cevorob；思源宋體字型授權附於 `assets/fonts/SourceHanSerif-LICENSE.txt`。
