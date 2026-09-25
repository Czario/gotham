import json

log_file = "/Users/aijaz/.gemini/antigravity-ide/brain/c69eb494-b2bb-4eee-ae62-ddccffdc1eeb/.system_generated/tasks/task-986.log"
with open(log_file, "r") as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "Agent finalize JSON for AAPL" in line:
        print(f"Found finalize at line {i}")
        print(line[:1000]) # just peek
