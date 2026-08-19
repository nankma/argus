# 跨領域調研：樣本來源過度集中的處理方法

其他領域怎麼處理「我的樣本被單一來源主導」這個問題。本文是
`news-ranking-plan.md` 的姊妹篇，起因是 2026-08-18 發現的來源塌陷問題
（Hacker News 佔文章快取的 33%，卻佔最新 50 篇的 64%），暴露出本專案對
這件事完全沒有任何防禦機制。

本文是 `sample-diversity-survey.md` 的中文版，兩份需同步維護。

**這是調研，不是建議方案。** 目的是呈現有哪些解法型態存在、以及其中哪些
本專案真的用得上。針對本程式碼庫的具體可選方案在 `news-ranking-plan.md`。

## 統整性的觀察

這個問題確實是通用的——調查統計、生態學、金融、基因體學、臨床試驗、
天文學、資訊檢索、機器學習都有成熟的相關文獻。但它們解決的**不是同一個
問題**。它們在**管線的四個不同位置**介入，而你選在哪個位置介入，決定了
事後還能挽回什麼。

| 介入位置 | 回答什麼問題 | 哪些領域在這裡 |
|---|---|---|
| **1. 設計階段** | 一開始就怎麼收集到均衡的樣本？ | 調查統計、臨床試驗、天文學 |
| **2. 度量階段** | 手上的樣本有多集中，能用一個數字表達嗎？ | 生態學、金融、媒體研究 |
| **3. 挑選階段** | 給定一個已經傾斜的池子，怎麼挑出均衡的子集？ | 資訊檢索、推薦系統、機器學習、搜尋引擎 |
| **4. 校正階段** | 樣本已傾斜且無法重收，怎麼還能得到無偏的結論？ | 調查加權、天文學、基因體學 |

**本專案目前四個位置一個都沒做。** 這就是本次的發現，也是為什麼值得在
挑選修法之前先做這份調研。

---

## 一、設計階段——從源頭防止失衡

### 分層抽樣（統計學）

把母體切成互斥的層（strata），然後在**每一層內部**隨機抽樣。無論某層的
自然出現頻率多低，都保證有代表性。這是機率抽樣的黃金標準，因為它支撐
正式的統計推論。

**配額抽樣（quota sampling）** 是它的非機率版本：同樣的分組結構，但每個
配額內部是按可得性挑選而非隨機挑選。快得多也便宜得多；而配額內部的
非隨機挑選正是偏差重新滲入的地方。標準建議是：速度與成本比精確度重要時
用配額抽樣，結果必須支撐正式推論時用分層抽樣。

**對應到本專案**：推送中設定每來源（或每來源類別）的配額——「單一來源
最多 N 篇」。直接類比，而且便宜。

### 區組隨機化與最小化法（臨床試驗）

臨床試驗面對的是更尖銳的版本：受試者是**依序到達**的，你無法等到樣本
收齊之後再來平衡。

- **排列區組隨機化** — 在固定大小的小區組內隨機化，使每個區組內部都
  精確達成預期的分配比例。
- **分層區組隨機化** — 在每個共變量層內部再做區組隨機化。
- **最小化法（minimization）** — 一種**動態**分配程序：每來一位受試者，
  計算把他分到哪一組會讓整體失衡最小，就分到那組。值得注意的是，文獻
  指出最小化法**特別在樣本量小的時候**有明顯優勢——而這正是簡單隨機化
  的平衡保證最弱的情境。

**為什麼這是整份調研中最被低估的類比**：抓取任務本質上就是一個依序到達
的問題。文章一輪一輪進來，我們必須在看不到未來的情況下決定留哪些。
最小化法的核心想法——「貪婪地挑選最能減少當前失衡的那個」——可以直接
實作，而且對串流場景來說**比靜態配額更合適**。

### 體積受限樣本（天文學）

與其事後校正偏差，不如**限縮樣本範圍讓偏差在結構上不可能發生**。流量受限
的巡天會過度取樣本質明亮的天體（Malmquist 偏差，見下），而體積受限樣本
改成「取固定距離內的全部天體」，用較小的樣本量換取無偏。

**可遷移的想法**：有時候乾淨的解法是**收緊納入規則**直到扭曲在結構上不可能
發生，代價是樣本變小。對應到我們就是：「只考慮最近 N 小時內、所有來源
一視同仁的文章」——池子變小，但沒有頻率壓制。

---

## 二、度量階段——把集中度量化

沒有度量就沒有管理，而本專案目前**完全沒有任何集中度指標**。有兩個領域
在這裡各自獨立收斂到了**同一套數學**，這件事本身值得知道。

### 赫芬達爾指數（HHI）與「有效數量」（經濟／金融）

