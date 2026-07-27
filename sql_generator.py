import anthropic
import os
from dotenv import load_dotenv
from schema_context import get_schema_context
from config import CHAT_MODEL

load_dotenv()

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def generate_sql(user_question: str, conversation_history: list = None) -> tuple[str, str]:
    """
    Converts a natural language question to a SQL query using Claude.

    Args:
        user_question: Plain English question from the user
        conversation_history: List of prior (question, sql) tuples for context

    Returns:
        (sql_query, error_message) — one will be None
    """
    schema = get_schema_context()

    # Build system prompt
    system_prompt = f"""{schema}

Your job: Convert the user's question into a single valid SQLite SQL query.
Output ONLY the SQL query. No explanation. No markdown. No backticks. No preamble.
If the question cannot be answered with the available data, output: SELECT 'Unable to answer this question with available data' AS message;
"""

    # Build messages — include conversation history for follow-up questions
    messages = []

    if conversation_history:
        for prior_q, prior_sql in conversation_history[-3:]:  # Last 3 exchanges
            messages.append({"role": "user", "content": prior_q})
            messages.append({"role": "assistant", "content": prior_sql})

    messages.append({"role": "user", "content": user_question})

    try:
        response = client.messages.create(
            model=CHAT_MODEL,
            max_tokens=1000,
            system=system_prompt,
            messages=messages,
        )
        sql = response.content[0].text.strip()

        # Clean up any accidental markdown wrapping
        if sql.startswith("```"):
            lines = sql.split("\n")
            sql = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])

        return sql, None

    except Exception as e:
        return None, f"SQL generation error: {str(e)}"