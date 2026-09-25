import json
from collections import Counter
import sys

def analyze_audit():
    with open(".filings_agent/audit.jsonl") as f:
        counts = Counter()
        issues = []
        for line in f:
            try:
                record = json.loads(line)
                action = record.get("action")
                counts[action] += 1
                
                # Look for errors or breakages
                if record.get("status") in ("error", "failed") or "error" in record or "finding" in record or "findings" in record:
                    issues.append(record)
                
                # Check for specific hierarchy issues
                if action == "propose_hierarchy":
                    if "validation_failures" in record.get("details", {}):
                        issues.append(record)
                        
                if action == "propose_hierarchy_result":
                    pass
            except Exception as e:
                pass
                
        print("Summary of actions:")
        for k, v in counts.most_common():
            print(f"  {k}: {v}")
            
        print(f"\nFound {len(issues)} potential issue records.")
        if issues:
            print("Sample issues:")
            for i in issues[:5]:
                print(json.dumps(i, indent=2))

analyze_audit()