HHI = Σ(wᵢ)²，即各佔比的平方和。用於衡量市場集中度（反壟斷）與投資組合
集中風險。它的倒數 **1/HHI 就是「有效持股數量」**——一個持有 100 檔股票
但其中一檔佔 90% 的組合，有效數量接近 1，而不是 100。

值得一併帶走的已知限制：**HHI 不考慮持股之間的相關性。** 兩個「不同」的
新聞源如果轉載同一份通訊社稿件，它們就不是兩個來源，但 HHI 會當成兩個算。

### 多樣性指數與 Hill 數（生態學）

- **Shannon 指數** — 同時考慮豐富度與均勻度；對稀有物種敏感。
- **Simpson 指數** — 側重優勢度；當你在意的是「有沒有單一物種壓倒性
  主導」時更合適。
- **均勻度／等級-豐度曲線** — 把「有幾種」和「分布得多平均」分開看。
- **Hill 數** — 一個統一的家族，把多樣性表達為**有效物種數量**。

**值得注意的收斂現象**：生態學的 2 階 Hill 數在數學上等於倒數 Simpson
指數，而這與**金融的 1/HHI 是同一個量**。兩個領域，沒有交流，得出同一個
答案。這是一個不錯的信號，說明「有效來源數量」對我們而言是個正確的指標，
而不是臨時發明的東西。

### 稀釋曲線與物種累積曲線（生態學）

另一個同樣重要但不同的問題：**我採樣夠了嗎？** 稀釋（rarefaction）讓不同
採樣強度之間的比較得以標準化；累積曲線則顯示繼續採樣還會不會發現新物種，
還是已經進入平台期。

**對應到本專案**：這是我們目前欠缺、但確實有用的診斷——「我們這 21 個
來源真的在帶來不同的故事，還是已經進入平台期、只是在重複收集同一批事件？」
文獻明確指出兩種多樣性指數都對樣本量敏感，這正是稀釋方法存在的原因，也
說明為什麼在規模不同的來源之間直接比原始計數會誤導。

---

## 三、挑選階段——從傾斜的池子裡挑出多樣的子集

這一族最直接對應我們的處境：池子已經收好、也已經傾斜，問題是**推送裡該
放什麼**。

### 最大邊際相關性 MMR（資訊檢索）

經典做法。貪婪地挑選「**與查詢的相關性**」與「**與已選項目的相異度**」
加權組合最高的項目。一個項目要同時既相關**又**不冗餘，才會得高分。

**對我們而言它最重要的性質是那個顯式的 λ 旋鈕**，用來調節相關性與多樣性
的取捨。這個取捨無法迴避——MMR 的貢獻在於把它變成一個**看得見的參數**，
而不是一個意外。

### 行列式點過程 DPP（推薦系統）

機率化的表述：最大化項目相似度核矩陣的行列式。幾何上，行列式就是所選
項目向量張成的體積——最大化它等於挑選在特徵空間中「攤得最開」的項目。
比 MMR 的貪婪啟發式更有原則，代價是更重。

MMR 與 DPP 都是**後處理重排器**：它們重新排序模型的輸出，而不改動模型
本身。這種分離對我們很有吸引力——意味著可以在不動現有挑選邏輯的前提下
加上多樣性。

### 次模最大化／設施選址（機器學習）

底層的通用理論。多樣性目標（設施選址、k-center、DPP、coreset 挑選）大多
是**次模的（submodular）**——具有邊際遞減性質，每多加一個項目帶來的增益
都比前一個少。關鍵結論是：在基數限制下最大化單調次模函數是 NP-hard，
**但一個簡單的貪婪演算法就能達到最優解的 (1 − 1/e) ≈ 63%**，而且有證明。

**這在實務上的意義**：那個顯而易見的貪婪做法——反覆取增益最大的那個——
不是土法煉鋼，它有最壞情況保證。對本專案這種規模而言，這等於是「可以放心
實作簡單版本，而且知道它站得住腳」的許可。

### 主機擁擠限制 host crowding（搜尋引擎的生產實踐）

Google 長期以來的答案粗暴而直接，也正因如此值得尊重：**限制每個網域的
結果數**——多數查詢下每個站點最多約兩條，但當查詢本身就顯示使用者對某個
特定網域有興趣時放寬。

**這是整份調研中最直接可遷移的解法。** 同樣的問題（單一發布者主導結果
集），用最粗暴的機制解決，而且是在全世界最大的生產排序系統之一裡運行。
值得一併抄走的細節是那個逃生門：**當集中本身就是使用者想要的時候，限制
會放寬。**

---

## 四、校正階段——接受傾斜，事後校正

適用於既無法重新收集、也無法重新挑選的情況。

### 事後分層、耙梳法、逆機率加權（調查研究）

對已收集的樣本加權，使其組成符合已知的母體邊際分布。這是「收集端沒法修，
那就用數學修」的路線。

