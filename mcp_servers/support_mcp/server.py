"""ShopAssist support MCP server (AgentCore Runtime MCP contract: stateless streamable HTTP on 0.0.0.0:8000/mcp).

The tool functions are plain functions, importable and testable without starting the server.

What this file does
    It is a small MCP server that offers three customer-support tools: `create_ticket`, `get_ticket`
    and `search_faq` (a keyword search over the bundled `faq.json`).

Where it fits
    This server runs on Amazon Bedrock AgentCore Runtime, a service that hosts it for us. The
    AgentCore Gateway lists it as its "support" target, so the agent sees the tools as
    `support___create_ticket` and so on. When the agent calls one, the Gateway signs in with its own
    machine token (OAuth) and forwards the call over HTTPS to this server. Tickets are saved through
    `common/store.py` (the DynamoDB tickets table when deployed).

    How the Runtime hosts it: `scripts/build.py` zips this file, `faq.json`, the `common/` folder and
    the Python libraries. The Runtime unpacks the zip, runs `server.py`, and expects an MCP endpoint on
    port 8000 at the path /mcp. "Stateless" means each request stands alone: the server keeps no
    conversation state between requests, which is what the Runtime's MCP contract asks for.

Step by step
    1. On start-up, an `MCPServer` named "shopassist-support" is created and the three tool functions
       are registered. Each function's name, its description text and its typed inputs become the
       tool definition that the AI model reads.
    2. When run as a program, the server listens on 0.0.0.0:8000/mcp ("0.0.0.0" means "accept
       connections from outside this machine", which the Runtime needs).
    3. For each tool call, the matching function runs and its dict result is sent back as JSON text.

    Important: the text in the tool functions' docstrings and in the `Field(description=...)` notes is
    NOT just a comment. The MCP library sends it to the AI model as the tool's instructions, so it is
    kept short and aimed at the model. Explanations for human readers are in the # comments above
    each function instead.

Key terms
    MCP (Model Context Protocol): a standard way for an AI agent to discover and call tools.
    MCP server: a program that offers tools over MCP. Clients ask it "tools/list" (which tools exist)
        and "tools/call" (run one).
    Streamable HTTP: the way MCP messages travel over normal web (HTTP) requests.
    AgentCore Runtime: the AWS service that runs the agent and this server in isolated small virtual
        machines.
    FAQ: frequently asked questions.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from common.store import get_store

# The FAQ entries ship next to this file inside the deployment zip.
FAQ_FILE = Path(__file__).with_name("faq.json")
PRIORITIES = ("low", "medium", "high")
# Common words that carry no meaning for the FAQ search ("how", "the", "my", ...), so they are ignored.
STOPWORDS = {"the", "and", "for", "you", "your", "are", "can", "how", "what", "does", "with", "my", "is",
             "do", "to", "of", "a", "an", "in", "on", "it", "i", "me", "about", "have", "will", "get"}

# The only priority values a ticket may have; the tool definition shows this list to the AI model.
Priority = Literal["low", "medium", "high"]


# Tool: open a support ticket so a human colleague can follow up (e.g. damaged item, complaint).
# Step 1: reject an unknown priority. Step 2: build the ticket with a random id like "TCK-1A2B3C",
# status OPEN and the current UTC time. Step 3: save it. Step 4: return the id, status and time,
# which the agent gives to the customer.
# Note: Field(description=...) texts and the docstring below are read by the AI model at runtime.
def create_ticket(
    customer_id: Annotated[str, Field(description='Customer id, e.g. "C001".')],
    subject: Annotated[str, Field(description="Short summary of the problem (one line).")],
    description: Annotated[str, Field(description="What happened, what the customer wants, and what was already tried.")],
    order_id: Annotated[str, Field(description='Related order number, e.g. "1042". Empty if none.')] = "",
    priority: Annotated[Priority, Field(description="low, medium (default) or high (damaged item, payment issue, angry customer).")] = "medium",
) -> dict[str, Any]:
    """Open a support ticket for a human agent to follow up. Use it when the request can't be solved with the other tools (damaged or wrong item, payment problem, complaint) or when the customer asks for a human. Returns the ticket_id (TCK-xxxxxx) to give to the customer, status OPEN and created_at."""
    # Step 1: check the priority (the MCP library also checks it; this guards direct calls in tests).
    if priority not in PRIORITIES:
        return {"error": "INVALID_PRIORITY", "allowed": list(PRIORITIES)}
    # Step 2: build the ticket. Text is trimmed and the customer id upper-cased for consistent lookups.
    ticket = {
        "ticket_id": f"TCK-{secrets.token_hex(3).upper()}",
        "customer_id": customer_id.strip().upper(),
        "order_id": order_id.strip(),
        "subject": subject.strip(),
        "description": description.strip(),
        "priority": priority,
        "status": "OPEN",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    # Step 3: save it (memory or the DynamoDB tickets table).
    get_store().put_ticket(ticket)
    # Step 4: return only what the customer needs to know.
    return {k: ticket[k] for k in ("ticket_id", "status", "created_at")}


# Tool: look up an existing ticket by its id (case does not matter: "tck-1a2b3c" works too).
# Returns the whole ticket, or an error the agent can explain if the id is unknown.
def get_ticket(ticket_id: Annotated[str, Field(description='Ticket id, e.g. "TCK-1A2B3C".')]) -> dict[str, Any]:
    """Get a support ticket by id: customer, related order, subject, description, priority, status and creation time. Returns {"error": "TICKET_NOT_FOUND"} if it doesn't exist."""
    ticket = get_store().get_ticket(ticket_id.strip().upper())
    return ticket if ticket else {"error": "TICKET_NOT_FOUND", "ticket_id": ticket_id}


@lru_cache(maxsize=1)
def load_faq() -> list[dict[str, Any]]:
    """Bundled FAQ entries.

    Each entry has a question, an answer and a list of keywords. The file is read once and then kept
    in memory (that is what the cache does).
    """
    return json.loads(FAQ_FILE.read_text(encoding="utf-8"))


def _terms(text: str) -> set[str]:
    """Split text into a set of simple search words.

    Lower-cases the text, keeps words made of letters, digits and dashes, drops stop words and
    one-letter words, and removes a trailing "s" from longer words so "refunds" matches "refund".
    """
    words = re.findall(r"[a-z0-9][a-z0-9-]*", text.lower())
    return {w.rstrip("s") if len(w) > 3 else w for w in words if w not in STOPWORDS and len(w) > 1}


# Tool: search the FAQ with a simple keyword score (no AI involved).
# Step 1: turn the customer's question into search words.
# Step 2: score every FAQ entry. A word that matches the entry's keywords counts 3 points, one in its
#         question 2 points, and one in its answer 1 point.
# Step 3: sort best first and keep at most 3 entries that are strong enough (see the threshold below).
def search_faq(query: Annotated[str, Field(description='Customer question or keywords, e.g. "how long is shipping".')]) -> dict[str, Any]:
    """Search the store's FAQ (shipping times, returns, refunds, damaged items, order changes, payment methods, tracking, contact). Returns up to 3 matching entries as {question, answer}; an empty list means no FAQ covers it."""
    # Step 1: the customer's search words.
    wanted = _terms(query)
    # Step 2: score each entry by how many search words it shares with it.
    scored = []
    for idx, entry in enumerate(load_faq()):
        score = (3 * len(wanted & _terms(" ".join(entry.get("keywords", []))))
                 + 2 * len(wanted & _terms(entry["question"]))
                 + len(wanted & _terms(entry["answer"])))
        if score:
            # The score is stored as a negative number so a normal sort puts the best match first;
            # idx keeps the file order when two entries have the same score.
            scored.append((-score, idx, entry))
    # Step 3: best first, then filter and keep the top 3.
    scored.sort()
    # Drop weak matches (a single incidental word in an answer) relative to the best hit.
    # The bar is at least 2 points, or half the best score if that is higher.
    threshold = max(2, -scored[0][0] / 2) if scored else 0
    results = [{"question": e["question"], "answer": e["answer"]} for s, _, e in scored[:3] if -s >= threshold]
    return {"results": results}


# The three tools this server offers. scripts/local_gateway.py reuses this list for local testing.
SUPPORT_TOOLS = (create_ticket, get_ticket, search_faq)

# Create the MCP server and register each tool. The "instructions" text is sent to MCP clients.
mcp = MCPServer("shopassist-support", instructions="ShopAssist customer support tools: tickets and FAQ.")
for _fn in SUPPORT_TOOLS:
    # Unstructured output: the tool's JSON is returned as text content (same as the Gateway's Lambda targets).
    mcp.add_tool(_fn, structured_output=False)


# Start the web server only when this file is run as a program (on the Runtime or locally),
# not when tests import it. Port 8000 and the /mcp path are what AgentCore Runtime expects.
if __name__ == "__main__":
    mcp.run("streamable-http", host="0.0.0.0", port=8000, streamable_http_path="/mcp", stateless_http=True)
