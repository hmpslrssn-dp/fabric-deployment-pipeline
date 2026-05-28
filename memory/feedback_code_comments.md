---
name: feedback_code_comments
description: User wants thorough comments in all generated code explaining functionality and reasoning, especially for Python scripts
metadata:
  type: feedback
---

Always include thorough inline comments in generated code — explain what each section does AND why it's done that way. User is new to deployment pipelines and not very experienced with Python, so comments should be educational and build understanding, not just label what the code does.

**Why:** User has explicitly asked for this to help them learn as they read the code.

**How to apply:** Apply to all Python scripts and config files in this project. Comments should explain concepts (e.g. why we use environment variables instead of hardcoding values), not just narrate the code line by line.