### Malmquist 偏差校正（天文學）

選擇效應的經典範例：在流量受限的巡天中，本質明亮的天體被過度取樣，因為
它們在大得多的體積範圍內仍可被偵測到。1924 年首次描述，且可校正——已知
距離的前提下，對「給定真實光度的天體能被偵測到的相對體積」做幾何校正。

**這裡真正的概念禮物是那個重新框定**：過度取樣**不是資料的錯誤**。每一筆
觀測都是真實且正確的。扭曲在於**偵測機率隨著你要量測的那個屬性而變化**。
套用到我們身上：HN 的獨占不是壞資料，而是偵測率的產物——HN 比較「亮」
（發文頻率高），所以填滿了時間窗口，正如明亮的星系填滿流量受限的巡天。

### 批次效應與族群分層（基因體學）

跨來源匯總樣本會產生**批次效應**，可能製造出偽發現——虛假的族群結構、
錯誤的插補、偽突變判讀。標準緩解手段是共變量調整（主成分）與 ComBat
之類的資料協調管線。

**該文獻本身給出的誠實但書**：校正並不完整。當結構是近期形成或分布尖銳
時，**任何方法都無法完全校正**——基於常見變異的主成分對這類結構根本
不具資訊量。這一點值得帶走當作現實感：**事後校正嚴格弱於一開始就不製造
失衡。**

---

## 三個跨領域的通用教訓

**1. 多樣性一定與相關性互相排擠，所以要把它做成參數，而不是意外。**
MMR 的 λ、區組隨機化的區組大小、host crowding 的網域上限——每一個成熟的
解法都把這個取捨顯式暴露出來。本專案這次的失效模式恰恰是：這個取捨是被
**隱含地**做掉的（純時間排序碰巧等於「零多樣性」），所以根本沒有人選擇過它。

**2. 集中有時候是真實信號，不是偏差。** Malmquist 偏差是這一點最銳利的
表述：那些明亮天體確實就是亮的。Google 的 host crowding 逃生門在操作層面
說了同一件事——當查詢真的就是關於某一個站點時，只顯示那個站點才是正確的。
**粗暴的硬性上限會丟掉真實資訊。** 如果 HN 確實比 BBC 承載更多科技新聞，
強迫它們均分只會讓推送變差，不會變好。

**3. 你在哪裡介入，界定了你還能挽回多少。** 設計階段的解法（分層、體積
受限）從根本上防止問題發生。挑選階段的解法（MMR、host crowding）在你已經
收到的東西上運作。校正階段的解法（加權、ComBat）最弱，而基因體學文獻明白
指出它們並不完整。應該盡量往前介入——但要注意，對我們而言**抓取階段已經
收集並快取完畢**，所以挑選階段才是現實的層級，設計階段（抓取時的每來源
配額）則是上游選項。

---

## 哪些真的能對應到本專案

| 方法 | 領域 | 這裡適用嗎 | 對應到 |
|---|---|---|---|
| **Host crowding（每來源上限）** | 搜尋引擎 | **適用——投入產出比最高。** 粗暴、經過大規模驗證、約 10 行 | `news-ranking-plan.md` 方案 A |
| **有效數量（1/HHI、Hill 數）** | 金融／生態 | **適用——但作為指標而非修法。** 每次推送記錄有效來源數；讓問題可見、可調 | 目前沒有——真正的缺口 |
| **MMR** | 資訊檢索 | **適用** — 需要項目相似度函數，而這正是方案 E（嵌入向量）會提供的東西 | 方案 B + E |
| **最小化法** | 臨床試驗 | **適用，而且被低估** — 抓取是依序到達的問題，正是最小化法設計來解決的 | 可改善抓取端的平衡 |
| **分層／配額抽樣** | 調查統計 | **適用** — 在抓取階段依來源類別（`forum`／`api`／`rss`）設配額 | 方案 A 的上游版本 |
| **稀釋／累積曲線** | 生態學 | **適用，作為診斷** — 「21 個來源真的在增加不同故事，還是已經進入平台期？」 | 目前沒有 |
| **次模貪婪保證** | 機器學習 | **間接適用** — 為「實作簡單的貪婪版本」提供理論背書，且知道它在最優解的 (1−1/e) 之內 | 方案 B 的理論基礎 |
| **DPP** | 推薦系統 | 在這個規模下可能過度設計，但如果 MMR 太粗糙，它是正確的下一步 | — |
| **事後分層／加權** | 調查研究 | **不適用** — 我們直接控制挑選，事後校正嚴格劣於一開始就挑得更好 | — |
| **ComBat／主成分調整** | 基因體學 | **不適用** — 沒有等價的潛在批次結構需要迴歸掉 | — |
| **Malmquist 校正** | 天文學 | **方法不適用，但心智模型適用** — 把 HN 獨占重新框定為偵測率產物而非壞資料 | 方案 B 的思考框架 |

