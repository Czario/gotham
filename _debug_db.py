from pymongo import MongoClient
import os
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / '.env')
client = MongoClient(os.getenv('MONGODB_URI'))
db = client[os.getenv('DATABASE_NAME')]
nc_a = db['normalized_concepts_annual']
cv_a = db['concept_values_annual']
cv_q = db['concept_values_quarterly']
metrics = ['us-gaap:Revenues','us-gaap:SalesRevenueNet','us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax',
           'us-gaap:CostOfRevenue','us-gaap:CostOfGoodsAndServicesSold','us-gaap:GrossProfit',
           'us-gaap:OperatingIncomeLoss','us-gaap:NetIncomeLoss']
for tk, cik in [('CSCO','0000858877'),('HPQ','0000047217')]:
    print(f"\n===== {tk} ({cik}) =====")
    print("  comprehensive_income (annual/quarterly, expect 0/0):",
          cv_a.count_documents({'company_cik':cik,'statement_type':'comprehensive_income'}),
          "/", cv_q.count_documents({'company_cik':cik,'statement_type':'comprehensive_income'}))
    print("  Annual statement types:")
    for d in cv_a.aggregate([{'$match':{'company_cik':cik}},{'$group':{'_id':'$statement_type','count':{'$sum':1}}},{'$sort':{'_id':1}}]):
        print(f"    {d['_id']}: {d['count']}")
    print("  income_statement concepts-per-year:")
    rows = list(cv_a.aggregate([{'$match':{'company_cik':cik,'statement_type':'income_statement'}},
        {'$group':{'_id':'$reporting_period.fiscal_year','count':{'$sum':1}}},{'$sort':{'_id':1}}]))
    print("    " + ", ".join(f"{r['_id']}:{r['count']}" for r in rows))
    print("  Key metric year coverage:")
    for m in metrics:
        concs = list(nc_a.find({'company_cik':cik,'concept':m,'statement_type':'income_statement'}))
        if not concs: continue
        yrs=set()
        for c in concs:
            for v in cv_a.find({'concept_id':c['_id']}):
                fy=v['reporting_period'].get('fiscal_year')
                if fy: yrs.add(fy)
        if yrs:
            ys=sorted(yrs); print(f"    {m}: {len(ys)} yrs {min(ys)}-{max(ys)}")
client.close()
