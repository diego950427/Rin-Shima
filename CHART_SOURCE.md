# 大類學分圖

缺失線依使用者要求採淡紅色。灰線代表實際主修採計配置；每條 G 缺口線代表一待補學分（末條可為小數），不是虛構課程。只有主修各類門檻加總等於總門檻，且配置與缺口加總也一致時，才顯示完整分區。hover/focus 依分類聚焦並在下方列出該類課程配置；缺口始終與實際課程分開標示。

目前總圖依使用者新提供的參考改為 L5 Radial Convergence，來源 lupi-gallery.html / 48 requests pull toward five themes。外圈每點代表一筆課程至分類的真實主修 EXCLUSIVE 配置，內圈為分類。曲線沿用原版 .42/.3 控制點、分類標籤虛線、外圈旋轉代碼；不沿用示例隨機分群。配置加總須與主修總學分一致才出圖，不填充虛構節點。L12 同樣能顯示歸屬但不是使用者指定的圓形；F4 僅構成比例，已被此圖取代。

新增總學分圓環採 F4 Tick Donut（basics-gallery.html / Where the traffic comes from），保留百刻度圓形分段與中心讀數。F11 是單值半圓，L14 是方形點陣，皆不符合使用者指定可互動分區的大圓。圓環只有「已採計／尚缺」兩區，資料來自原版已驗證的 primary aggregate dataset，排除雙重採計，不以大類 effective credits 加總替代。

使用 lieflat-charts，Lupi Basics F5 Tick Rows，來源為 templates/basics-gallery.html 的 Six teams, shipped and counted 卡片及 tickrows 渲染區塊。

候選：L15 Ballot Tally 適合獨立百分比，但此處要直接讀取學分；F11 Tick Gauge 僅適合單一指標；F5 可容納各類長名稱，並維持一刻度一學分，因此採用 F5。其餘 Editorial 時間、關係、構成及分布模板不適用。未使用 Glance。

保留 guide line、逐刻度隊列、每五刻度圓點、列延遲與滾入顯示。使用目前網站已指定的近白、深灰色系與字型，數據只來自原版審核快照；各區不可加總當作畢業總學分。UNKNOWN 明示待確認，不以比例製造通過。

lieflat-charts 採 PolyForm Noncommercial 1.0.0；本次是個人非商業學分工具。若轉商業使用需另行確認授權。
