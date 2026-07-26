"""Unit tests for classify module — no network required."""
import pytest

from classify import classify, importance, LABEL_ZH

# ── classify() tests ──

EARNINGS_CASES = [
    "贵州茅台 2024年三季报发布",
    "Apple Q4 earnings beat expectations",
    "公司全年净利润同比增长20%",
    "营收突破千亿大关",
    "2024年年度报告摘要",
    "半年度业绩预告",
]

MNA_CASES = [
    "英伟达拟收购以色列AI芯片公司",
    "重大资产重组停牌公告",
    "公司私有化退市方案获批",
    "分拆子公司独立上市",
]

POLICY_CASES = [
    "美联储12月议息会议维持利率不变",
    "证监会发布上市公司治理新规",
    "央行降息25个基点",
    "SEC charges company with fraud",
]

MACRO_CASES = [
    "中国11月CPI同比上涨0.5%",
    "美国非农就业数据超预期",
    "GDP增长5.2%符合预期",
    "通胀率降至2%以下",
]

PRODUCT_CASES = [
    "特斯拉Model Y 2026款正式发布",
    "公司推出新一代AI芯片",
    "新车上市首月订单破万",
]


@pytest.mark.parametrize("title", EARNINGS_CASES)
def test_classify_earnings(title):
    assert classify(title) == "earnings", f"expected earnings for: {title}"


@pytest.mark.parametrize("title", MNA_CASES)
def test_classify_mna(title):
    assert classify(title) == "mna", f"expected mna for: {title}"


@pytest.mark.parametrize("title", POLICY_CASES)
def test_classify_policy(title):
    assert classify(title) == "policy", f"expected policy for: {title}"


@pytest.mark.parametrize("title", MACRO_CASES)
def test_classify_macro(title):
    assert classify(title) == "macro", f"expected macro for: {title}"


@pytest.mark.parametrize("title", PRODUCT_CASES)
def test_classify_product(title):
    assert classify(title) == "product", f"expected product for: {title}"


def test_classify_other():
    assert classify("Some random headline with no financial keywords") == "other"


def test_classify_with_summary():
    result = classify("Short title", "Detailed summary about 并购重组")
    assert result == "mna"


def test_classify_traditional_chinese():
    assert classify("騰訊控股全年業績超預期") == "earnings"
    assert classify("公司回購計劃獲股東大會通過") == "capital"


# ── importance() tests ──

def _make_item(tier, title, summary=""):
    return {
        "title": title,
        "summary": summary,
        "source_tier_rank": tier,
    }


def test_importance_tier0_earnings():
    label, score = importance(_make_item(0, "公司发布年度报告 净利润增长20%"))
    assert label == "high"
    assert score >= 70


def test_importance_tier0_policy():
    label, score = importance(_make_item(0, "证监会发布新规"))
    assert label == "high"
    assert score >= 70


def test_importance_tier1_medium():
    label, score = importance(_make_item(1, "行业动态：芯片产能提升"))
    assert score >= 40
    assert score < 70
    assert label == "medium"


def test_importance_tier3_low():
    label, score = importance(_make_item(3, "普通市场评论"))
    assert label == "low"
    assert score < 40


def test_importance_caps_at_100():
    _, score = importance(_make_item(0, "美联储加息 公司发布财报 宣布并购重组"))
    # tier 0 base 60 + category bonus 25 + strong word bonus 10 = 95
    assert score == 95


# ── LABEL_ZH sanity ──

def test_label_zh_has_all_labels():
    expected = {"earnings", "guidance", "mna", "policy", "macro", "product",
                "capital", "management", "industry", "market", "other"}
    assert set(LABEL_ZH.keys()) == expected
