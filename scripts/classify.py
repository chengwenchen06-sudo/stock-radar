"""
分类与打分。

分类（label）：
  earnings       财报/业绩
  guidance       业绩指引
  mna            并购/重组
  policy         监管/政策
  macro          宏观/数据
  product        产品/业务
  capital        融资/回购/股权
  management     管理层/治理
  industry       行业动态
  market         大盘/行情
  other          综合

重要性分层（importance_label）：
  high / medium / low

策略：
  - 一手公告（tier 0）默认 high
  - 一手公告里财报/并购/政策关键词命中 → high
  - 主流财经媒体（tier 1）+ 关键词命中 → medium
  - 其他 → low
"""
from __future__ import annotations
import re

# (label, 关键词正则列表)。中文/英文都覆盖。
KEYWORDS = [
    ("earnings",   [r"财报", r"业绩", r"季报", r"年报", r"盈利", r"营收", r"净利润", r"EPS",
                   r"earnings", r"revenue", r"profit", r"quarterly results"]),
    ("guidance",   [r"指引", r"预期", r"展望", r"预警", r"下调", r"上调",
                   r"guidance", r"outlook", r"forecast", r"preliminary"]),
    ("mna",        [r"并购", r"收购", r"重组", r"分拆", r"私有化", r"退市",
                   r"acqui", r"merger", r"buyout", r"spin.?off", r"delist"]),
    ("policy",     [r"证监会", r"央行", r"美联储", r"Fed", r"加息", r"降息", r"监管", r"政策",
                   r"SEC", r"antitrust", r"tariff", r"sanction"]),
    ("macro",      [r"CPI", r"PPI", r"GDP", r"非农", r"PMI", r"通胀", r"失业率", r"零售销售",
                   r"inflation", r"jobs report", r"unemployment"]),
    ("product",    [r"发布", r"新品", r"上市", r"芯片", r"车型", r"签约", r"订单",
                   r"launch", r"unveil", r"release", r"ship"]),
    ("capital",    [r"回购", r"分红", r"派息", r"增发", r"配股", r"IPO", r"融资",
                   r"buyback", r"dividend", r"offering"]),
    ("management", [r"CEO", r"CFO", r"董事长", r"总经理", r"辞职", r"任命", r"董事会",
                   r"resign", r"appoint", r"board"]),
    ("industry",   [r"行业", r"产业链", r"产能", r"涨价", r"降价", r"出货量"]),
    ("market",     [r"大盘", r"指数", r"开盘", r"收盘", r"涨跌", r"牛市", r"熊市",
                   r"index", r"S&P", r"NASDAQ", r"Dow", r"hang seng", r"恒指"]),
]

def classify(title: str, summary: str = "") -> str:
    """返回单个最匹配的 label。
    优先级（从高到低）：mna > policy > macro > earnings > guidance > capital > management > industry > product > market > other
    同优先级内命中次数最多的胜出。
    """
    blob = f"{title} {summary or ''}".lower()

    # 优先：精确短语（高特异性场景）
    precise = [
        ("macro",      [r"cpi", r"ppi", r"gdp", r"pmi", r"非农", r"失业率", r"零售销售", r"通胀"]),
        ("industry",   [r"产业链", r"出货量", r"销量", r"行业动态", r"产能"]),
        ("mna",        [r"并购", r"收购", r"私有化", r"退市", r"分拆", r"重组",
                       r"acqui", r"merger", r"buyout", r"spin.?off", r"delist"]),
        ("policy",     [r"证监会", r"央行", r"美联储", r"sec\b", r"加息", r"降息",
                       r"antitrust", r"tariff", r"sanction"]),
        ("earnings",   [r"财报", r"季报", r"年报", r"净利润", r"营收", r"eps",
                       r"earnings", r"quarterly results"]),
        ("capital",    [r"回购", r"分红", r"派息", r"增发", r"配股", r"ipo",
                       r"buyback", r"dividend", r"offering"]),
        ("product",    [r"发布", r"新品", r"上市", r"芯片", r"车型",
                       r"launch", r"unveil", r"ship"]),
        ("management", [r"ceo", r"cfo", r"董事长", r"总经理", r"辞职", r"任命",
                       r"resign", r"appoint"]),
        ("market",     [r"大盘", r"开盘", r"收盘", r"恒指", r"hang seng",
                       r"nasdaq", r"s&p", r"dow"]),
        ("guidance",   [r"业绩指引", r"业绩预告", r"业绩快报",
                       r"guidance", r"outlook", r"preliminary"]),
    ]
    for label, patterns in precise:
        for p in patterns:
            if re.search(p, blob, flags=re.IGNORECASE):
                return label
    return "other"

def importance(item: dict) -> tuple[str, int]:
    """返回 (importance_label, importance_score 0-100)。"""
    title = item.get("title", "")
    summary = item.get("summary", "")
    tier = item.get("source_tier_rank", 5)
    label = classify(title, summary)
    score = 0

    # 一手源基础分
    if tier == 0:
        score += 60
    elif tier == 1:
        score += 40
    elif tier == 2:
        score += 30
    else:
        score += 15

    # 关键类别加分
    if label in ("earnings", "mna", "policy", "macro"):
        score += 25
    elif label in ("guidance", "capital", "management"):
        score += 15
    elif label in ("product", "industry", "market"):
        score += 8

    # 命中强信号词加分
    blob = f"{title} {summary}".lower()
    STRONG = ["财报", "业绩", "并购", "收购", "重组", "私有化", "退市",
              "earnings", "acquisition", "merger", "buyout",
              "美联储", "fed", "加息", "降息", "tariff", "sanction"]
    if any(s.lower() in blob for s in STRONG):
        score += 10

    score = min(score, 100)
    if score >= 70:
        label_out = "high"
    elif score >= 40:
        label_out = "medium"
    else:
        label_out = "low"
    return label_out, score

LABEL_ZH = {
    "earnings":   "财报业绩",
    "guidance":   "业绩指引",
    "mna":        "并购重组",
    "policy":     "监管政策",
    "macro":      "宏观数据",
    "product":    "产品业务",
    "capital":    "资本运作",
    "management": "管理层",
    "industry":   "行业动态",
    "market":     "大盘行情",
    "other":      "综合",
}

MARKET_ZH = {"cn": "A股", "hk": "港股", "us": "美股", "global": "全球"}