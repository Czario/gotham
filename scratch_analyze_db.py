import os
from collections import defaultdict
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(".env")
client = MongoClient(os.environ["MONGODB_URI"])
db = client[os.environ["DATABASE_NAME"]]
cik = "0000320193"

print("=================================================================")
print("COMPREHENSIVE HIERARCHY ANALYSIS FOR AAPL (CIK 0000320193)")
print("=================================================================")

for coll_name in ["normalized_concepts_annual", "normalized_concepts_quarterly"]:
    coll = db[coll_name]
    print(f"\n################ {coll_name} ################")
    
    for stmt in ["income", "balancesheet", "cashflow"]:
        docs = list(coll.find({"cik": cik, "statement_type": stmt}))
        main_docs = [d for d in docs if not d.get("dimension_concept")]
        dim_docs = [d for d in docs if d.get("dimension_concept")]
        
        print(f"\n>>> STATEMENT: {stmt.upper()} (Main: {len(main_docs)}, Dims: {len(dim_docs)}) <<<")
        
        # 1. Path duplicate / collision check
        path_to_concepts = defaultdict(list)
        for d in main_docs:
            p = str(d.get("path"))
            path_to_concepts[p].append(d.get("concept"))
        
        collisions = {p: cs for p, cs in path_to_concepts.items() if len(cs) > 1}
        if collisions:
            print(f"  ❌ PATH COLLISIONS IN MAIN ROWS ({len(collisions)}):")
            for p, cs in collisions.items():
                print(f"     Path {p}: {cs}")
        else:
            print("  ✅ No path collisions in main rows")
            
        # 2. Check for missing paths or None paths
        no_paths = [d.get("concept") for d in main_docs if not d.get("path")]
        if no_paths:
            print(f"  ❌ CONCEPTS WITH NO PATH ({len(no_paths)}): {no_paths}")
            
        # 3. Check for orphan paths (parent path does not exist)
        all_paths = {str(d.get("path")) for d in docs if d.get("path")}
        orphans = []
        for d in docs:
            p = str(d.get("path") or "")
            if "." in p and p != "555":
                parent_p = p.rsplit(".", 1)[0]
                if parent_p not in all_paths:
                    orphans.append((d.get("concept"), p, parent_p))
        if orphans:
            print(f"  ❌ ORPHAN PATHS ({len(orphans)}):")
            for c, p, pp in orphans[:8]:
                print(f"     {c} at {p} missing parent {pp}")
        else:
            print("  ✅ No orphan paths")
            
        # 4. Check parent_concept vs path hierarchy alignment
        misaligned_parents = []
        concept_to_doc = {d.get("concept"): d for d in main_docs}
        for d in main_docs:
            p = str(d.get("path") or "")
            par_c = d.get("parent_concept")
            if "." in p and p != "555" and par_c:
                parent_p = p.rsplit(".", 1)[0]
                expected_parent_doc = concept_to_doc.get(par_c)
                if expected_parent_doc and str(expected_parent_doc.get("path")) != parent_p:
                    misaligned_parents.append((d.get("concept"), p, par_c, expected_parent_doc.get("path"), parent_p))
        if misaligned_parents:
            print(f"  ⚠️ MISALIGNED PARENT CONCEPTS ({len(misaligned_parents)}):")
            for c, p, par_c, exp_p, actual_p in misaligned_parents[:8]:
                print(f"     {c} path={p} parent={par_c} (parent path={exp_p}, expected prefix={actual_p})")
                
        # 5. List all main rows in order
        main_docs.sort(key=lambda x: str(x.get("path") or ""))
        print("\n  FULL TREE (Main Rows):")
        for d in main_docs:
            p = str(d.get("path"))
            o = str(d.get("order_key"))
            c = str(d.get("concept"))
            lbl = str(d.get("label") or "")[:35]
            par = str(d.get("parent_concept") or "ROOT")
            is_abs = " [ABSTRACT]" if d.get("abstract") else ""
            print(f"    {p:<14} {o:<4} {c:<50} | {lbl:<35} | parent={par}{is_abs}")

