import json
import sqlite3

# We can't access the database because it wasn't written. But we can parse the json from the log.
import re

log_file = "/Users/aijaz/.gemini/antigravity-ide/brain/c69eb494-b2bb-4eee-ae62-ddccffdc1eeb/.system_generated/tasks/task-1079.log"
with open(log_file, "r") as f:
    text = f.read()

# Let's find the findings json in the hooks output
matches = re.findall(r'{"event":"node_end","node":"validate_final_node".*?}', text)
for m in matches:
    try:
        data = json.loads(m)
        print("Found validate output for", data.get("ticker"))
        # The findings are usually printed in the log, not in the hook.
    except:
        pass

# Let's extract the exact log line
for line in text.splitlines():
    if "duplicate_path_order" in line:
        print(line)
