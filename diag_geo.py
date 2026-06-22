"""
Diagnostic: Load Apple FY2026 Q2 XBRL and check geographic segment facts.
"""
import os, sys
from dotenv import load_dotenv
load_dotenv(dotenv_path='.env')

# Find the cached XBRL zip/file
import glob
cache_path = os.getenv('XBRL_ZIP_CACHE_PATH', '')
print(f'XBRL cache: {cache_path}')

# Find Apple's Q2 FY2026 file
patterns = [
    f'{cache_path}/*320193*26-000013*',
    f'{cache_path}/**/*aapl-2026*',
    f'{cache_path}/**/*320193*2026*',
]
found = []
for p in patterns:
    found.extend(glob.glob(p, recursive=True))
print(f'Cached files: {found[:5]}')

if not found:
    print('No cached file - downloading via Arelle...')
    url = 'https://www.sec.gov/Archives/edgar/data/320193/000032019326000013/aapl-20260328.htm'
else:
    url = found[0]
    print(f'Using cached: {url}')

print(f'\nLoading XBRL from: {url}')

# Load via Arelle
from arelle import Cntlr, ModelManager
import logging
logging.disable(logging.CRITICAL)

ctrl = Cntlr.Cntlr(logFileName='logToStdErr')
modelManager = ModelManager.initialize(ctrl)
modelXbrl = modelManager.load(url)

if not modelXbrl:
    print('Failed to load XBRL')
    sys.exit(1)

print(f'Loaded. Total facts: {len(modelXbrl.facts)}')

# Find geographic segment members
geo_keywords = ['americas', 'europe', 'china', 'japan', 'asia', 'pacific', 'segment']
geo_facts = []
product_facts = []
for fact in modelXbrl.facts:
    if fact.concept is None or fact.context is None: continue
    if not hasattr(fact.context, 'qnameDims') or not fact.context.qnameDims: continue
    
    dims = fact.context.qnameDims
    for dim_qname, dim_val in dims.items():
        if hasattr(dim_val, 'memberQname') and dim_val.memberQname:
            member = str(dim_val.memberQname).lower()
            axis = str(dim_qname).lower()
            
            is_geo = any(kw in member for kw in geo_keywords) or any(kw in axis for kw in ['segment', 'geography', 'geographic'])
            is_product = 'product' in axis or 'service' in axis or any(kw in member for kw in ['iphone','ipad','mac','wearable'])
            
            if is_geo and hasattr(fact, 'xValue') and fact.xValue is not None:
                geo_facts.append((str(fact.concept.qname), str(dim_qname), str(dim_val.memberQname), fact.xValue, fact.context.period if hasattr(fact.context,'period') else '?'))
            if is_product:
                product_facts.append((str(fact.concept.qname), str(dim_qname), str(dim_val.memberQname)))

print(f'\nGeo segment facts with values: {len(geo_facts)}')
print(f'Product segment facts: {len(product_facts)}')

print('\n=== Sample geographic segment facts ===')
seen = set()
for concept, axis, member, value, period in sorted(geo_facts, key=lambda x: x[0])[:30]:
    key = (concept, member)
    if key in seen: continue
    seen.add(key)
    concept_local = concept.split(':')[-1]
    member_local = member.split(':')[-1]
    axis_local = axis.split(':')[-1]
    print(f'  {concept_local:<50} {axis_local:<35} {member_local:<35} = {value}')

print('\n=== Period info for geo facts ===')
from datetime import datetime
from collections import Counter
periods = []
for fact in modelXbrl.facts:
    if fact.concept is None or fact.context is None: continue
    if not hasattr(fact.context, 'qnameDims') or not fact.context.qnameDims: continue
    for dim_qname, dim_val in fact.context.qnameDims.items():
        if hasattr(dim_val, 'memberQname') and dim_val.memberQname:
            member = str(dim_val.memberQname).lower()
            if any(kw in member for kw in ['americas', 'europe', 'china', 'japan', 'asiapacific', 'restofasia']):
                if hasattr(fact.context, 'isStartEndPeriod') and fact.context.isStartEndPeriod:
                    start = fact.context.startDatetime
                    end = fact.context.endDatetime
                    if start and end:
                        dur = (end - start).days / 30.44
                        periods.append(f'{start.date()} to {end.date()} ({dur:.1f}mo)')
                elif hasattr(fact.context, 'isInstantPeriod') and fact.context.isInstantPeriod:
                    periods.append(f'instant: {fact.context.instantDatetime}')

for p, n in Counter(periods).most_common(10):
    print(f'  {n:3d}x  {p}')
