import re

with open("/Users/aijaz/.gemini/antigravity-ide/brain/c69eb494-b2bb-4eee-ae62-ddccffdc1eeb/.system_generated/tasks/task-986.log", "r") as f:
    for line in f:
        if "orphan_hierarchy_path" in line:
            print(line.strip())