### 這份調研真正暴露出的缺口

四個介入位置當中，本專案最便宜就能補上的是**度量**。整個程式碼庫裡沒有
任何一個指標在衡量一份推送有多集中。有效來源數只是對現有的來源計數做一行
算術，而少了它，任何從 `news-ranking-plan.md` 選出來的修法都只能靠肉眼
看推送來評估——而這恰恰就是讓來源塌陷一直沒被發現、直到人類注意到才浮現的
那個失效模式。

**本調研的建議閱讀順序**：先做度量（幾乎免費，而且讓後面所有東西變得
可評估），再上 host crowding 當止血，等嵌入向量到位後再做 MMR。

## 參考來源

**挑選／重排**
- [*Result Diversification in Search and Recommendation: A Survey*](https://arxiv.org/pdf/2212.14464)
- [*SMMR: Sampling-Based MMR Reranking*](https://dl.acm.org/doi/10.1145/3726302.3730250)（SIGIR 2025）
- [*Personalized Re-ranking for Improving Diversity in Live Recommender Systems*](https://dlp-kdd.github.io/dlp-kdd2020/assets/pdf/a8-wang.pdf)（KDD）
- [*apricot: Submodular selection for data summarization in Python*](https://arxiv.org/pdf/1906.03543) 與 [*Coresets for Data-efficient Training*](https://cs.stanford.edu/people/jure/pubs/craig-icml20.pdf)（ICML）
- [*A Coreset Selection of Coreset Selection Literature*](https://arxiv.org/html/2505.17799v1)
- Google host crowding／站點多樣性：[SEroundtable](https://www.seroundtable.com/google-search-domain-diversity-update-27696.html)、[Site Diversity System](https://clicksgorilla.com/blog/site-diversity-system-how-google-prevents-overrepresentation-in-search-results)

**設計／抽樣**
- [分層抽樣](https://en.wikipedia.org/wiki/Stratified_sampling) 與 [配額抽樣](https://en.wikipedia.org/wiki/Quota_sampling)
- [*Techniques for randomization and allocation for clinical trials*](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11758574/)
- [*Minimization in randomized clinical trials*](https://onlinelibrary.wiley.com/doi/10.1002/sim.9916)（Statistics in Medicine, 2023）
- [*How to Balance Prognostic Factors: Stratified Permuted Block Randomization or Minimization?*](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11202503/)

**度量**
- [*Concentration indicators*](https://www.bis.org/ifc/events/6ifcconf/avilaetal.pdf)（國際清算銀行 BIS）
- [*Generalized Herfindahl-Hirschman Index to Estimate Diversity Score of a Portfolio*](https://dvararesearch.com/wp-content/uploads/2023/12/Generalized-HHI-to-Estimate-Diversity-Score-of-a-Portfolio.pdf)
- [*Community assessment techniques and the implications for rarefaction and extrapolation with Hill numbers*](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5743490/)
- [Measuring Biodiversity](https://bio.libretexts.org/Courses/CT_State_Northwestern/General_Ecology_Ecology/Chapter_22%3A_Biodiversity/22.5%3A_Measuring_Biodiveristy)（Biology LibreTexts）

**校正**
- [Malmquist 偏差](https://www.oxfordreference.com/view/10.1093/oi/authority.20110803100129765)（Oxford Reference）與 [*Selection effects in correlated observations*](https://arxiv.org/html/2607.22425v1)
- [*What's in a Survey? Simulation-Induced Selection Effects in Astronomy*](https://link.springer.com/chapter/10.1007/978-3-031-26618-8_12)
- [*A data harmonization pipeline to leverage external controls and boost power in GWAS*](https://pmc.ncbi.nlm.nih.gov/articles/PMC8825237/)
- [*Demographic history mediates the effect of stratification on polygenic scores*](https://elifesciences.org/articles/61548)（eLife）
- [*Who's (Not) Afraid of the Batch Effect Boogeyman?*](https://gatk.broadinstitute.org/hc/en-us/articles/18440923786907-Who-s-Not-Afraid-of-the-Batch-Effect-Boogeyman)（Broad Institute / GATK）

**媒體多樣性**
- [*Echo chambers, filter bubbles, and polarisation: a literature review*](https://reutersinstitute.politics.ox.ac.uk/echo-chambers-filter-bubbles-and-polarisation-literature-review)（Reuters Institute）
- [*Understanding Echo Chambers and Filter Bubbles*](https://misq.umn.edu/misq/article/44/4/1619/1818/Understanding-Echo-Chambers-and-Filter-Bubbles-The)（MIS Quarterly）
