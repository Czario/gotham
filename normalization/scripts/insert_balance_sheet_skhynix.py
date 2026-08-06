#!/usr/bin/env python3
"""
Insert balance sheet concepts + values for SK hynix Inc. (CIK: 0002120882).

Data sources:
  - Annual 2018-2025: Euroland IR XLS (levelZero=0)
  - Quarterly 2024Q2-2026Q1: Euroland IR XLS (levelZero=1)
  - All source values in KRW millions; converted to USD using period average rates.

Collections written:
  - normalized_concepts_annual / normalized_concepts_quarterly
  - concept_values_annual / concept_values_quarterly

Run:
    uv run python normalization/scripts/insert_balance_sheet_skhynix.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "src"))

from bson import ObjectId
from pymongo import MongoClient
from data_normalization_service.core.config import AppConfig

COMPANY_CIK = "0002120882"
STATEMENT_TYPE = "balancesheet"

# ---------------------------------------------------------------------------
# Exchange rates (annual average KRW per 1 USD)
# ---------------------------------------------------------------------------
ANNUAL_FX = {
    2018: 1100.3, 2019: 1165.7, 2020: 1180.1, 2021: 1144.9,
    2022: 1291.8, 2023: 1316.0, 2024: 1380.0, 2025: 1413.0,
}
QUARTERLY_FX = {
    (2024, 2): 1375.0, (2024, 3): 1349.0, (2024, 4): 1400.0,
    (2025, 1): 1452.0, (2025, 2): 1376.0, (2025, 3): 1381.0,
    (2025, 4): 1475.0, (2026, 1): 1460.0,
}

def krw_to_usd(krw_millions: float, fx_rate: float) -> float:
    return round(krw_millions * 1_000_000 / fx_rate)

# ---------------------------------------------------------------------------
# Annual balance sheet — KRW millions (Euroland, years 2018-2025)
# ---------------------------------------------------------------------------
ANNUAL_YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025]

ANNUAL_KRW = {
    # ── Assets ──────────────────────────────────────────────────────────
    "us-gaap:Assets": [
        63_658_335, 65_248_350, 71_173_853, 96_346_526,
        103_871_512, 100_330_165, 119_855_209, 176_107_659,
    ],
    "us-gaap:AssetsCurrent": [
        19_894_146, 14_457_602, 16_570_953, 26_907_076,
        28_733_332, 30_468_100, 42_278_887, 69_458_073,
    ],
    "us-gaap:CashAndCashEquivalentsAtCarryingValue": [
         8_369_350,  3_994_713,  4_948_215,  8_672_542,
         6_408_992,  8_920_919, 14_156_363, 34_942_253,
    ],
    "us-gaap:AccountsReceivableNetCurrent": [
         6_319_994,  4_261_674,  4_931_322,  8_267_111,
         5_186_054,  6_600_273, 13_019_006, 18_199_078,
    ],
    "us-gaap:InventoryNet": [
         4_422_733,  5_295_835,  6_136_318,  8_950_087,
        15_664_707, 13_480_659, 13_313_937, 14_289_390,
    ],
    "us-gaap:AssetsNoncurrent": [
        43_764_189, 50_790_748, 54_602_900, 69_439_450,
        75_138_180, 69_862_065, 77_576_322, 106_649_586,
    ],
    "us-gaap:PropertyPlantAndEquipmentNet": [
        34_952_617, 39_949_940, 41_230_562, 53_225_667,
        60_228_528, 52_704_853, 60_157_474, 77_502_704,
    ],
    "us-gaap:IntangibleAssetsNetExcludingGoodwill": [
         2_678_770,  2_571_049,  3_400_278,  4_797_162,
         3_512_107,  3_834_567,  4_018_847,  4_049_402,
    ],
    # ── Liabilities ─────────────────────────────────────────────────────
    "us-gaap:LiabilitiesCurrent": [
        13_031_852,  7_961_966,  9_072_360, 14_735_395,
        19_843_696, 21_007_810, 24_965_444, 37_386_270,
    ],
    "us-gaap:LongTermDebt": [
         5_281_937, 10_523_506, 11_251_648, 17_623_808,
        22_994_604, 29_468_632, 22_683_733, 22_247_905,
    ],
    "us-gaap:AccountsPayableCurrent": [
         1_096_380,  1_042_542,  1_046_159,  1_359_247,
         2_186_230,  1_845_537,  2_277_347,  2_848_455,
    ],
    # ── Equity ──────────────────────────────────────────────────────────
    "us-gaap:StockholdersEquity": [
        46_852_331, 47_935_882, 51_909_097, 62_191_059,
        63_290_542, 53_503_752, 73_915_704, 120_666_733,
    ],
    "us-gaap:CommonStockValue": [
        3_657_652, 3_657_652, 3_657_652, 3_657_652,
        3_657_652, 3_657_652, 3_657_652,   3_657_652,
    ],
    "us-gaap:RetainedEarningsAccumulatedDeficit": [
        42_033_601, 42_923_362, 46_995_728, 55_784_068,
        56_685_260, 46_729_313, 65_418_062, 106_576_548,
    ],
}

# ---------------------------------------------------------------------------
# Quarterly balance sheet — KRW millions (Euroland, 2024Q2-2026Q1)
# ---------------------------------------------------------------------------
QUARTERLY_PERIODS = [
    (2024, 2), (2024, 3), (2024, 4),
    (2025, 1), (2025, 2), (2025, 3), (2025, 4),
    (2026, 1),
]
QUARTER_END   = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}

QUARTERLY_KRW = {
    "us-gaap:Assets": [
        105_624_349, 108_367_058, 119_855_209,
        123_985_470, 129_087_781, 148_435_030, 176_107_659,
        222_828_744,
    ],
    "us-gaap:AssetsCurrent": [
        35_683_696, 37_078_030, 42_278_887,
        41_228_190, 45_203_170, 57_139_556, 69_458_073,
        106_506_116,
    ],
    "us-gaap:CashAndCashEquivalentsAtCarryingValue": [
         9_688_059, 10_857_868, 14_156_363,
        12_558_069,  9_075_311, 10_814_515, 14_923_766,
        21_166_904,
    ],
    "us-gaap:AccountsReceivableNetCurrent": [
        10_223_675, 10_664_450, 13_019_006,
        10_628_589, 13_125_210, 14_312_462, 18_199_078,
        33_807_843,
    ],
    "us-gaap:InventoryNet": [
        13_354_938, 13_353_865, 13_313_937,
        14_551_350, 13_408_333, 13_156_389, 14_289_390,
        15_974_133,
    ],
    "us-gaap:AssetsNoncurrent": [
        69_940_652, 71_289_027, 77_576_322,
        82_757_280, 83_884_610, 91_295_474, 106_649_586,
        116_322_628,
    ],
    "us-gaap:PropertyPlantAndEquipmentNet": [
        53_231_992, 54_633_704, 60_157_474,
        63_015_300, 64_472_933, 68_145_161, 77_502_704,
        82_051_924,
    ],
    "us-gaap:IntangibleAssetsNetExcludingGoodwill": [
         3_839_880,  3_926_466,  4_018_847,
         3_962_700,  3_945_141,  3_953_945,  4_049_402,
         4_050_617,
    ],
    "us-gaap:LiabilitiesCurrent": [
        23_354_170, 23_031_263, 24_965_444,
        24_836_401, 25_324_404, 29_769_431, 37_386_270,
        40_700_530,
    ],
    "us-gaap:LongTermDebt": [
        25_227_968, 21_844_780, 22_683_733,
        23_333_671, 21_841_038, 24_078_735, 22_247_905,
        19_317_665,
    ],
    "us-gaap:AccountsPayableCurrent": [
         2_093_222,  1_889_926,  2_277_347,
         1_875_167,  1_805_749,  2_270_386,  2_848_455,
         2_797_840,
    ],
    "us-gaap:StockholdersEquity": [
        59_830_020, 65_301_520, 73_915_704,
        81_438_753, 87_142_485, 100_006_279, 120_666_733,
        164_379_799,
    ],
    "us-gaap:CommonStockValue": [
        3_657_652, 3_657_652, 3_657_652,
        3_657_652, 3_657_652, 3_657_652,  3_657_652,
        3_657_652,
    ],
    "us-gaap:RetainedEarningsAccumulatedDeficit": [
        52_301_637, 57_842_344, 65_418_062,
        72_621_873, 79_357_906, 91_690_724, 106_576_548,
        148_746_385,
    ],
}

# ---------------------------------------------------------------------------
# Concept hierarchy
# ---------------------------------------------------------------------------
CONCEPTS = [
    # ── Assets ──────────────────────────────────────────────────────────
    {"concept": "us-gaap:Assets",                                "label": "Total assets",                        "path": "001",         "order_key": "a"},
    {"concept": "us-gaap:AssetsCurrent",                         "label": "Total current assets",               "path": "001.001",     "order_key": "aa"},
    {"concept": "us-gaap:CashAndCashEquivalentsAtCarryingValue", "label": "Cash and cash equivalents",          "path": "001.001.001", "order_key": "aaa"},
    {"concept": "us-gaap:AccountsReceivableNetCurrent",          "label": "Trade receivables, net",             "path": "001.001.002", "order_key": "aab"},
    {"concept": "us-gaap:InventoryNet",                          "label": "Inventories, net",                   "path": "001.001.003", "order_key": "aac"},
    {"concept": "us-gaap:AssetsNoncurrent",                      "label": "Total non-current assets",           "path": "001.002",     "order_key": "ab"},
    {"concept": "us-gaap:PropertyPlantAndEquipmentNet",          "label": "Property, plant and equipment, net", "path": "001.002.001", "order_key": "aba"},
    {"concept": "us-gaap:IntangibleAssetsNetExcludingGoodwill",  "label": "Intangible assets, net",             "path": "001.002.002", "order_key": "abb"},
    # ── Liabilities & Equity ────────────────────────────────────────────
    {"concept": "us-gaap:LiabilitiesAndStockholdersEquity",      "label": "Total liabilities and equity",       "path": "002",         "order_key": "b"},
    {"concept": "us-gaap:Liabilities",                           "label": "Total liabilities",                  "path": "002.001",     "order_key": "ba"},
    {"concept": "us-gaap:LiabilitiesCurrent",                    "label": "Total current liabilities",          "path": "002.001.001", "order_key": "baa"},
    {"concept": "us-gaap:LongTermDebt",                          "label": "Interest-bearing debts (total)",     "path": "002.001.002", "order_key": "bab"},
    {"concept": "us-gaap:AccountsPayableCurrent",                "label": "Trade payables",                     "path": "002.001.003", "order_key": "bac"},
    {"concept": "us-gaap:StockholdersEquity",                    "label": "Total shareholders' equity",         "path": "002.002",     "order_key": "bb"},
    {"concept": "us-gaap:CommonStockValue",                      "label": "Capital stock",                      "path": "002.002.001", "order_key": "bba"},
    {"concept": "us-gaap:RetainedEarningsAccumulatedDeficit",    "label": "Retained earnings",                  "path": "002.002.002", "order_key": "bbb"},
]

# ---------------------------------------------------------------------------
# Helpers (same pattern as income statement script)
# ---------------------------------------------------------------------------
def build_concept_doc(concept_def: dict, form_type: str, now: datetime) -> dict:
    return {
        "cik": COMPANY_CIK,
        "statement_type": STATEMENT_TYPE,
        "concept": concept_def["concept"],
        "form_type": form_type,
        "label": concept_def["label"],
        "canonical_concept": concept_def["concept"],
        "path": concept_def["path"],
        "order_key": concept_def["order_key"],
        "abstract": False,
        "dimension": False,
        "dimension_concept": False,
        "hide": False,
        "active": True,
        "merge": False,
        "created_at": now,
        "updated_at": now,
        "updatedAt": now.isoformat().replace("+00:00", "Z"),
        "__v": 0,
    }


def upsert_concepts(collection, form_type: str, now: datetime) -> dict:
    id_map: dict = {}
    for c in CONCEPTS:
        doc = build_concept_doc(c, form_type, now)
        filter_key = {
            "cik": COMPANY_CIK,
            "statement_type": STATEMENT_TYPE,
            "concept": c["concept"],
            "form_type": form_type,
        }
        result = collection.update_one(
            filter_key,
            {
                "$set": {k: v for k, v in doc.items() if k != "created_at"},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        if result.upserted_id:
            id_map[c["concept"]] = result.upserted_id
        else:
            existing = collection.find_one(filter_key, {"_id": 1})
            if existing:
                id_map[c["concept"]] = existing["_id"]
    return id_map


def upsert_value(collection, concept_id, form_type, reporting_period, value, now) -> bool:
    query: dict = {
        "concept_id": concept_id,
        "cik": COMPANY_CIK,
        "statement_type": STATEMENT_TYPE,
        "form_type": form_type,
        "reporting_period.fiscal_year": reporting_period["fiscal_year"],
        "reporting_period.period_date": reporting_period["period_date"],
    }
    if "quarter" in reporting_period:
        query["reporting_period.quarter"] = reporting_period["quarter"]
    if collection.find_one(query):
        return False
    collection.insert_one({
        "concept_id": concept_id,
        "cik": COMPANY_CIK,
        "statement_type": STATEMENT_TYPE,
        "form_type": form_type,
        "reporting_period": reporting_period,
        "value": value,
        "dimension_value": False,
        "decimals": "-6",
        "source": "manual_euroland_f1",
        "created_at": now,
    })
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    config = AppConfig.from_env()
    client = MongoClient(config.database.mongodb_uri)
    db = client[config.database.database_name]
    now = datetime.now(timezone.utc)

    # ── Annual ────────────────────────────────────────────────────────────
    annual_id_map = upsert_concepts(db["normalized_concepts_annual"], "10-K", now)
    print(f"[normalized_concepts_annual]    upserted {len(annual_id_map)} concepts")

    ann_inserted = ann_skipped = 0
    for i, year in enumerate(ANNUAL_YEARS):
        fx = ANNUAL_FX[year]
        period = {
            "fiscal_year": year,
            "period_date": f"{year}-12-31",
            "start_date": datetime(year, 1, 1, tzinfo=timezone.utc),
            "end_date":   datetime(year, 12, 31, tzinfo=timezone.utc),
            "form_type": "10-K",
        }
        for concept, series in ANNUAL_KRW.items():
            cid = annual_id_map.get(concept)
            if not cid:
                continue
            usd = krw_to_usd(series[i], fx)
            if upsert_value(db["concept_values_annual"], cid, "10-K", period, usd, now):
                ann_inserted += 1
            else:
                ann_skipped += 1

    print(f"[concept_values_annual]         inserted={ann_inserted}  skipped={ann_skipped}")

    # ── Quarterly ─────────────────────────────────────────────────────────
    qtr_id_map = upsert_concepts(db["normalized_concepts_quarterly"], "10-Q", now)
    print(f"[normalized_concepts_quarterly] upserted {len(qtr_id_map)} concepts")

    qtr_inserted = qtr_skipped = 0
    for i, (year, quarter) in enumerate(QUARTERLY_PERIODS):
        fx = QUARTERLY_FX[(year, quarter)]
        month = int(QUARTER_END[quarter][:2])
        day   = int(QUARTER_END[quarter][3:])
        sq_month = ((quarter - 1) * 3) + 1
        period = {
            "fiscal_year": year,
            "quarter": quarter,
            "period_date": f"{year}-{QUARTER_END[quarter]}",
            "start_date": datetime(year, sq_month, 1, tzinfo=timezone.utc),
            "end_date":   datetime(year, month, day, tzinfo=timezone.utc),
            "form_type": "10-Q",
        }
        for concept, series in QUARTERLY_KRW.items():
            cid = qtr_id_map.get(concept)
            if not cid:
                continue
            usd = krw_to_usd(series[i], fx)
            if upsert_value(db["concept_values_quarterly"], cid, "10-Q", period, usd, now):
                qtr_inserted += 1
            else:
                qtr_skipped += 1

    print(f"[concept_values_quarterly]      inserted={qtr_inserted}  skipped={qtr_skipped}")
    print("\nDone — SK hynix balance sheet loaded.")
    client.close()


if __name__ == "__main__":
    main()
