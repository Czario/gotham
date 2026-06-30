"""
Trace exactly why geographic segment facts don't make it into the DB for Apple FY2026 Q2.
"""
import os, sys
from dotenv import load_dotenv
load_dotenv(dotenv_path='.env')
import logging
logging.disable(logging.CRITICAL)

from arelle import Cntlr, ModelManager
ctrl = Cntlr.Cntlr(logFileName='logToStdErr')
modelManager = ModelManager.initialize(ctrl)
url = 'https://www.sec.gov/Archives/edgar/data/320193/000032019326000013/aapl-20260328.htm'
print(f'Loading {url}...')
modelXbrl = modelManager.load(url)
print(f'Loaded. {len(modelXbrl.facts)} facts\n')

# Find CostOfGoodsAndServicesSold facts with geographic segment dimension
target_concept_local = 'CostOfGoodsAndServicesSold'
geo_axis_keyword = 'StatementBusinessSegments'

print(f'=== All facts for {target_concept_local} ===')
target_facts = [f for f in modelXbrl.facts 
                if f.concept is not None and f.concept.qname.localName == target_concept_local]
print(f'Total: {len(target_facts)} facts\n')

for f in target_facts:
    dims = {}
    if f.context and hasattr(f.context, 'qnameDims') and f.context.qnameDims:
        for dq, dv in f.context.qnameDims.items():
            axis = dq.localName if hasattr(dq, 'localName') else str(dq)
            if hasattr(dv, 'memberQname') and dv.memberQname:
                member = dv.memberQname.localName if hasattr(dv.memberQname, 'localName') else str(dv.memberQname)
                dims[axis] = member
    
    period = '?'
    if f.context:
        if hasattr(f.context, 'isStartEndPeriod') and f.context.isStartEndPeriod:
            period = f'{f.context.startDatetime.date()} to {f.context.endDatetime.date()}'
        elif hasattr(f.context, 'isInstantPeriod') and f.context.isInstantPeriod:
            period = f'instant:{f.context.instantDatetime.date()}'
    
    print(f'  val={f.xValue}  period={period}  dims={dims}')

print()
print(f'=== Simulating _filter_facts_to_primary_period for CostOfGoodsAndServicesSold (10-Q) ===')
# Simulate the duration filter used in _filter_facts_to_primary_period
kept = []
dropped = []
for f in target_facts:
    if not f.context: continue
    if hasattr(f.context, 'isStartEndPeriod') and f.context.isStartEndPeriod:
        start = f.context.startDatetime
        end = f.context.endDatetime
        dur = (end - start).days / 30.44
        if 2.5 <= dur <= 4.0:
            kept.append((f, dur, start.date(), end.date()))
        else:
            dropped.append((f, dur, start.date(), end.date()))
    elif hasattr(f.context, 'isInstantPeriod') and f.context.isInstantPeriod:
        kept.append((f, 0, None, f.context.instantDatetime.date()))

print(f'Kept by duration filter: {len(kept)}')
for f, dur, s, e in kept:
    dims = {}
    if f.context and hasattr(f.context, 'qnameDims') and f.context.qnameDims:
        for dq, dv in f.context.qnameDims.items():
            if hasattr(dv, 'memberQname') and dv.memberQname:
                dims[dq.localName] = dv.memberQname.localName
    print(f'  {s} to {e} ({dur:.1f}mo) val={f.xValue} dims={dims}')

print(f'\nDropped by duration filter: {len(dropped)}')
for f, dur, s, e in dropped[:5]:
    dims = {}
    if f.context and hasattr(f.context, 'qnameDims') and f.context.qnameDims:
        for dq, dv in f.context.qnameDims.items():
            if hasattr(dv, 'memberQname') and dv.memberQname:
                dims[dq.localName] = dv.memberQname.localName
    print(f'  {s} to {e} ({dur:.1f}mo) val={f.xValue} dims={dims}')
