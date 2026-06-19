# 📈 Stock Radar

24 小时股票/财经信号雷达，覆盖 **A 股 / 港股 / 美股**。零 API、零 Key、零服务器。

> Fork 自 [LearnPrompt/ai-news-radar](https://github.com/LearnPrompt/ai-news-radar) 的双层架构，把 AI 信号换成股票信号。

## 快速开始

### 本地预览

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install feedparser requests

# 跑一次（用 demo 数据，无网络请求）
python scripts/update_news.py --skip-network --output-dir data

# 启动本地预览
python -m http.server 8080
# 打开 http://localhost:8080
```

### 真实抓取

不传 `--skip-network` 就会走真实网络：

```bash
python scripts/update_news.py --output-dir data
```

## 架构

```
┌─────────────────┐    ┌──────────────────┐    ┌───────────────┐
│  fetchers.py    │ →  │  classify.py     │ →  │  update_news  │
│  RSS + 巨潮 +   │    │  分类 + 打分      │    │  主流程       │
│  SEC EDGAR +    │    │                  │    │               │
│  港交所 + OPML  │    │                  │    │               │
└─────────────────┘    └──────────────────┘    └───────┬───────┘
                                                       │
                                          ┌────────────▼────────────┐
                                          │  data/*.json            │
                                          │  · latest-24h.json      │
                                          │  · stories-merged.json  │
                                          │  · daily-brief.json     │
                                          │  · source-status.json   │
                                          └────────────┬────────────┘
                                                       │
                                          ┌────────────▼────────────┐
                                          │  index.html + assets/   │
                                          │  GitHub Pages 部署      │
                                          └─────────────────────────┘
```

## 信源

| 类型 | 来源 | 层级 | 备注 |
|---|---|---|---|
| A 股 RSS | 财新、华尔街见闻、证券时报 | 主流财经 (tier 1) | `feeds/follow.example.opml` |
| 美股 RSS | Reuters、MarketWatch、CNBC | 主流财经 (tier 1) | 同上 |
| 港股 RSS | AAStocks | 二线财经 (tier 2) | 同上 |
| A 股公告 | 巨潮资讯 | 官方一手 (tier 0) | `fetchers.fetch_cninfo` |
| 美股公告 | SEC EDGAR (8-K/10-Q/10-K) | 官方一手 (tier 0) | `fetchers.fetch_sec_edgar` |
| 港股公告 | 港交所 HKEXnews | 官方一手 (tier 0) | `fetchers.fetch_hkexnews` |
| 私有 RSS | 自加 OPML | RSS/OPML (tier 3) | 通过 `FOLLOW_OPML_B64` Secret 注入 |

## 分类标签

`earnings 财报业绩` · `guidance 业绩指引` · `mna 并购重组` · `policy 监管政策` · `macro 宏观数据` · `product 产品业务` · `capital 资本运作` · `management 管理层` · `industry 行业动态` · `market 大盘行情` · `other 综合`

重要性打分规则：见 `scripts/classify.py`，一手源基础分高，财报/并购/政策类额外加权。

## 部署到 GitHub Pages

1. Fork 这个仓库
2. Settings → Pages → Source 选 `GitHub Actions`
3. (可选) 想加自己的私有信源？Settings → Secrets → New repository secret：
   - Name: `FOLLOW_OPML_B64`
   - Value: 你的 `feeds/follow.opml` 文件 base64 编码后的字符串
4. 每日 UTC 22:00 自动跑 Actions；也可以手动 `Actions → Update Stock Radar → Run workflow`

## 加新信源

修改 `feeds/follow.example.opml`（公开版）或用私有 OPML（Secret 注入）。需要新分类/新逻辑？看 `scripts/classify.py`。

## 安全边界

- 只发 GET 请求（巨潮是 POST 但只拉公开公告数据）
- 不需要任何 API Key、Token、Cookie
- 私有 OPML 走 GitHub Secret，**不要**提交到仓库
- SEC EDGAR 自带限流（10 req/s），已在 fetcher 中加 0.15s 间隔
- 巨潮无鉴权但有频控，建议每小时 ≤ 60 次

## License

MIT
