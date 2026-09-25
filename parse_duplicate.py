import re

with open("/Users/aijaz/.gemini/antigravity-ide/brain/c69eb494-b2bb-4eee-ae62-ddccffdc1eeb/.system_generated/tasks/task-1079.log", "r") as f:
    for line in f:
        if "duplicate_path_order" in line:
            print(line.strip())
