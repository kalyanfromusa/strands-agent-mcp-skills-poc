# UX standards
The chat page (`scripts/chat_ui.html`) is served locally by `scripts/chat_ui.py` and hosted per env on CloudFront (sign-in with Cognito or Entra, `chat_ui_url` in outputs.json). The CLI (`scripts/invoke_agent.py`, `scripts/run_local.py`) prints the agent reply, then a dim line listing the skills and tools used.
Agent replies are short, friendly and never invent order data: everything comes from tools.
